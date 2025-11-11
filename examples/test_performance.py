"""
Performance test for inference
"""

import time
import requests
import json

API_BASE = "http://localhost:5000/api"

def test_inference_performance():
    """Test inference speed for different area sizes"""
    
    # Different area sizes (in degrees)
    test_areas = [
        ("Small", 0.1, 0.1),    # ~10km x 10km
        ("Medium", 0.2, 0.2),   # ~20km x 20km
        ("Large", 0.5, 0.5),    # ~50km x 50km
    ]
    
    results = []
    
    for name, width, height in test_areas:
        print(f"\nTesting {name} area ({width}° x {height}°)...")
        
        params = {
            "aoi": {
                "west": 110.3,
                "south": -7.0,
                "east": 110.3 + width,
                "north": -7.0 + height
            },
            "year": 2023,
            "start_month": 1,
            "end_month": 3,
            "cloud_threshold": 20,
            "clip_to_aoi": True
        }
        
        start = time.time()
        try:
            response = requests.post(
                f"{API_BASE}/analyze/carbon",
                json=params,
                timeout=600
            )
            duration = time.time() - start
            
            if response.status_code == 200:
                result = response.json()
                area_ha = result['area_info']['calculation_area_ha']
                
                results.append({
                    'name': name,
                    'duration': duration,
                    'area_ha': area_ha,
                    'mean_carbon': result['carbon_estimated']['statistics']['mean']
                })
                
                print(f"  ✓ Completed in {duration:.2f}s")
                print(f"    Area: {area_ha:.2f} ha")
            else:
                print(f"  ✗ Failed: {response.status_code}")
                
        except Exception as e:
            print(f"  ✗ Error: {e}")
    
    # Summary
    print("\n" + "=" * 60)
    print("PERFORMANCE SUMMARY")
    print("=" * 60)
    
    for r in results:
        print(f"\n{r['name']}:")
        print(f"  Duration: {r['duration']:.2f}s")
        print(f"  Area: {r['area_ha']:.2f} ha")
        print(f"  Speed: {r['area_ha']/r['duration']:.2f} ha/s")


if __name__ == "__main__":
    test_inference_performance()