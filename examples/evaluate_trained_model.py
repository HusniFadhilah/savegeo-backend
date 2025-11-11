"""
Example: Evaluate trained carbon model
File: examples/evaluate_trained_model.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
from models.carbon_model import CarbonEstimationModel
from training.model_evaluation import (
    evaluate_model,
    compare_models,
    cross_validate_model,
    generate_evaluation_report
)
from sklearn.model_selection import train_test_split
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def example_1_evaluate_single_model():
    """
    Example 1: Evaluate a single trained model
    """
    logger.info("=" * 60)
    logger.info("EXAMPLE 1: Evaluate Single Model")
    logger.info("=" * 60)
    
    # Load trained model
    model_path = 'saved_models/carbon_random_forest_custom_20251111_111909'
    model = CarbonEstimationModel.load(model_path)
    
    logger.info(f"\n✓ Loaded model: {model.metadata['algorithm']}")
    logger.info(f"  Trained at: {model.metadata.get('trained_at', 'Unknown')}")
    logger.info(f"  Training samples: {model.metadata.get('n_samples', 'Unknown')}")
    
    # Load or generate test data
    # For this example, we'll simulate test data
    # In practice, you would use real held-out test data
    
    np.random.seed(42)
    n_test = 500
    
    # Simulate test features (16 features)
    X_test = np.random.rand(n_test, 16)
    
    # Simulate test target (carbon values)
    # Add some correlation with features for realism
    y_test = 50 + 30 * X_test[:, 10] + np.random.randn(n_test) * 10
    y_test = np.clip(y_test, 0, 200)  # Clip to realistic range
    
    logger.info(f"\n✓ Test data prepared:")
    logger.info(f"  Samples: {len(X_test)}")
    logger.info(f"  Features: {X_test.shape[1]}")
    logger.info(f"  Target range: {y_test.min():.2f} - {y_test.max():.2f} Mg/ha")
    
    # Evaluate model
    results = evaluate_model(
        model=model,
        X_test=X_test,
        y_test=y_test,
        feature_names=model.feature_names,
        save_plots=True,
        output_dir='evaluation_results/example_1'
    )
    
    # Generate text report
    generate_evaluation_report(
        results,
        output_file='evaluation_results/example_1/report.txt'
    )
    
    logger.info("\n✓ Evaluation complete!")
    logger.info(f"  Results saved to: evaluation_results/example_1/")
    
    return results


def example_2_compare_multiple_models():
    """
    Example 2: Compare multiple trained models
    """
    logger.info("\n" + "=" * 60)
    logger.info("EXAMPLE 2: Compare Multiple Models")
    logger.info("=" * 60)
    
    # Load multiple models
    models = {
        'Random Forest': CarbonEstimationModel.load('saved_models/carbon_random_forest_custom_20251111_111909'),
        'Linear Regression': CarbonEstimationModel.load('saved_models/carbon_linear_centraljava_v1'),
        'Ridge Regression': CarbonEstimationModel.load('saved_models/carbon_ridge_centraljava_v1')
    }
    
    logger.info(f"\n✓ Loaded {len(models)} models for comparison")
    
    # Generate test data
    np.random.seed(42)
    n_test = 500
    X_test = np.random.rand(n_test, 16)
    y_test = 50 + 30 * X_test[:, 10] + np.random.randn(n_test) * 10
    y_test = np.clip(y_test, 0, 200)
    
    # Compare models
    comparison_results = compare_models(
        models_dict=models,
        X_test=X_test,
        y_test=y_test
    )
    
    # Display comparison table
    logger.info("\n" + "=" * 60)
    logger.info("MODEL COMPARISON SUMMARY")
    logger.info("=" * 60)
    
    print("\n{:<25} {:>10} {:>10} {:>10}".format(
        "Model", "RMSE", "MAE", "R²"
    ))
    print("-" * 60)
    
    for model_name, metrics in comparison_results['comparison'].items():
        print("{:<25} {:>10.2f} {:>10.2f} {:>10.4f}".format(
            model_name,
            metrics['rmse'],
            metrics['mae'],
            metrics['r2']
        ))
    
    print("\n" + "=" * 60)
    print(f"🏆 BEST MODEL: {comparison_results['best_model']['name']}")
    print(f"   RMSE: {comparison_results['best_model']['metrics']['rmse']:.2f} Mg/ha")
    print(f"   R²: {comparison_results['best_model']['metrics']['r2']:.4f}")
    print("=" * 60)
    
    return comparison_results


def example_3_cross_validation_existing_model():
    """
    Example 3: Perform cross-validation on existing model with new data
    """
    logger.info("\n" + "=" * 60)
    logger.info("EXAMPLE 3: Cross-Validation on New Data")
    logger.info("=" * 60)
    
    # Load model
    model = CarbonEstimationModel.load('saved_models/carbon_random_forest_custom_20251111_111909')
    
    # Generate validation data
    np.random.seed(123)
    n_val = 2000
    X_val = np.random.rand(n_val, 16)
    y_val = 50 + 30 * X_val[:, 10] + np.random.randn(n_val) * 10
    y_val = np.clip(y_val, 0, 200)
    
    logger.info(f"\n✓ Validation data prepared: {len(X_val)} samples")
    
    # Perform cross-validation
    cv_results = cross_validate_model(
        model=model.model,  # sklearn model
        X=X_val,
        y=y_val,
        cv=5
    )
    
    logger.info("\n✓ Cross-validation complete!")
    
    return cv_results


if __name__ == '__main__':
    # Run examples
    
    print("\n" + "🚀" * 30)
    print("MODEL EVALUATION EXAMPLES")
    print("🚀" * 30 + "\n")
    
    # Example 1: Single model evaluation
    results_1 = example_1_evaluate_single_model()
    
    # Example 2: Compare multiple models
    # results_2 = example_2_compare_multiple_models()
    
    # Example 3: Cross-validation
    # results_3 = example_3_cross_validation_existing_model()
    
    print("\n" + "✅" * 30)
    print("ALL EXAMPLES COMPLETED!")
    print("✅" * 30)