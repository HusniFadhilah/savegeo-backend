"""
Carbon Estimation Model
Supports 19 algorithms: Linear, Ridge, Lasso, ElasticNet, Decision Tree,
Random Forest, Extra Trees, Gradient Boosting, HistGradientBoosting,
XGBoost, LightGBM, CatBoost, AdaBoost, SVR, SGD, KNN, GPR, and MLP.
"""

import json
import hashlib
import hmac
import logging
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from app.inference.safe_model import OnnxRegressor

# --- Core scikit-learn (required) ---
try:
    from sklearn.ensemble import (
        AdaBoostRegressor,
        ExtraTreesRegressor,
        GradientBoostingRegressor,
        HistGradientBoostingRegressor,
        RandomForestRegressor,
    )
    from sklearn.gaussian_process import GaussianProcessRegressor
    from sklearn.linear_model import ElasticNet, Lasso, LinearRegression, Ridge, SGDRegressor
    from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
    from sklearn.model_selection import KFold
    from sklearn.neighbors import KNeighborsRegressor
    from sklearn.neural_network import MLPRegressor
    from sklearn.preprocessing import StandardScaler
    from sklearn.svm import SVR
    from sklearn.tree import DecisionTreeRegressor
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    print("⚠ Warning: scikit-learn not available")

# --- Optional boosting libraries (graceful degradation) ---
try:
    from xgboost import XGBRegressor
    XGBOOST_AVAILABLE = True
except ImportError:
    XGBOOST_AVAILABLE = False

try:
    from lightgbm import LGBMRegressor
    LGBM_AVAILABLE = True
except ImportError:
    LGBM_AVAILABLE = False

try:
    from catboost import CatBoostRegressor
    CATBOOST_AVAILABLE = True
except ImportError:
    CATBOOST_AVAILABLE = False

logger = logging.getLogger(__name__)


def _model_load_error_message(filepath: Path, metadata: dict, exc: Exception) -> str:
    """Return a user-facing message for common sklearn/joblib incompatibilities."""
    try:
        import sklearn
        sklearn_version = sklearn.__version__
    except Exception:  # noqa: BLE001 - version is just for a diagnostic message, never worth failing over
        sklearn_version = "unknown"

    raw = str(exc)
    if "__pyx_unpickle_CyHalfSquaredError" in raw or "CyHalfSquaredError" in raw:
        return (
            f"Model '{filepath.with_suffix('.pkl').name}' tidak kompatibel dengan "
            f"versi scikit-learn yang sedang berjalan ({sklearn_version}). "
            "Install dependency backend sesuai requirements.txt "
            "(scikit-learn==1.3.2), lalu restart backend. "
            "Alternatif: latih ulang / upload ulang model dengan versi scikit-learn saat ini. "
            f"Detail asli: {raw}"
        )

    return (
        f"Gagal membuka model '{filepath.with_suffix('.pkl').name}' "
        f"(algorithm={metadata.get('algorithm', 'unknown')}, sklearn={sklearn_version}): {raw}"
    )


def _build_supported_algorithms():
    """Build the supported algorithms dict dynamically based on available packages."""
    if not SKLEARN_AVAILABLE:
        return {}

    algos = {
        'linear':               LinearRegression,
        'ridge':                Ridge,
        'lasso':                Lasso,
        'elastic_net':          ElasticNet,
        'decision_tree':        DecisionTreeRegressor,
        'random_forest':        RandomForestRegressor,
        'extra_trees':          ExtraTreesRegressor,
        'gradient_boosting':    GradientBoostingRegressor,
        'hist_gradient_boosting': HistGradientBoostingRegressor,
        'adaboost':             AdaBoostRegressor,
        'svr':                  SVR,
        'sgd':                  SGDRegressor,
        'knn':                  KNeighborsRegressor,
        'gpr':                  GaussianProcessRegressor,
        'mlp':                  MLPRegressor,
    }

    if XGBOOST_AVAILABLE:
        algos['xgboost'] = XGBRegressor
    if LGBM_AVAILABLE:
        algos['lightgbm'] = LGBMRegressor
    if CATBOOST_AVAILABLE:
        algos['catboost'] = CatBoostRegressor

    return algos


