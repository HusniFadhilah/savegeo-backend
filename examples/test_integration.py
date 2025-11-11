"""
Integration test for full workflow
"""

import requests
import json
import time

API_BASE = "http://localhost:5000/api"

def test_full_workflow():
    """Test complete workflow from model selection to inference"""
    
    print("\n" + "=" * 60)
    print("INTEGRATION TEST: Full Carbon Analysis Workflow")
    print("=" * 60)
    
    # Step 1: Check API health
    print("\n1. Checking API health...")
    response = requests.get(f"{API_BASE}/health")
    assert response.status_code == 200
    health = response.json()
    print(f"   Status: {health['status']}")
    print(f"   EE Initialized: {health['ee_initialized']}")
    
    # Step 2: List available models
    print("\n2. Listing available models...")
    response = requests.get(f"{API_BASE}/models/list")
    assert response.status_code == 200
    models_data = response.json()
    models = models_data['models']
    print(f"   Found {len(models)} models")
    
    if not models:
        print("   ERROR: No models available")
        return False
    
    # Step 3: Get info for first model
    model_name = models[0]['name']
    print(f"\n3. Getting info for model: {model_name}")
    response = requests.get(f"{API_BASE}/models/info/{model_name}")
    assert response.status_code == 200
    model_info = response.json()
    print(f"   Algorithm: {model_info['algorithm']}")
    print(f"   Samples: {model_info['n_samples']}")
    
    # Step 4: Run inference
    print(f"\n4. Running inference with model: {model_name}")
    
    inference_params = {
        "aoi": {
            "west": 110.3,
            "south": -7.0,
            "east": 110.5,
            "north": -6.8
        },
        "year": 2023,
        "start_month": 1,
        "end_month": 3,
        "cloud_threshold": 20,
        "clip_to_aoi": True,
        "model_name": model_name
    }
    
    start_time = time.time()
    response = requests.post(
        f"{API_BASE}/analyze/carbon",
        json=inference_params,
        timeout=300
    )
    duration = time.time() - start_time
    
    assert response.status_code == 200
    result = response.json()
    
    print(f"   ✓ Inference completed in {duration:.2f}s")
    
    # Step 5: Validate results
    print("\n5. Validating results...")
    
    assert 'carbon_estimated' in result
    assert 'tile_url' in result['carbon_estimated']
    assert 'statistics' in result['carbon_estimated']
    assert 'area_info' in result
    assert 'model_info' in result
    
    stats = result['carbon_estimated']['statistics']
    print(f"   Mean Carbon: {stats['mean']:.2f} Mg/ha")
    print(f"   Total Carbon: {result['area_info']['total_carbon_tons']:.2f} tons")
    
    print("\n" + "=" * 60)
    print("✓ ALL INTEGRATION TESTS PASSED")
    print("=" * 60)
    
    return True


if __name__ == "__main__":
    try:
        success = test_full_workflow()
        exit(0 if success else 1)
    except Exception as e:
        print(f"\n✗ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        exit(1)