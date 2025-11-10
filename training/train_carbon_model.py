"""
Training script for carbon estimation model
Run this to train a new model or retrain existing one
"""

import ee, os
import numpy as np
import argparse
import logging
from pathlib import Path
import sys
from datetime import datetime

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from models.carbon_model import CarbonEstimationModel
from models.model_registry import ModelRegistry
from training.data_preparation import sample_training_data

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

    
# Initialize Earth Engine
SERVICE_ACCOUNT = os.getenv("GEE_SERVICE_ACCOUNT", "your-sa@project.iam.gserviceaccount.com")
KEY_FILE = os.getenv("GEE_KEY_FILE", "../endless-bounty-416008-a6cce2f8b208.json")
API_BASE_URL = "https://api.sp3stab.id/api/en"

def init_ee():
    if not Path(KEY_FILE).exists():
        raise FileNotFoundError(f"Key file not found: {KEY_FILE}")
    credentials = ee.ServiceAccountCredentials(SERVICE_ACCOUNT, KEY_FILE)
    ee.Initialize(credentials)
    print("✓ Earth Engine initialized with Service Account")

init_ee()

def train_carbon_model(
    training_region: str = 'indonesia',
    reference_dataset: str = 'WCMC',
    n_samples: int = 10000,
    algorithm: str = 'random_forest',
    model_name: str = None,
    **model_params
):
    """
    Train carbon estimation model
    
    Args:
        training_region: Geographic region for training data
        reference_dataset: Reference biomass dataset
        n_samples: Number of training samples
        algorithm: ML algorithm to use
        model_name: Name for saving model
        **model_params: Algorithm-specific parameters
    """
    
    logger.info("=" * 60)
    logger.info("CARBON ESTIMATION MODEL TRAINING")
    logger.info("=" * 60)
    
    # Define training region
    if training_region == 'indonesia':
        roi = ee.Geometry.Rectangle([95, -11, 141, 6])  # Indonesia bbox
    elif training_region == 'southeast_asia':
        roi = ee.Geometry.Rectangle([95, -11, 155, 20])
    elif training_region == 'global':
        roi = ee.Geometry.Rectangle([-180, -60, 180, 60])
    else:
        raise ValueError(f"Unknown region: {training_region}")
    
    logger.info(f"Training region: {training_region}")
    logger.info(f"Reference dataset: {reference_dataset}")
    logger.info(f"Target samples: {n_samples}")
    logger.info(f"Algorithm: {algorithm}")
    
    # Sample training data from Google Earth Engine
    logger.info("\n--- Sampling Training Data ---")
    training_data = sample_training_data(
        roi=roi,
        reference_dataset=reference_dataset,
        n_samples=n_samples,
        year=2022
    )
    
    X = training_data['features']
    y = training_data['target']
    feature_names = training_data['feature_names']
    
    logger.info(f"✓ Sampled {len(X)} valid training samples")
    logger.info(f"✓ Features: {len(feature_names)}")
    logger.info(f"✓ Target range: {y.min():.2f} - {y.max():.2f} Mg/ha")
    
    # Initialize model
    logger.info("\n--- Training Model ---")
    model = CarbonEstimationModel(algorithm=algorithm, **model_params)
    
    # Train with cross-validation
    training_results = model.train(
        X=X,
        y=y,
        feature_names=feature_names,
        cv_folds=5,
        scale_features=True
    )
    
    # Display results
    logger.info("\n--- Training Results ---")
    logger.info(f"Training RMSE: {training_results['train_metrics']['rmse']:.2f} Mg/ha")
    logger.info(f"Training R²: {training_results['train_metrics']['r2']:.4f}")
    logger.info(f"CV RMSE: {training_results['cv_metrics']['rmse_mean']:.2f} ± {training_results['cv_metrics']['rmse_std']:.2f} Mg/ha")
    logger.info(f"CV R²: {training_results['cv_metrics']['r2_mean']:.4f} ± {training_results['cv_metrics']['r2_std']:.4f}")
    
    # Feature importance
    if 'feature_importance' in training_results:
        logger.info("\n--- Top 10 Important Features ---")
        for i, (feat, imp) in enumerate(list(training_results['feature_importance'].items())[:10], 1):
            logger.info(f"{i}. {feat}: {imp:.4f}")
    
    # Save model
    if model_name is None:
        model_name = f"carbon_{algorithm}_{training_region}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    model_path = Path('saved_models') / model_name
    model.save(str(model_path))
    
    # Register model
    registry = ModelRegistry()
    registry.register_model(
        model_name=model_name,
        model_path=str(model_path),
        metadata=model.get_info(),
        set_as_default=True
    )
    
    logger.info(f"\n✓ Model saved and registered: {model_name}")
    logger.info("=" * 60)
    
    return model


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Train carbon estimation model')
    parser.add_argument('--region', default='indonesia', 
                        choices=['indonesia', 'southeast_asia', 'global'],
                        help='Training region')
    parser.add_argument('--dataset', default='WCMC',
                        choices=['WCMC', 'ESA_CCI', 'GEDI', 'Simard'],
                        help='Reference biomass dataset')
    parser.add_argument('--samples', type=int, default=10000,
                        help='Number of training samples')
    parser.add_argument('--algorithm', default='random_forest',
                        choices=['linear', 'ridge', 'lasso', 'random_forest', 'gradient_boosting'],
                        help='ML algorithm')
    parser.add_argument('--name', type=str, default=None,
                        help='Model name (auto-generated if not provided)')
    
    # Random Forest specific parameters
    parser.add_argument('--n_estimators', type=int, default=100,
                        help='Number of trees (Random Forest)')
    parser.add_argument('--max_depth', type=int, default=20,
                        help='Max tree depth (Random Forest)')
    
    args = parser.parse_args()
    
    # Prepare model parameters
    model_params = {}
    if args.algorithm == 'random_forest':
        model_params = {
            'n_estimators': args.n_estimators,
            'max_depth': args.max_depth,
            'random_state': 42,
            'n_jobs': -1
        }
    elif args.algorithm == 'gradient_boosting':
        model_params = {
            'n_estimators': args.n_estimators,
            'max_depth': args.max_depth,
            'random_state': 42
        }
    
    # Train model
    train_carbon_model(
        training_region=args.region,
        reference_dataset=args.dataset,
        n_samples=args.samples,
        algorithm=args.algorithm,
        model_name=args.name,
        **model_params
    )