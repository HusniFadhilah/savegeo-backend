"""
Carbon Estimation Model
Supports 19 algorithms: Linear, Ridge, Lasso, ElasticNet, Decision Tree,
Random Forest, Extra Trees, Gradient Boosting, HistGradientBoosting,
XGBoost, LightGBM, CatBoost, AdaBoost, SVR, SGD, KNN, GPR, and MLP.
"""

import numpy as np
import pickle
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import logging

# --- Core scikit-learn (required) ---
try:
    from sklearn.linear_model import (
        LinearRegression, Ridge, Lasso, ElasticNet, SGDRegressor
    )
    from sklearn.tree import DecisionTreeRegressor
    from sklearn.ensemble import (
        RandomForestRegressor, ExtraTreesRegressor,
        GradientBoostingRegressor, HistGradientBoostingRegressor,
        AdaBoostRegressor
    )
    from sklearn.svm import SVR
    from sklearn.neighbors import KNeighborsRegressor
    from sklearn.gaussian_process import GaussianProcessRegressor
    from sklearn.gaussian_process.kernels import RBF, WhiteKernel
    from sklearn.neural_network import MLPRegressor
    from sklearn.preprocessing import StandardScaler
    from sklearn.model_selection import KFold
    from sklearn.metrics import mean_squared_error, r2_score, mean_absolute_error
    import joblib
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


def _model_load_error_message(filepath: Path, metadata: Dict, exc: Exception) -> str:
    """Return a user-facing message for common sklearn/joblib incompatibilities."""
    try:
        import sklearn
        sklearn_version = sklearn.__version__
    except Exception:
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
            'created_at': datetime.now().isoformat(),
            'training_params': kwargs
        }

    def train(self,
              X: np.ndarray,
              y: np.ndarray,
              feature_names: List[str],
              cv_folds: int = 5,
              scale_features: bool = True) -> Dict:
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

        # Fit scaler and transform ONLY on training data
        if scale_features:
            X_scaled = self.scaler.fit_transform(X)
        else:
            X_scaled = X.copy()

        # Cross-validation (on training data only - no data leakage)
        cv_metrics = self._cross_validate(X_scaled, y, cv_folds)

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
            'scaled_features': scale_features,
            'trained_at':      datetime.now().isoformat()
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

    def _cross_validate(self, X: np.ndarray, y: np.ndarray, n_folds: int) -> Dict:
        """Perform k-fold cross-validation on the (already-scaled) training data."""
        kfold = KFold(n_splits=n_folds, shuffle=True, random_state=42)

        cv_rmse, cv_mae, cv_r2 = [], [], []

        for train_idx, val_idx in kfold.split(X):
            X_tr, X_val = X[train_idx], X[val_idx]
            y_tr, y_val = y[train_idx], y[val_idx]

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
        """Save model, scaler, and metadata."""
        filepath = Path(filepath)
        filepath.parent.mkdir(parents=True, exist_ok=True)

        model_path = filepath.with_suffix('.pkl')
        joblib.dump({
            'model':         self.model,
            'scaler':        self.scaler,
            'feature_names': self.feature_names
        }, model_path)

        metadata_path = filepath.with_suffix('.json')
        with open(metadata_path, 'w') as f:
            json.dump(self.metadata, f, indent=2)

        logger.info(f"✓ Model saved to {model_path}")
        logger.info(f"✓ Metadata saved to {metadata_path}")

    @classmethod
    def load(cls, filepath: str) -> 'CarbonEstimationModel':
        """Load a previously saved model."""
        filepath = Path(filepath)

        metadata_path = filepath.with_suffix('.json')
        with open(metadata_path, 'r') as f:
            metadata = json.load(f)

        instance = cls(
            algorithm=metadata['algorithm'],
            **metadata.get('training_params', {})
        )

        model_path = filepath.with_suffix('.pkl')
        try:
            saved_data = joblib.load(model_path)
        except Exception as exc:
            raise ValueError(_model_load_error_message(filepath, metadata, exc)) from exc

        instance.model         = saved_data['model']
        instance.scaler        = saved_data['scaler']
        instance.feature_names = saved_data['feature_names']
        instance.metadata      = metadata

        logger.info(f"✓ Model loaded: {metadata['algorithm']}")
        cv = metadata.get('cv_metrics', {})
        rmse_mean = cv.get('rmse_mean')
        r2_mean = cv.get('r2_mean')
        rmse_str = f"{rmse_mean:.2f}" if isinstance(rmse_mean, (int, float)) else "N/A"
        r2_str = f"{r2_mean:.4f}" if isinstance(r2_mean, (int, float)) else "N/A"
        logger.info(f"  CV RMSE: {rmse_str}  R²: {r2_str}")

        return instance

    def get_info(self) -> Dict:
        """Return model metadata."""
        return self.metadata.copy()
