"""
Test script for carbon inference engine
"""

import ee
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.carbon_inference import CarbonInferenceEngine
from models.model_registry import ModelRegistry
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def test_list_models():
    """Test listing available models"""
    print("\n" + "=" * 60)
    print("TEST 1: List Available Models")
    print("=" * 60)
    
    registry = ModelRegistry()
    models = registry.list_models()
    
    print(f"\nFound {len(models)} models:")
    for model in models:
        print(f"\n  Name: {model['name']}")
        print(f"  Algorithm: {model['algorithm']}")
        print(f"  Trained: {model['registered_at']}")
        if model.get('cv_metrics'):
            print(f"  R²: {model['cv_metrics'].get('r2_mean', 'N/A')}")
            print(f"  RMSE: {model['cv_metrics'].get('rmse_mean', 'N/A')}")
    
    return models


def test_model_info(model_name: str):
    """Test getting model info"""
    print("\n" + "=" * 60)
    print(f"TEST 2: Get Model Info - {model_name}")
    print("=" * 60)
    
    registry = ModelRegistry()
    try:
        info = registry.get_model_info(model_name)
        
        print(f"\nModel Information:")
        print(f"  Name: {info['name']}")
        print(f"  Algorithm: {info['algorithm']}")
        print(f"  Samples: {info['n_samples']}")
        print(f"  Features: {info['n_features']}")
        
        if info.get('cv_metrics'):
            print(f"\nCross-Validation Metrics:")
            print(f"  Mean R²: {info['cv_metrics']['r2_mean']:.4f}")
            print(f"  Mean RMSE: {info['cv_metrics']['rmse_mean']:.2f} Mg/ha")
        
        if info.get('feature_importance'):
            print(f"\nTop 5 Features:")
            sorted_features = sorted(
                info['feature_importance'].items(), 
                key=lambda x: x[1], 
                reverse=True
            )[:5]
            for feat, imp in sorted_features:
                print(f"    {feat}: {imp*100:.1f}%")
        
        return info
        
    except Exception as e:
        print(f"ERROR: {e}")
        return None


def test_inference_small_area(model_name: str = None):
    """Test inference on a small area"""
    print("\n" + "=" * 60)
    print("TEST 3: Run Inference (Small Area)")
    print("=" * 60)
    
    # Initialize EE (make sure you're authenticated)
    try:
        ee.Initialize()
        print("✓ Earth Engine initialized")
    except Exception as e:
        print(f"ERROR: Failed to initialize Earth Engine: {e}")
        print("Run: earthengine authenticate")
        return None
    
    # Define small test area (e.g., around Semarang, Indonesia)
    test_roi = ee.Geometry.Rectangle([110.3, -7.0, 110.5, -6.8])
    
    print(f"\nTest Parameters:")
    print(f"  Model: {model_name or 'default'}")
    print(f"  Location: Semarang area")
    print(f"  Year: 2023")
    print(f"  Months: 1-3")
    print(f"  Cloud threshold: 20%")
    
    try:
        # Initialize inference engine
        print("\n--- Loading Model ---")
        engine = CarbonInferenceEngine(model_name=model_name)
        
        model_info = engine.get_model_info()
        print(f"✓ Loaded: {model_info['algorithm']}")
        
        # Run inference
        print("\n--- Running Inference ---")
        carbon_image = engine.predict_for_region(
            roi=test_roi,
            year=2023,
            start_month=1,
            end_month=3,
            cloud_threshold=20
        )
        
        print("✓ Inference complete")
        
        # Calculate statistics
        print("\n--- Calculating Statistics ---")
        stats = carbon_image.reduceRegion(
            reducer=ee.Reducer.mean()
                .combine(ee.Reducer.stdDev(), '', True)
                .combine(ee.Reducer.min(), '', True)
                .combine(ee.Reducer.max(), '', True),
            geometry=test_roi,
            scale=250,
            maxPixels=1e10
        ).getInfo()
        
        print(f"\nCarbon Statistics:")
        print(f"  Mean: {stats.get('carbon_estimated_mean', 0):.2f} Mg/ha")
        print(f"  Std Dev: {stats.get('carbon_estimated_stdDev', 0):.2f} Mg/ha")
        print(f"  Min: {stats.get('carbon_estimated_min', 0):.2f} Mg/ha")
        print(f"  Max: {stats.get('carbon_estimated_max', 0):.2f} Mg/ha")
        
        # Calculate total carbon
        area_ha = test_roi.area().divide(10000).getInfo()
        mean_carbon = stats.get('carbon_estimated_mean', 0)
        total_carbon = mean_carbon * area_ha
        
        print(f"\nTotal Carbon Stock:")
        print(f"  Area: {area_ha:.2f} hectares")
        print(f"  Total: {total_carbon:.2f} tons")
        print(f"  CO₂ equivalent: {total_carbon * 3.67:.2f} tons CO₂e")
        
        return carbon_image, stats
        
    except Exception as e:
        print(f"\nERROR during inference: {e}")
        import traceback
        traceback.print_exc()
        return None


