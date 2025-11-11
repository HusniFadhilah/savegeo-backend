"""
Example: Complete workflow from training to inference
File: examples/complete_workflow.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import ee
import logging
from models.carbon_model import CarbonEstimationModel
from models.model_registry import ModelRegistry
from training.data_preparation import sample_training_data
from training.model_evaluation import evaluate_model, generate_evaluation_report
from inference.carbon_inference import CarbonInferenceEngine
import numpy as np

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def complete_carbon_workflow():
    """
    Complete workflow: Sample → Train → Evaluate → Inference
    """
    
    logger.info("🚀" * 30)
    logger.info("COMPLETE CARBON ESTIMATION WORKFLOW")
    logger.info("🚀" * 30)
    
    # Initialize Earth Engine
    ee.Initialize()
    logger.info("\n✓ Earth Engine initialized")
    
    # ========================================
    # STEP 1: Sample Training Data
    # ========================================
    logger.info("\n" + "=" * 60)
    logger.info("STEP 1: SAMPLE TRAINING DATA")
    logger.info("=" * 60)
    
    roi_train = ee.Geometry.Rectangle([109.5, -8.0, 111.5, -6.5])
    
    training_data = sample_training_data(
        roi=roi_train,
        reference_dataset='WCMC',
        n_samples=5000,
        year=2022,
        month_range=(6, 9),
        use_mosaic=True
    )
    
    X = training_data['features']
    y = training_data['target']
    feature_names = training_data['feature_names']
    
    logger.info(f"\n✓ Training data ready:")
    logger.info(f"  Samples: {len(X)}")
    logger.info(f"  Features: {len(feature_names)}")
    
    # Train/test split
    from sklearn.model_selection import train_test_split
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42
    )
    
    # ========================================
    # STEP 2: Train Model
    # ========================================
    logger.info("\n" + "=" * 60)
    logger.info("STEP 2: TRAIN MODEL")
    logger.info("=" * 60)
    
    model = CarbonEstimationModel(
        algorithm='random_forest',
        n_estimators=100,
        max_depth=20,
        random_state=42,
        n_jobs=-1
    )
    
    training_results = model.train(
        X=X_train,
        y=y_train,
        feature_names=feature_names,
        cv_folds=5
    )
    
    logger.info("\n✓ Training complete:")
    logger.info(f"  CV RMSE: {training_results['cv_metrics']['rmse_mean']:.2f} Mg/ha")
    logger.info(f"  CV R²: {training_results['cv_metrics']['r2_mean']:.4f}")
    
    # ========================================
    # STEP 3: Evaluate Model
    # ========================================
    logger.info("\n" + "=" * 60)
    logger.info("STEP 3: EVALUATE MODEL")
    logger.info("=" * 60)
    
    eval_results = evaluate_model(
        model=model,
        X_test=X_test,
        y_test=y_test,
        feature_names=feature_names,
        save_plots=True,
        output_dir='evaluation_results/complete_workflow'
    )
    
    generate_evaluation_report(
        eval_results,
        output_file='evaluation_results/complete_workflow/report.txt'
    )
    
    logger.info("\n✓ Evaluation complete")
    
    # ========================================
    # STEP 4: Save & Register Model
    # ========================================
    logger.info("\n" + "=" * 60)
    logger.info("STEP 4: SAVE & REGISTER MODEL")
    logger.info("=" * 60)
    
    model_name = 'carbon_workflow_example'
    model_path = f'saved_models/{model_name}'
    
    model.save(model_path)
    
    registry = ModelRegistry()
    registry.register_model(
        model_name=model_name,
        model_path=model_path,
        metadata=model.get_info(),
        set_as_default=True
    )
    
    logger.info(f"\n✓ Model saved and registered: {model_name}")
    
    # ========================================
    # STEP 5: Run Inference
    # ========================================
    logger.info("\n" + "=" * 60)
    logger.info("STEP 5: RUN INFERENCE")
    logger.info("=" * 60)
    
    # Use different area for inference
    roi_inference = ee.Geometry.Rectangle([110.3, -7.8, 110.5, -7.6])
    
    engine = CarbonInferenceEngine(model_name=model_name)
    
    carbon_map = engine.predict_for_region(
        roi=roi_inference,
        year=2023,
        start_month=1,
        end_month=12,
        cloud_threshold=20
    )
    
    # Calculate statistics
    stats = carbon_map.reduceRegion(
        reducer=ee.Reducer.mean()
            .combine(ee.Reducer.stdDev(), '', True)
            .combine(ee.Reducer.min(), '', True)
            .combine(ee.Reducer.max(), '', True),
        geometry=roi_inference,
        scale=250,
        maxPixels=1e8
    ).getInfo()
    
    area_ha = roi_inference.area().divide(10000).getInfo()
    mean_carbon = stats.get('carbon_estimated_mean', 0)
    total_carbon = mean_carbon * area_ha
    
    logger.info("\n--- Inference Results ---")
    logger.info(f"Area: {area_ha:.2f} ha")
    logger.info(f"Mean Carbon: {mean_carbon:.2f} Mg/ha")
    logger.info(f"Total Carbon: {total_carbon:.2f} tons")
    logger.info(f"CO₂ Equivalent: {total_carbon * 3.67:.2f} tons CO₂e")
    
    # ========================================
    # SUMMARY
    # ========================================
    logger.info("\n" + "=" * 60)
    logger.info("WORKFLOW SUMMARY")
    logger.info("=" * 60)
    
    summary = f"""
Training:
  - Samples: {len(X_train)}
  - CV RMSE: {training_results['cv_metrics']['rmse_mean']:.2f} Mg/ha
  - CV R²: {training_results['cv_metrics']['r2_mean']:.4f}

Evaluation:
  - Test RMSE: {eval_results['metrics']['rmse']:.2f} Mg/ha
  - Test R²: {eval_results['metrics']['r2']:.4f}

Inference:
  - Area: {area_ha:.2f} ha
  - Mean Carbon: {mean_carbon:.2f} Mg/ha
  - Total Carbon: {total_carbon:.2f} tons

Model: {model_name}
Status: Ready for production use
    """
    
    print(summary)
    
    logger.info("\n✅ WORKFLOW COMPLETED SUCCESSFULLY!")
    
    return {
        'training_results': training_results,
        'eval_results': eval_results,
        'inference_stats': stats,
        'model_name': model_name
    }


if __name__ == '__main__':
    result = complete_carbon_workflow()