"""
Inference engine for carbon estimation using pre-trained models
"""

import ee
import numpy as np
from typing import Dict, Optional
import logging

from models.carbon_model import CarbonEstimationModel
from models.model_registry import ModelRegistry

logger = logging.getLogger(__name__)


class CarbonInferenceEngine:
    """
    Perform carbon estimation inference using pre-trained models
    """
    
    def __init__(self, model_name: Optional[str] = None):
        """
        Initialize inference engine
        
        Args:
            model_name: Name of model to use (uses default if None)
        """
        self.registry = ModelRegistry()
        
        if model_name is None:
            model_name = self.registry.registry.get('default')
            if model_name is None:
                raise ValueError("No default model found. Train a model first.")
        
        self.model_name = model_name
        logger.info(f"Loading model: {model_name}")
        
        # Load model
        model_path = self.registry.get_model_path(model_name)
        self.model = CarbonEstimationModel.load(model_path)
        
        logger.info("✓ Inference engine ready")
    
    def predict_for_region(self,
                           roi: ee.Geometry,
                           year: int,
                           start_month: int,
                           end_month: int,
                           cloud_threshold: int = 20) -> ee.Image:
        """
        Predict carbon stock for a region using the trained model
        
        Args:
            roi: Region of interest
            year: Year for Sentinel-2 imagery
            start_month: Start month
            end_month: End month
            cloud_threshold: Maximum cloud cover percentage
        
        Returns:
            Earth Engine Image with predicted carbon values
        """
        
        logger.info(f"Running inference for {year}-{start_month:02d} to {year}-{end_month:02d}")
        
        # Load Sentinel-2
        start_date = f'{year}-{start_month:02d}-01'
        end_date = f'{year}-{end_month:02d}-28'
        
        sen2_collection = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
                          .filterBounds(roi)
                          .filterDate(start_date, end_date)
                          .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', cloud_threshold)))
        
        collection_size = sen2_collection.size().getInfo()
        logger.info(f"Found {collection_size} Sentinel-2 images")
        
        if collection_size == 0:
            raise ValueError("No Sentinel-2 images found. Adjust parameters.")
        
        # Create median composite
        sen2 = sen2_collection.median().multiply(0.0001)
        
        # Select bands (must match training features)
        bands = ['B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'B8A', 'B11', 'B12']
        sen2 = sen2.select(bands)
        
        # Calculate indices (must match training)
        ndvi = sen2.normalizedDifference(['B8', 'B4']).rename('NDVI')
        ndwi = sen2.normalizedDifference(['B3', 'B8']).rename('NDWI')
        ndmi = sen2.normalizedDifference(['B8', 'B11']).rename('NDMI')
        
        evi = sen2.expression(
            '2.5 * ((NIR - RED) / (NIR + 6 * RED - 7.5 * BLUE + 1))',
            {
                'NIR': sen2.select('B8'),
                'RED': sen2.select('B4'),
                'BLUE': sen2.select('B2')
            }
        ).rename('EVI')
        
        savi = sen2.expression(
            '((NIR - RED) / (NIR + RED + 0.5)) * 1.5',
            {
                'NIR': sen2.select('B8'),
                'RED': sen2.select('B4')
            }
        ).rename('SAVI')
        
        # Combine features
        predictors = (ee.Image.constant(1).rename('constant')
                     .addBands(sen2)
                     .addBands([ndvi, ndwi, ndmi, evi, savi]))
        
        # Get model coefficients
        # For sklearn models, we need to convert to GEE-compatible format
        coefficients = self._get_model_coefficients_for_gee()
        
        # Apply model
        carbon_predicted = (predictors
                           .multiply(ee.Image.constant(coefficients))
                           .reduce(ee.Reducer.sum())
                           .rename('carbon_estimated'))
        
        logger.info("✓ Inference complete")
        
        return carbon_predicted
    
    def _get_model_coefficients_for_gee(self) -> list:
        """
        Extract model coefficients for GEE application
        
        For linear models, this is straightforward.
        For tree-based models, we create a linear surrogate.
        """
        
        # Check if model is linear
        if hasattr(self.model.model, 'coef_'):
            # Linear model
            coefficients = self.model.model.coef_.tolist()
            intercept = float(self.model.model.intercept_)
            
            # Adjust first coefficient to include intercept
            coefficients[0] += intercept
            
            return coefficients
        else:
            # Non-linear model - create linear surrogate
            logger.warning(
                f"Model algorithm '{self.model.algorithm}' is not directly compatible with GEE. "
                "Creating linear surrogate model for GEE deployment."
            )
            
            return self._create_linear_surrogate()
        
    def _create_linear_surrogate(self) -> list:
        """
        Create a linear surrogate model for non-linear models
        This allows deployment to GEE at the cost of some accuracy
        """
        from sklearn.linear_model import Ridge
        
        # Get training data from model metadata
        training_info = self.model.metadata.get('training_info', {})
        
        if 'X_sample' not in training_info or 'y_sample' not in training_info:
            raise ValueError(
                "Cannot create surrogate: training data not saved in model. "
                "Please retrain the model or use a linear algorithm."
            )
        
        X_sample = np.array(training_info['X_sample'])
        y_sample = np.array(training_info['y_sample'])
        
        logger.info("Training linear surrogate model...")
        
        # Predict with original model
        y_pred_original = self.model.model.predict(X_sample)
        
        # Train Ridge regression to approximate the original model
        surrogate = Ridge(alpha=1.0)
        surrogate.fit(X_sample, y_pred_original)
        
        # Get coefficients
        coefficients = surrogate.coef_.tolist()
        intercept = float(surrogate.intercept_)
        coefficients[0] += intercept
        
        # Calculate surrogate accuracy
        from sklearn.metrics import r2_score, mean_squared_error
        y_pred_surrogate = surrogate.predict(X_sample)
        r2 = r2_score(y_pred_original, y_pred_surrogate)
        rmse = np.sqrt(mean_squared_error(y_pred_original, y_pred_surrogate))
        
        logger.info(f"✓ Linear surrogate created:")
        logger.info(f"  R² (surrogate vs original): {r2:.4f}")
        logger.info(f"  RMSE (surrogate vs original): {rmse:.2f} Mg/ha")
        
        if r2 < 0.8:
            logger.warning(
                f"Surrogate R² is low ({r2:.4f}). "
                "Consider using a linear model for better GEE compatibility."
            )
        
        return coefficients
    
    def get_model_info(self) -> Dict:
        """Get information about the loaded model"""
        return self.model.get_info()