class CarbonEstimationModel:
    """
    Carbon stock estimation model with training, validation, and inference capabilities.
    Supports 19 regression algorithms.
    """

    SUPPORTED_ALGORITHMS = _build_supported_algorithms()

    def __init__(self, algorithm='linear', **kwargs):
        """
        Initialize model

        Args:
            algorithm: One of the supported algorithm keys (e.g. 'random_forest')
            **kwargs: Algorithm-specific parameters passed to the underlying estimator
        """
        if not SKLEARN_AVAILABLE:
            raise ImportError("scikit-learn is required for CarbonEstimationModel")

        # Refresh at instantiation time in case packages were imported later
        self.SUPPORTED_ALGORITHMS = _build_supported_algorithms()

        if algorithm not in self.SUPPORTED_ALGORITHMS:
            raise ValueError(
                f"Unsupported algorithm: '{algorithm}'. "
                f"Available: {sorted(self.SUPPORTED_ALGORITHMS.keys())}"
            )

        self.algorithm = algorithm
        self.model = self.SUPPORTED_ALGORITHMS[algorithm](**kwargs)
        self.scaler = StandardScaler()
        self.feature_names = None
        self.metadata = {
            'algorithm': algorithm,
            'version': '2.0',
            'created_at': datetime.now(UTC).isoformat(),
            'training_params': kwargs
        }

    def train(self,
              X: np.ndarray,
              y: np.ndarray,
              feature_names: list[str],
              cv_folds: int = 5,
              scale_features: bool = True) -> dict:
        """
        Train the model with cross-validation.

        Args:
            X: Training features (n_samples, n_features)
            y: Target values (n_samples,)
            feature_names: List of feature names
            cv_folds: Number of cross-validation folds
            scale_features: Whether to standardize features before fitting

        Returns:
            Dict with training metrics and CV metrics
        """
        logger.info(f"Training '{self.algorithm}' with {len(X)} samples, {cv_folds}-fold CV")

        self.feature_names = feature_names
        self._scale_features = scale_features

        if len(X) < 50:
            raise ValueError(f"Insufficient training samples: {len(X)} (minimum 50)")

        # Each validation fold learns its own preprocessing from training rows.
        cv_metrics = self._cross_validate(X, y, cv_folds)

        # The final inference scaler is fitted only after cross-validation.
        if scale_features:
            X_scaled = self.scaler.fit_transform(X)
        else:
            X_scaled = X.copy()

        # Fit final model on the full training set
        self.model.fit(X_scaled, y)

        # Training-set metrics (in-sample; use CV metrics for generalisation estimate)
        y_pred_train = self.model.predict(X_scaled)
        train_metrics = {
            'rmse': float(np.sqrt(mean_squared_error(y, y_pred_train))),
            'mae':  float(mean_absolute_error(y, y_pred_train)),
            'r2':   float(r2_score(y, y_pred_train))
        }

        self.metadata.update({
            'n_samples':       len(X),
            'n_features':      len(feature_names),
            'feature_names':   feature_names,
            'train_metrics':   train_metrics,
            'cv_metrics':      cv_metrics,
            'cv_method':       'kfold_train_only_scaler_v1',
            'scaled_features': scale_features,
            'trained_at':      datetime.now(UTC).isoformat()
        })

        # Feature importance (where available)
        if hasattr(self.model, 'feature_importances_'):
            importance = dict(zip(feature_names, self.model.feature_importances_))
            self.metadata['feature_importance'] = {
                k: float(v) for k, v in sorted(importance.items(),
                                                key=lambda x: x[1], reverse=True)
            }
        elif hasattr(self.model, 'coef_'):
            coef = self.model.coef_
            if coef.ndim == 1:
                importance = dict(zip(feature_names, np.abs(coef)))
                self.metadata['feature_importance'] = {
                    k: float(v) for k, v in sorted(importance.items(),
                                                    key=lambda x: x[1], reverse=True)
                }

        logger.info(
            f"  Train  RMSE={train_metrics['rmse']:.2f}  R²={train_metrics['r2']:.4f}"
        )
        logger.info(
            f"  CV     RMSE={cv_metrics['rmse_mean']:.2f}±{cv_metrics['rmse_std']:.2f}"
            f"  R²={cv_metrics['r2_mean']:.4f}±{cv_metrics['r2_std']:.4f}"
        )

        return {
            'train_metrics':    train_metrics,
            'cv_metrics':       cv_metrics,
            'n_samples':        len(X),
            'feature_importance': self.metadata.get('feature_importance', {})
        }

    def _cross_validate(self, X: np.ndarray, y: np.ndarray, n_folds: int) -> dict:
        """Fit preprocessing on training rows independently in every fold."""
        kfold = KFold(n_splits=n_folds, shuffle=True, random_state=42)

        cv_rmse, cv_mae, cv_r2 = [], [], []

        for train_idx, val_idx in kfold.split(X):
            X_tr, X_val = X[train_idx], X[val_idx]
            y_tr, y_val = y[train_idx], y[val_idx]
            if self._scale_features:
                fold_scaler = StandardScaler()
                X_tr = fold_scaler.fit_transform(X_tr)
                X_val = fold_scaler.transform(X_val)

            temp_model = self.SUPPORTED_ALGORITHMS[self.algorithm](
                **self.metadata['training_params']
            )
            temp_model.fit(X_tr, y_tr)
            y_pred = temp_model.predict(X_val)

            cv_rmse.append(np.sqrt(mean_squared_error(y_val, y_pred)))
            cv_mae.append(mean_absolute_error(y_val, y_pred))
            cv_r2.append(r2_score(y_val, y_pred))

        return {
            'rmse_mean': float(np.mean(cv_rmse)),
            'rmse_std':  float(np.std(cv_rmse)),
            'mae_mean':  float(np.mean(cv_mae)),
            'mae_std':   float(np.std(cv_mae)),
            'r2_mean':   float(np.mean(cv_r2)),
            'r2_std':    float(np.std(cv_r2)),
            'n_folds':   n_folds
        }

    def predict(self, X: np.ndarray) -> np.ndarray:
        """
        Make predictions on new data.

        The scaler fitted during training is applied automatically.

        Args:
            X: Feature array (n_samples, n_features)

        Returns:
            Predicted carbon values (n_samples,)
        """
        if self.model is None:
            raise ValueError("Model not trained. Call train() first.")

        if hasattr(self.scaler, 'mean_') and self.metadata.get('scaled_features', True):
            X_scaled = self.scaler.transform(X)
        else:
            X_scaled = X

        return self.model.predict(X_scaled)

    def save(self, filepath: str):
        """Export a data-only estimator and JSON preprocessing state."""
        from app.inference.safe_model import export_estimator
        target_path = Path(filepath)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.feature_names:
            raise ValueError("Train the model before exporting")
        export_estimator(self.model, target_path.with_suffix('.onnx'), len(self.feature_names))
        metadata = self.metadata.copy()
        metadata['format'] = 'onnx'
        metadata['artifact_sha256'] = hashlib.sha256(target_path.with_suffix('.onnx').read_bytes()).hexdigest()
        if hasattr(self.scaler, 'mean_'):
            metadata['scaler'] = {k: np.asarray(getattr(self.scaler, k)).tolist()
                                  for k in ('mean_', 'scale_', 'var_')}
        if hasattr(self.model, 'coef_') and np.asarray(self.model.coef_).ndim == 1:
            metadata['linear_parameters'] = {'coef': self.model.coef_.tolist(),
                                              'intercept': float(self.model.intercept_)}
        target_path.with_suffix('.json').write_text(json.dumps(metadata, indent=2), encoding='utf8')

    @classmethod
    def load(cls, filepath: str) -> 'CarbonEstimationModel':
        """Load ONNX + JSON only. Legacy pickle conversion is an offline operation."""
        target_path = Path(filepath)
        model_path = target_path.with_suffix('.onnx')
        if not model_path.is_file() or model_path.is_symlink():
            raise ValueError("Model requires a verified ONNX export before it can be used")
        metadata_path = target_path.with_suffix('.json')
        if metadata_path.is_symlink():
            raise ValueError("Model metadata must be a regular file")
        metadata = json.loads(metadata_path.read_text(encoding='utf8'))
        raw = model_path.read_bytes()
        expected = metadata.get('artifact_sha256', '')
        if not isinstance(expected, str) or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected):
            raise ValueError("Model integrity verification failed")
        instance = cls(algorithm=metadata['algorithm'])
        instance.model = OnnxRegressor(raw, metadata)
        instance.feature_names = metadata.get('feature_names')
        instance.metadata = metadata
        for key, value in metadata.get('scaler', {}).items():
            if key not in {'mean_', 'scale_', 'var_'}:
                raise ValueError("Invalid scaler metadata")
            values = np.asarray(value, dtype=float)
            if values.ndim != 1 or not np.isfinite(values).all():
                raise ValueError("Invalid scaler values")
            setattr(instance.scaler, key, values)
        if hasattr(instance.scaler, 'mean_'):
            instance.scaler.n_features_in_ = len(instance.scaler.mean_)
        return instance

    def get_info(self) -> dict:
        """Return model metadata."""
        return self.metadata.copy()
