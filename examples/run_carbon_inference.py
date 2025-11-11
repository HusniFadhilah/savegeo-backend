"""
Example: Use pre-trained model for carbon inference
File: examples/run_carbon_inference.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import ee
from inference.carbon_inference import CarbonInferenceEngine
from models.model_registry import ModelRegistry
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def example_1_simple_inference():
    """
    Example 1: Simple inference on a specific area
    """
    logger.info("=" * 60)
    logger.info("EXAMPLE 1: Simple Carbon Inference")
    logger.info("=" * 60)
    
    # Initialize Earth Engine
    try:
        ee.Initialize()
        logger.info("✓ Earth Engine initialized")
    except Exception as e:
        logger.error(f"Failed to initialize Earth Engine: {e}")
        return
    
    # Define area of interest (Bogor, West Java)
    roi = ee.Geometry.Rectangle([106.7, -6.7, 107.0, -6.4])
    
    logger.info("\n--- Loading Pre-trained Model ---")
    
    # Initialize inference engine (loads default model)
    engine = CarbonInferenceEngine()
    
    # Get model info
    model_info = engine.get_model_info()
    logger.info(f"✓ Model loaded:")
    logger.info(f"  Name: {engine.model_name}")
    logger.info(f"  Algorithm: {model_info['algorithm']}")
    logger.info(f"  CV RMSE: {model_info.get('cv_metrics', {}).get('rmse_mean', 'N/A')}")
    
    # Run inference
    logger.info("\n--- Running Inference ---")
    logger.info("  Region: Bogor, West Java")
    logger.info("  Year: 2023")
    logger.info("  Period: Jan-Dec")
    
    try:
        carbon_map = engine.predict_for_region(
            roi=roi,
            year=2023,
            start_month=1,
            end_month=12,
            cloud_threshold=20
        )
        
        logger.info("✓ Inference complete!")
        
        # Calculate statistics
        logger.info("\n--- Calculating Statistics ---")
        stats = carbon_map.reduceRegion(
            reducer=ee.Reducer.mean()
                .combine(ee.Reducer.stdDev(), '', True)
                .combine(ee.Reducer.min(), '', True)
                .combine(ee.Reducer.max(), '', True),
            geometry=roi,
            scale=250,
            maxPixels=1e8
        ).getInfo()
        
        area_ha = roi.area().divide(10000).getInfo()
        mean_carbon = stats.get('carbon_estimated_mean', 0)
        total_carbon = mean_carbon * area_ha
        
        logger.info("\n--- Results ---")
        logger.info(f"Area: {area_ha:.2f} ha")
        logger.info(f"Mean Carbon Density: {mean_carbon:.2f} Mg/ha")
        logger.info(f"Std Dev: {stats.get('carbon_estimated_stdDev', 0):.2f} Mg/ha")
        logger.info(f"Min: {stats.get('carbon_estimated_min', 0):.2f} Mg/ha")
        logger.info(f"Max: {stats.get('carbon_estimated_max', 0):.2f} Mg/ha")
        logger.info(f"Total Carbon Stock: {total_carbon:.2f} tons")
        logger.info(f"CO₂ Equivalent: {total_carbon * 3.67:.2f} tons CO₂e")
        
        # Generate tile URL for visualization
        logger.info("\n--- Generating Visualization ---")
        vis_params = {
            'min': 0,
            'max': 200,
            'palette': ['440154', '414487', '2a788e', '22a884', '7ad151', 'fde725']
        }
        
        carbon_rgb = carbon_map.visualize(**vis_params)
        map_id = carbon_rgb.getMapId()
        tile_url = map_id['tile_fetcher'].url_format
        
        logger.info(f"✓ Tile URL: {tile_url}")
        logger.info("\n✓ Use this URL in Leaflet/OpenLayers to display the map")
        
        return {
            'carbon_map': carbon_map,
            'statistics': stats,
            'tile_url': tile_url,
            'area_ha': area_ha,
            'total_carbon_tons': total_carbon
        }
        
    except Exception as e:
        logger.error(f"Inference failed: {e}")
        import traceback
        traceback.print_exc()
        return None


def example_2_compare_multiple_models():
    """
    Example 2: Compare predictions from different models
    """
    logger.info("\n" + "=" * 60)
    logger.info("EXAMPLE 2: Compare Multiple Model Predictions")
    logger.info("=" * 60)
    
    # Initialize Earth Engine
    ee.Initialize()
    
    # Define small test area
    roi = ee.Geometry.Rectangle([110.3, -7.8, 110.5, -7.6])
    
    # Load model registry
    registry = ModelRegistry()
    available_models = registry.list_models()
    
    logger.info(f"\n✓ Found {len(available_models)} trained models")
    
    results = {}
    
    for model_info in available_models[:3]:  # Test first 3 models
        model_name = model_info['name']
        logger.info(f"\n--- Testing Model: {model_name} ---")
        
        try:
            # Initialize engine with specific model
            engine = CarbonInferenceEngine(model_name=model_name)
            
            # Run inference
            carbon_map = engine.predict_for_region(
                roi=roi,
                year=2023,
                start_month=6,
                end_month=9,
                cloud_threshold=20
            )
            
            # Calculate mean
            stats = carbon_map.reduceRegion(
                reducer=ee.Reducer.mean(),
                geometry=roi,
                scale=250,
                maxPixels=1e8
            ).getInfo()
            
            mean_carbon = stats.get('carbon_estimated_mean', 0)
            
            results[model_name] = {
                'mean_carbon': mean_carbon,
                'algorithm': engine.model.algorithm,
                'cv_rmse': engine.model.metadata.get('cv_metrics', {}).get('rmse_mean', 'N/A')
            }
            
            logger.info(f"✓ Mean Carbon: {mean_carbon:.2f} Mg/ha")
            
        except Exception as e:
            logger.error(f"Failed for {model_name}: {e}")
            continue
    
    # Display comparison
    logger.info("\n" + "=" * 60)
    logger.info("PREDICTION COMPARISON")
    logger.info("=" * 60)
    
    print("\n{:<35} {:>15} {:>15}".format(
        "Model", "Mean Carbon", "CV RMSE"
    ))
    print("-" * 70)
    
    for model_name, data in results.items():
        print("{:<35} {:>12.2f} Mg/ha {:>12} Mg/ha".format(
            model_name[:35],
            data['mean_carbon'],
            f"{data['cv_rmse']:.2f}" if isinstance(data['cv_rmse'], float) else data['cv_rmse']
        ))
    
    return results


def example_3_seasonal_analysis():
    """
    Example 3: Compare carbon estimates across different seasons
    """
    logger.info("\n" + "=" * 60)
    logger.info("EXAMPLE 3: Seasonal Carbon Analysis")
    logger.info("=" * 60)
    
    # Initialize
    ee.Initialize()
    engine = CarbonInferenceEngine()
    
    # Define area
    roi = ee.Geometry.Rectangle([110.4, -7.8, 110.6, -7.6])
    
    # Define seasons
    seasons = {
        'Dry Season (Jun-Sep)': (6, 9),
        'Wet Season (Dec-Feb)': (12, 2),
        'Transition (Mar-May)': (3, 5)
    }
    
    results = {}
    
    for season_name, (start_month, end_month) in seasons.items():
        logger.info(f"\n--- {season_name} ---")
        
        try:
            # Handle year wrap-around for Dec-Feb
            if start_month > end_month:
                # Dec-Feb spans two years, use Dec only for simplicity
                year = 2022
                start_month = 12
                end_month = 12
            else:
                year = 2023
            
            carbon_map = engine.predict_for_region(
                roi=roi,
                year=year,
                start_month=start_month,
                end_month=end_month,
                cloud_threshold=20
            )
            
            stats = carbon_map.reduceRegion(
                reducer=ee.Reducer.mean(),
                geometry=roi,
                scale=250,
                maxPixels=1e8
            ).getInfo()
            
            mean_carbon = stats.get('carbon_estimated_mean', 0)
            results[season_name] = mean_carbon
            
            logger.info(f"✓ Mean Carbon: {mean_carbon:.2f} Mg/ha")
            
        except Exception as e:
            logger.error(f"Failed for {season_name}: {e}")
            continue
    
    # Display seasonal comparison
    logger.info("\n" + "=" * 60)
    logger.info("SEASONAL COMPARISON")
    logger.info("=" * 60)
    
    for season, carbon in results.items():
        print(f"{season:<30} {carbon:>10.2f} Mg/ha")
    
    return results


def example_4_inference_with_specific_model():
    """
    Example 4: Use specific model version for inference
    """
    logger.info("\n" + "=" * 60)
    logger.info("EXAMPLE 4: Inference with Specific Model")
    logger.info("=" * 60)
    
    # Initialize
    ee.Initialize()
    
    # Specify model name
    model_name = 'carbon_random_forest_custom_20251111_111909'
    
    logger.info(f"\n--- Loading Specific Model: {model_name} ---")
    
    try:
        # Load specific model
        engine = CarbonInferenceEngine(model_name=model_name)
        
        logger.info("✓ Model loaded successfully")
        
        # Define area
        roi = ee.Geometry.Point([110.4, -7.7]).buffer(5000)  # 5km radius
        
        # Run inference
        carbon_map = engine.predict_for_region(
            roi=roi,
            year=2023,
            start_month=1,
            end_month=12,
            cloud_threshold=15
        )
        
        # Get statistics
        stats = carbon_map.reduceRegion(
            reducer=ee.Reducer.mean()
                .combine(ee.Reducer.count(), '', True),
            geometry=roi,
            scale=250,
            maxPixels=1e8
        ).getInfo()
        
        logger.info("\n--- Results ---")
        logger.info(f"Mean Carbon: {stats.get('carbon_estimated_mean', 0):.2f} Mg/ha")
        logger.info(f"Pixel Count: {stats.get('carbon_estimated_count', 0)}")
        
        return stats
        
    except ValueError as e:
        logger.error(f"Model not found: {e}")
        logger.info("\nAvailable models:")
        
        registry = ModelRegistry()
        for model in registry.list_models():
            logger.info(f"  - {model['name']}")
        
        return None


if __name__ == '__main__':
    print("\n" + "🌍" * 30)
    print("CARBON INFERENCE EXAMPLES")
    print("🌍" * 30 + "\n")
    
    # Example 1: Simple inference
    result_1 = example_1_simple_inference()
    
    # Example 2: Compare models
    # result_2 = example_2_compare_multiple_models()
    
    # Example 3: Seasonal analysis
    # result_3 = example_3_seasonal_analysis()
    
    # Example 4: Specific model
    # result_4 = example_4_inference_with_specific_model()
    
    print("\n" + "✅" * 30)
    print("ALL INFERENCE EXAMPLES COMPLETED!")
    print("✅" * 30)