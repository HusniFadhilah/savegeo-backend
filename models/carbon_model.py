"""
Carbon Estimation Model
Supports multiple algorithms: Linear Regression, Random Forest, XGBoost
"""

import numpy as np
import pickle
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import logging

try:
    from sklearn.linear_model import LinearRegression, Ridge, Lasso
    from sklearn.ensemble import RandomForestRegressor, GradientBoostingRegressor
    from sklearn.preprocessing import StandardScaler
    from sklearn.model_selection import KFold
    from sklearn.metrics import mean_squared_error, r2_score, mean_absolute_error
    import joblib
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    print("⚠ Warning: scikit-learn not available")

logger = logging.getLogger(__name__)


class CarbonEstimationModel:
    """
    Carbon stock estimation model with training, validation, and inference capabilities
    """
    
    SUPPORTED_ALGORITHMS = {
        'linear': LinearRegression,
        'ridge': Ridge,
        'lasso': Lasso,
        'random_forest': RandomForestRegressor,
        'gradient_boosting': GradientBoostingRegressor
    }
    
    def __init__(self, algorithm='linear', **kwargs):
        """
        Initialize model
        
        Args:
            algorithm: 'linear', 'ridge', 'lasso', 'random_forest', 'gradient_boosting'
            **kwargs: Algorithm-specific parameters
        """
        if not SKLEARN_AVAILABLE:
            raise ImportError("scikit-learn is required for CarbonEstimationModel")
        
        self.algorithm = algorithm
        self.model = None
        self.scaler = StandardScaler()
        self.feature_names = None
        self.metadata = {
            'algorithm': algorithm,
            'version': '1.0',
            'created_at': datetime.now().isoformat(),
            'training_params': kwargs
        }
        
        # Initialize model
        if algorithm in self.SUPPORTED_ALGORITHMS:
            self.model = self.SUPPORTED_ALGORITHMS[algorithm](**kwargs)
        else:
            raise ValueError(f"Unsupported algorithm: {algorithm}. Choose from {list(self.SUPPORTED_ALGORITHMS.keys())}")
    
    def train(self, 
              X: np.ndarray, 
              y: np.ndarray, 
              feature_names: List[str],
              validation_split: float = 0.2,
              cv_folds: int = 5,
              scale_features: bool = True) -> Dict:
        """
        Train the model with cross-validation
        
        Args:
            X: Training features (n_samples, n_features)
            y: Target values (n_samples,)
            feature_names: List of feature names
            validation_split: Fraction for validation set
            cv_folds: Number of cross-validation folds
            scale_features: Whether to standardize features
        
        Returns:
            Dict with training metrics
        """
        logger.info(f"Training {self.algorithm} model with {len(X)} samples")
        
        self.feature_names = feature_names
        
        # Data validation
        if len(X) < 50:
            raise ValueError(f"Insufficient training samples: {len(X)} (minimum 50 required)")
        
        # Feature scaling
        if scale_features:
            X_scaled = self.scaler.fit_transform(X)
        else:
            X_scaled = X
        
        # Cross-validation
        cv_metrics = self._cross_validate(X_scaled, y, cv_folds)
        
        # Train final model on all data
        self.model.fit(X_scaled, y)
        
        # Calculate training metrics
        y_pred = self.model.predict(X_scaled)
        train_metrics = {
            'rmse': np.sqrt(mean_squared_error(y, y_pred)),
            'mae': mean_absolute_error(y, y_pred),
            'r2': r2_score(y, y_pred)
        }
        
        # Update metadata
        self.metadata.update({
            'n_samples': len(X),
            'n_features': len(feature_names),
            'feature_names': feature_names,
            'train_metrics': train_metrics,
            'cv_metrics': cv_metrics,
            'scaled_features': scale_features,
            'trained_at': datetime.now().isoformat()
        })
        
        # Feature importance (if available)
        if hasattr(self.model, 'feature_importances_'):
            importance = dict(zip(feature_names, self.model.feature_importances_))
            self.metadata['feature_importance'] = {
                k: float(v) for k, v in sorted(importance.items(), key=lambda x: x[1], reverse=True)
            }
        elif hasattr(self.model, 'coef_'):
            importance = dict(zip(feature_names, np.abs(self.model.coef_)))
            self.metadata['feature_importance'] = {
                k: float(v) for k, v in sorted(importance.items(), key=lambda x: x[1], reverse=True)
            }
        
        logger.info(f"✓ Training complete: RMSE={train_metrics['rmse']:.2f}, R²={train_metrics['r2']:.4f}")
        
        return {
            'train_metrics': train_metrics,
            'cv_metrics': cv_metrics,
            'n_samples': len(X),
            'feature_importance': self.metadata.get('feature_importance', {})
        }
    
    def _cross_validate(self, X: np.ndarray, y: np.ndarray, n_folds: int) -> Dict:
        """
        Perform k-fold cross-validation
        """
        kfold = KFold(n_splits=n_folds, shuffle=True, random_state=42)
        
        cv_rmse = []
        cv_mae = []
        cv_r2 = []
        
        for fold, (train_idx, val_idx) in enumerate(kfold.split(X)):
            X_train, X_val = X[train_idx], X[val_idx]
            y_train, y_val = y[train_idx], y[val_idx]
            
            # Create temporary model
            if self.algorithm in self.SUPPORTED_ALGORITHMS:
                temp_model = self.SUPPORTED_ALGORITHMS[self.algorithm](
                    **self.metadata['training_params']
                )
            else:
                temp_model = LinearRegression()
            
            temp_model.fit(X_train, y_train)
            y_pred = temp_model.predict(X_val)
            
            cv_rmse.append(np.sqrt(mean_squared_error(y_val, y_pred)))
            cv_mae.append(mean_absolute_error(y_val, y_pred))
            cv_r2.append(r2_score(y_val, y_pred))
        
        return {
            'rmse_mean': float(np.mean(cv_rmse)),
            'rmse_std': float(np.std(cv_rmse)),
            'mae_mean': float(np.mean(cv_mae)),
            'mae_std': float(np.std(cv_mae)),
            'r2_mean': float(np.mean(cv_r2)),
            'r2_std': float(np.std(cv_r2)),
            'n_folds': n_folds
        }
    
    def predict(self, X: np.ndarray) -> np.ndarray:
        """
        Make predictions
        
        Args:
            X: Feature array (n_samples, n_features)
        
        Returns:
            Predicted carbon values (n_samples,)
        """
        if self.model is None:
            raise ValueError("Model not trained. Call train() first or load a saved model.")
        
        # Scale features if scaler was fitted
        if hasattr(self.scaler, 'mean_'):
            X_scaled = self.scaler.transform(X)
        else:
            X_scaled = X
        
        return self.model.predict(X_scaled)
    
    def save(self, filepath: str):
        """
        Save model and metadata
        
        Args:
            filepath: Path to save model (without extension)
        """
        filepath = Path(filepath)
        
        # Save model
        model_path = filepath.with_suffix('.pkl')
        joblib.dump({
            'model': self.model,
            'scaler': self.scaler,
            'feature_names': self.feature_names
        }, model_path)
        
        # Save metadata
        metadata_path = filepath.with_suffix('.json')
        with open(metadata_path, 'w') as f:
            json.dump(self.metadata, f, indent=2)
        
        logger.info(f"✓ Model saved to {model_path}")
        logger.info(f"✓ Metadata saved to {metadata_path}")
    
    @classmethod
    def load(cls, filepath: str) -> 'CarbonEstimationModel':
        """
        Load saved model
        
        Args:
            filepath: Path to model file (without extension)
        
        Returns:
            Loaded CarbonEstimationModel instance
        """
        filepath = Path(filepath)
        
        # Load metadata
        metadata_path = filepath.with_suffix('.json')
        with open(metadata_path, 'r') as f:
            metadata = json.load(f)
        
        # Create instance
        instance = cls(
            algorithm=metadata['algorithm'],
            **metadata.get('training_params', {})
        )
        
        # Load model
        model_path = filepath.with_suffix('.pkl')
        saved_data = joblib.load(model_path)
        
        instance.model = saved_data['model']
        instance.scaler = saved_data['scaler']
        instance.feature_names = saved_data['feature_names']
        instance.metadata = metadata
        
        logger.info(f"✓ Model loaded from {model_path}")
        logger.info(f"  Algorithm: {metadata['algorithm']}")
        logger.info(f"  Trained: {metadata.get('trained_at', 'Unknown')}")
        logger.info(f"  CV RMSE: {metadata.get('cv_metrics', {}).get('rmse_mean', 'N/A')}")
        
        return instance
    
    def get_info(self) -> Dict:
        """Get model information"""
        return self.metadata.copy()