def test_inference_with_visualization(model_name: str = None):
    """Test inference and generate visualization URL"""
    print("\n" + "=" * 60)
    print("TEST 4: Generate Visualization")
    print("=" * 60)
    
    # Initialize EE
    try:
        ee.Initialize()
    except:
        print("ERROR: Earth Engine not initialized")
        return None
    
    test_roi = ee.Geometry.Rectangle([110.3, -7.0, 110.5, -6.8])
    
    try:
        engine = CarbonInferenceEngine(model_name=model_name)
        
        carbon_image = engine.predict_for_region(
            roi=test_roi,
            year=2023,
            start_month=1,
            end_month=3,
            cloud_threshold=20
        )
        
        # Generate tile URL
        vis_params = {
            'min': 0,
            'max': 200,
            'palette': ['440154', '414487', '2a788e', '22a884', '7ad151', 'fde725']
        }
        
        carbon_rgb = carbon_image.visualize(**vis_params)
        map_id = carbon_rgb.getMapId()
        tile_url = map_id['tile_fetcher'].url_format
        
        print(f"\n✓ Tile URL generated:")
        print(f"  {tile_url}")
        print(f"\n  You can view this in your frontend by adding it as a tile layer")
        
        return tile_url
        
    except Exception as e:
        print(f"ERROR: {e}")
        return None


def test_compare_models():
    """Compare predictions from different models"""
    print("\n" + "=" * 60)
    print("TEST 5: Compare Multiple Models")
    print("=" * 60)
    
    registry = ModelRegistry()
    models = registry.list_models()
    
    if len(models) < 2:
        print("Need at least 2 models for comparison")
        return
    
    # Initialize EE
    try:
        ee.Initialize()
    except:
        print("ERROR: Earth Engine not initialized")
        return
    
    test_roi = ee.Geometry.Rectangle([110.3, -7.0, 110.5, -6.8])
    
    results = {}
    
    for model in models[:3]:  # Test first 3 models
        model_name = model['name']
        print(f"\n--- Testing: {model_name} ---")
        
        try:
            engine = CarbonInferenceEngine(model_name=model_name)
            
            carbon_image = engine.predict_for_region(
                roi=test_roi,
                year=2023,
                start_month=1,
                end_month=3,
                cloud_threshold=20
            )
            
            stats = carbon_image.reduceRegion(
                reducer=ee.Reducer.mean(),
                geometry=test_roi,
                scale=250,
                maxPixels=1e10
            ).getInfo()
            
            mean_carbon = stats.get('carbon_estimated_mean', 0)
            results[model_name] = {
                'algorithm': model['algorithm'],
                'mean_carbon': mean_carbon
            }
            
            print(f"  Mean Carbon: {mean_carbon:.2f} Mg/ha")
            
        except Exception as e:
            print(f"  ERROR: {e}")
            results[model_name] = {'error': str(e)}
    
    print("\n" + "=" * 60)
    print("COMPARISON SUMMARY")
    print("=" * 60)
    
    for name, result in results.items():
        if 'error' in result:
            print(f"\n{name}: FAILED - {result['error']}")
        else:
            print(f"\n{name} ({result['algorithm']}):")
            print(f"  Mean Carbon: {result['mean_carbon']:.2f} Mg/ha")


def run_all_tests():
    """Run all tests"""
    print("\n" + "=" * 80)
    print(" " * 20 + "CARBON INFERENCE TEST SUITE")
    print("=" * 80)
    
    # Test 1: List models
    models = test_list_models()
    
    if not models:
        print("\nNo models available. Train a model first.")
        return
    
    # Get first model name
    model_name = models[0]['name']
    
    # Test 2: Model info
    test_model_info(model_name)
    
    # Test 3: Basic inference
    test_inference_small_area(model_name)
    
    # Test 4: Visualization
    test_inference_with_visualization(model_name)
    
    # Test 5: Compare models (if multiple available)
    if len(models) > 1:
        test_compare_models()
    
    print("\n" + "=" * 80)
    print(" " * 25 + "ALL TESTS COMPLETE")
    print("=" * 80)


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='Test carbon inference engine')
    parser.add_argument('--test', type=str, 
                       choices=['list', 'info', 'inference', 'viz', 'compare', 'all'],
                       default='all',
                       help='Test to run')
    parser.add_argument('--model', type=str, default=None,
                       help='Model name to test')
    
    args = parser.parse_args()
    
    if args.test == 'list':
        test_list_models()
    elif args.test == 'info':
        if not args.model:
            print("ERROR: --model required for info test")
        else:
            test_model_info(args.model)
    elif args.test == 'inference':
        test_inference_small_area(args.model)
    elif args.test == 'viz':
        test_inference_with_visualization(args.model)
    elif args.test == 'compare':
        test_compare_models()
    else:
        run_all_tests()