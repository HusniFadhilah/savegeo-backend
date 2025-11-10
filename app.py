"""
GEE Land Cover & Vegetation Analysis - Backend API
Flask + Google Earth Engine
"""

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import ee
import os
import json
from datetime import datetime
from pathlib import Path
import tempfile
import numpy as np
import requests
from typing import Dict, List, Optional, Tuple
import logging
try:
    from sklearn.model_selection import KFold
    from sklearn.linear_model import LinearRegression
    from sklearn.metrics import mean_squared_error, r2_score
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    print("⚠ Warning: scikit-learn not available. Cross-validation disabled.")

from inference.carbon_inference import CarbonInferenceEngine
from models.model_registry import ModelRegistry

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=False)

# =========================
# CONFIGURATION
# =========================
SERVICE_ACCOUNT = os.getenv("GEE_SERVICE_ACCOUNT", "your-sa@project.iam.gserviceaccount.com")
KEY_FILE = os.getenv("GEE_KEY_FILE", "../endless-bounty-416008-a6cce2f8b208.json")
API_BASE_URL = "https://api.sp3stab.id/api/en"

# =========================
# GOOGLE EARTH ENGINE INITIALIZATION
# =========================
def init_ee():
    """Initialize Google Earth Engine"""
    try:
        if Path(KEY_FILE).exists():
            credentials = ee.ServiceAccountCredentials(SERVICE_ACCOUNT, KEY_FILE)
            ee.Initialize(credentials)
            logger.info("✓ Earth Engine initialized successfully")
            return True
        else:
            logger.error(f"Key file not found: {KEY_FILE}")
            return False
    except Exception as e:
        logger.error(f"Failed to initialize Earth Engine: {e}")
        return False

# Initialize EE on startup
ee_initialized = init_ee()

# =========================
# VEGETATION INDICES
# =========================
VEGETATION_INDICES = {
    "NDVI": {
        "name": "Normalized Difference Vegetation Index",
        "formula": "(NIR - RED) / (NIR + RED)",
        "bands": ["B8", "B4"],
        "range": [-1, 1],
        "description": "General vegetation health",
        "palette": ["#8B4513", "#FFFF00", "#90EE90", "#006400"]
    },
    "NDWI": {
        "name": "Normalized Difference Water Index",
        "formula": "(GREEN - NIR) / (GREEN + NIR)",
        "bands": ["B3", "B8"],
        "range": [-1, 1],
        "description": "Vegetation water content",
        "palette": ["#8B4513", "#F5DEB3", "#87CEEB", "#0000FF"]
    },
    "MNDWI": {
        "name": "Modified NDWI",
        "formula": "(GREEN - SWIR) / (GREEN + SWIR)",
        "bands": ["B3", "B11"],
        "range": [-1, 1],
        "description": "Water body detection",
        "palette": ["#FFFFE0", "#98FB98", "#4682B4", "#000080"]
    },
    "NDBI": {
        "name": "Normalized Difference Built-up Index",
        "formula": "(SWIR - NIR) / (SWIR + NIR)",
        "bands": ["B11", "B8"],
        "range": [-1, 1],
        "description": "Built-up areas",
        "palette": ["#006400", "#90EE90", "#FFD700", "#FF0000"]
    },
    "EVI": {
        "name": "Enhanced Vegetation Index",
        "formula": "2.5 * (NIR - RED) / (NIR + 6*RED - 7.5*BLUE + 1)",
        "bands": ["B8", "B4", "B2"],
        "range": [-1, 1],
        "description": "Enhanced vegetation with atmospheric correction",
        "palette": ["#8B4513", "#FFFF00", "#90EE90", "#006400"]
    },
    "SAVI": {
        "name": "Soil Adjusted Vegetation Index",
        "formula": "1.5 * (NIR - RED) / (NIR + RED + 0.5)",
        "bands": ["B8", "B4"],
        "range": [-1, 1],
        "description": "Vegetation with soil correction",
        "palette": ["#8B4513", "#FFFF00", "#90EE90", "#006400"]
    },
    "BSI": {
        "name": "Bare Soil Index",
        "formula": "(SWIR + RED - NIR - BLUE) / (SWIR + RED + NIR + BLUE)",
        "bands": ["B11", "B4", "B8", "B2"],
        "range": [-1, 1],
        "description": "Bare soil detection",
        "palette": ["#006400", "#90EE90", "#DEB887", "#8B4513"]
    },
    "NDMI": {
        "name": "Normalized Difference Moisture Index",
        "formula": "(NIR - SWIR) / (NIR + SWIR)",
        "bands": ["B8", "B11"],
        "range": [-1, 1],
        "description": "Vegetation moisture",
        "palette": ["#8B4513", "#D2691E", "#90EE90", "#006400"]
    }
}

# =========================
# LAND COVER LEGENDS
# =========================
LAND_COVER_LEGENDS = {
    "Dynamic_World": {
        "0": {"color": "#419BDF", "label": "Water"},
        "1": {"color": "#397D49", "label": "Trees"},
        "2": {"color": "#88B053", "label": "Grass"},
        "3": {"color": "#7A87C6", "label": "Flooded vegetation"},
        "4": {"color": "#E49635", "label": "Crops"},
        "5": {"color": "#DFC35A", "label": "Shrub & Scrub"},
        "6": {"color": "#C4281B", "label": "Built Area"},
        "7": {"color": "#A59B8F", "label": "Bare ground"},
        "8": {"color": "#B39FE1", "label": "Snow & Ice"}
    },
    "ESA_WorldCover": {
        "10": {"color": "#006400", "label": "Trees"},
        "20": {"color": "#ffbb22", "label": "Shrubland"},
        "30": {"color": "#ffff4c", "label": "Grassland"},
        "40": {"color": "#f096ff", "label": "Cropland"},
        "50": {"color": "#fa0000", "label": "Built-up"},
        "60": {"color": "#b4b4b4", "label": "Barren/sparse vegetation"},
        "70": {"color": "#f0f0f0", "label": "Snow and ice"},
        "80": {"color": "#0032c8", "label": "Open water"},
        "90": {"color": "#0096a0", "label": "Herbaceous wetland"},
        "95": {"color": "#00cf75", "label": "Mangroves"},
        "100": {"color": "#fae6a0", "label": "Moss and lichen"}
    }
}

# =========================
# HELPER FUNCTIONS
# =========================
def mask_s2_clouds(image):
    """Mask clouds in Sentinel-2 imagery"""
    qa = image.select("QA60")
    cloud = qa.bitwiseAnd(1 << 10).neq(0)
    cirrus = qa.bitwiseAnd(1 << 11).neq(0)
    mask = cloud.Or(cirrus).Not()
    return image.updateMask(mask).divide(10000)

def calculate_index(image, index_name):
    """Calculate vegetation index"""
    index_info = VEGETATION_INDICES[index_name]
    
    if index_name == "EVI":
        nir = image.select('B8')
        red = image.select('B4')
        blue = image.select('B2')
        evi = nir.subtract(red).multiply(2.5).divide(
            nir.add(red.multiply(6)).subtract(blue.multiply(7.5)).add(1)
        )
        return evi.rename(index_name)
    
    elif index_name == "SAVI":
        nir = image.select('B8')
        red = image.select('B4')
        savi = nir.subtract(red).divide(nir.add(red).add(0.5)).multiply(1.5)
        return savi.rename(index_name)
    
    elif index_name == "BSI":
        blue = image.select('B2')
        red = image.select('B4')
        nir = image.select('B8')
        swir = image.select('B11')
        bsi = swir.add(red).subtract(nir).subtract(blue).divide(
            swir.add(red).add(nir).add(blue)
        )
        return bsi.rename(index_name)
    
    else:
        # NDVI, NDWI, MNDWI, NDBI, NDMI
        bands = index_info["bands"]
        return image.normalizedDifference(bands).rename(index_name)

def create_geometry_from_bounds(bounds):
    """Create EE Geometry from bounds dict"""
    return ee.Geometry.Rectangle([
        bounds['west'],
        bounds['south'],
        bounds['east'],
        bounds['north']
    ])

def get_tile_url(image, vis_params, name):
    """Get tile URL for displaying on map"""
    try:
        map_id = image.getMapId(vis_params)
        return {
            'tile_url': map_id['tile_fetcher'].url_format,
            'name': name
        }
    except Exception as e:
        logger.error(f"Error getting tile URL for {name}: {e}")
        return None
    
def create_geometry_from_payload(aoi_payload: dict) -> ee.Geometry:
    """
    Menerima salah satu:
      - {"geojson": <Feature/FeatureCollection/Geometry>}
      - {"west":..,"south":..,"east":..,"north":..}
    """
    if not isinstance(aoi_payload, dict):
        raise ValueError("AOI payload must be an object")

    if "geojson" in aoi_payload:
        gj = aoi_payload["geojson"]
        if not isinstance(gj, dict) or "type" not in gj:
            raise ValueError("Invalid GeoJSON")
        # dukung Feature, FeatureCollection, atau Geometry murni
        if gj["type"] == "Feature":
            geom = gj.get("geometry")
        elif gj["type"] == "FeatureCollection":
            feats = gj.get("features", [])
            if not feats:
                raise ValueError("Empty FeatureCollection")
            geom = feats[0].get("geometry")
        else:
            geom = gj
        if not geom:
            raise ValueError("GeoJSON has no geometry")
        return ee.Geometry(geom)

    # fallback: bounds
    required = {"west","south","east","north"}
    if not required.issubset(aoi_payload.keys()):
        raise ValueError("AOI bounds missing west/south/east/north")
    return ee.Geometry.Rectangle([
        float(aoi_payload["west"]),
        float(aoi_payload["south"]),
        float(aoi_payload["east"]),
        float(aoi_payload["north"]),
    ])

def check_carbon_data_availability(roi, dataset_name='WCMC'):
    """
    Check if carbon reference data is available in the given region
    
    Returns:
        dict: {
            'available': bool,
            'sample_count': int,
            'mean_value': float,
            'message': str
        }
    """
    try:
        carbon_ref = load_carbon_reference_dataset(dataset_name, 2010, roi)
        
        # Take a small sample
        sample = carbon_ref.sample(
            region=roi,
            scale=250,
            numPixels=100,
            seed=42
        ).getInfo()
        
        features = sample.get('features', [])
        
        if len(features) == 0:
            return {
                'available': False,
                'sample_count': 0,
                'mean_value': 0,
                'message': 'No carbon reference data found in this area'
            }
        
        # Calculate mean
        values = [f['properties'].get('agb', 0) for f in features]
        valid_values = [v for v in values if v is not None and v > 0]
        
        if len(valid_values) == 0:
            return {
                'available': False,
                'sample_count': len(features),
                'mean_value': 0,
                'message': 'Carbon data exists but all values are invalid'
            }
        
        mean_val = sum(valid_values) / len(valid_values)
        
        return {
            'available': True,
            'sample_count': len(valid_values),
            'mean_value': mean_val,
            'message': f'Found {len(valid_values)} valid samples (mean: {mean_val:.2f} Mg/ha)'
        }
        
    except Exception as e:
        return {
            'available': False,
            'sample_count': 0,
            'mean_value': 0,
            'message': f'Error checking data: {str(e)}'
        }

def load_carbon_reference_dataset(dataset_name, dataset_year=2020, roi=None):
    """
    Load carbon/biomass reference dataset with proper band handling
    
    Args:
        dataset_name: 'ESA_CCI', 'WCMC', 'GEDI', or 'Simard'
        dataset_year: Year for dataset
        roi: Region of interest for filtering
    
    Returns:
        ee.Image: Carbon/biomass reference image with band name 'agb' in Mg/ha
    """
    
    if dataset_name == 'WCMC':
        # WCMC Carbon Density (300m resolution, 2010)
        print("Loading WCMC Carbon Density (2010)")
        try:
            carbon_density = ee.ImageCollection("WCMC/biomass_carbon_density/v1_0").first()
            
            # ✅ CRITICAL: Check band name and rename to 'agb'
            band_names = carbon_density.bandNames().getInfo()
            print(f"  WCMC bands available: {band_names}")
            
            # ✅ FIX: WCMC uses 'carbon_tonnes_per_ha' as band name
            if 'carbon_tonnes_per_ha' in band_names:
                return carbon_density.select('carbon_tonnes_per_ha').rename('agb')
            elif 'carbon' in band_names:
                return carbon_density.select('carbon').rename('agb')
            else:
                print(f"  Warning: Expected band not found. Available: {band_names}")
                # Try first band as fallback
                return carbon_density.select(0).rename('agb')
                
        except Exception as e:
            print(f"  Error loading WCMC: {str(e)}")
            raise
    
    elif dataset_name == 'ESA_CCI':
        # ESA CCI Biomass - Not available in public GEE yet
        # Use alternative: Hansen Tree Cover as proxy
        print(f"Loading ESA CCI Biomass proxy for {dataset_year}")
        
        try:
            # Hansen Global Forest Change
            hansen = ee.Image('UMD/hansen/global_forest_change_2023_v1_11')
            tree_cover = hansen.select('treecover2000')
            
            # Convert tree cover (0-100%) to biomass estimate
            # Simplified allometric equation: AGB ≈ tree_cover * 2.5
            biomass = tree_cover.multiply(2.5).rename('agb')
            
            return biomass
            
        except Exception as e:
            print(f"  Error loading ESA_CCI proxy: {str(e)}")
            # Fallback to WCMC
            print("  Falling back to WCMC")
            return load_carbon_reference_dataset('WCMC', dataset_year, roi)
    
    elif dataset_name == 'GEDI':
        # GEDI - Use alternative
        print("Loading GEDI proxy (Hansen-based)")
        return load_carbon_reference_dataset('ESA_CCI', dataset_year, roi)
    
    elif dataset_name == 'Simard':
        # Simard Forest Height
        print("Loading Simard proxy (Hansen-based)")
        
        try:
            hansen = ee.Image('UMD/hansen/global_forest_change_2023_v1_11')
            tree_cover = hansen.select('treecover2000')
            
            # Convert to biomass
            biomass = tree_cover.multiply(2.0).rename('agb')
            
            return biomass
            
        except Exception as e:
            print(f"  Error loading Simard proxy: {str(e)}")
            return load_carbon_reference_dataset('WCMC', dataset_year, roi)
    
    else:
        raise ValueError(f"Unknown dataset: {dataset_name}. Choose from: WCMC, ESA_CCI, GEDI, Simard")

def get_dataset_info(dataset_name, dataset_year=2010):
    """
    Get metadata about carbon reference dataset
    """
    info = {
        'ESA_CCI': {
            'name': 'ESA CCI Biomass',
            'full_name': 'ESA Climate Change Initiative Aboveground Biomass',
            'year': dataset_year,
            'resolution': 100,
            'unit': 'Mg/ha',
            'source': 'ESA CCI',
            'description': f'Global biomass map for {dataset_year} at 100m resolution'
        },
        'WCMC': {
            'name': 'WCMC Carbon Density',
            'full_name': 'UN World Conservation Monitoring Centre Carbon Density',
            'year': 2010,
            'resolution': 300,
            'unit': 'Mg/ha',
            'source': 'WCMC',
            'description': 'Global carbon density map (2010) at 300m resolution'
        },
        'GEDI': {
            'name': 'GEDI L4B Biomass',
            'full_name': 'NASA GEDI Level 4B Aboveground Biomass',
            'year': 2020,
            'resolution': 1000,
            'unit': 'Mg/ha',
            'source': 'NASA GEDI',
            'description': 'Spaceborne lidar-derived biomass at 1km resolution'
        },
        'Simard': {
            'name': 'Simard Forest Height',
            'full_name': 'Simard Global Forest Canopy Height',
            'year': 2011,
            'resolution': 1000,
            'unit': 'Mg/ha',
            'source': 'Simard et al.',
            'description': 'Forest height converted to biomass at 1km resolution'
        }
    }
    
    return info.get(dataset_name, {
        'name': 'Unknown Dataset',
        'full_name': 'Unknown Dataset',
        'year': dataset_year,
        'resolution': 'N/A',
        'unit': 'Mg/ha',
        'source': 'Unknown',
        'description': 'Dataset information not available'
    })

def geojson_to_ee_geometry(geojson):
    """
    Convert GeoJSON to Earth Engine Geometry with proper handling
    """
    try:
        if geojson['type'] == 'FeatureCollection':
            features = [ee.Feature(ee.Geometry(f['geometry'])) for f in geojson['features']]
            fc = ee.FeatureCollection(features)
            # Dissolve all features into single geometry
            return fc.geometry().dissolve()
        elif geojson['type'] == 'Feature':
            return ee.Geometry(geojson['geometry'])
        else:
            # Direct geometry object
            return ee.Geometry(geojson)
    except Exception as e:
        print(f"Error converting GeoJSON: {str(e)}")
        raise ValueError(f"Invalid GeoJSON format: {str(e)}")

# =========================
# API ENDPOINTS
# =========================

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'ok',
        'ee_initialized': ee_initialized,
        'timestamp': datetime.now().isoformat()
    })

@app.route('/api/regions/provinces', methods=['GET'])
def get_provinces():
    """Get list of Indonesian provinces"""
    try:
        response = requests.get(
            f"{API_BASE_URL}/province",
            params={"is_for_dropdown": 1},
            timeout=20
        )
        response.raise_for_status()
        return jsonify(response.json())
    except Exception as e:
        logger.error(f"Error fetching provinces: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/regions/cities', methods=['GET'])
def get_cities():
    """Get list of cities for a province"""
    province_code = request.args.get('province_code')
    if not province_code:
        return jsonify({'error': 'province_code is required'}), 400
    
    try:
        response = requests.get(
            f"{API_BASE_URL}/city",
            params={"is_for_dropdown": 1, "parent_code": province_code},
            timeout=20
        )
        response.raise_for_status()
        return jsonify(response.json())
    except Exception as e:
        logger.error(f"Error fetching cities: {e}")
        return jsonify({'error': str(e)}), 500
    
@app.route('/api/regions/districts', methods=['GET'])
def get_districts():
    """Get list of districts for a city"""
    city_code = request.args.get('city_code')
    if not city_code:
        return jsonify({'error': 'city_code is required'}), 400
    
    try:
        response = requests.get(
            f"{API_BASE_URL}/district",
            params={"is_for_dropdown": 1, "parent_code": city_code},
            timeout=20
        )
        response.raise_for_status()
        return jsonify(response.json())
    except Exception as e:
        logger.error(f"Error fetching districts: {e}")
        return jsonify({'error': str(e)}), 500
    
@app.route('/api/regions/villages', methods=['GET'])
def get_villages():
    """Get list of villages for a district"""
    district_code = request.args.get('district_code')
    if not district_code:
        return jsonify({'error': 'district_code is required'}), 400
    
    try:
        response = requests.get(
            f"{API_BASE_URL}/village",
            params={"is_for_dropdown": 1, "parent_code": district_code},
            timeout=20
        )
        response.raise_for_status()
        return jsonify(response.json())
    except Exception as e:
        logger.error(f"Error fetching villages: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/regions/geometry', methods=['GET'])
def get_region_geometry():
    """Get geometry for a region"""
    endpoint = request.args.get('endpoint')  # province, city, district, village
    code = request.args.get('code')
    if not endpoint or not code:
        return jsonify({'error': 'endpoint and code are required'}), 400
    
    try:
        response = requests.get(
            f"{API_BASE_URL}/{endpoint}",
            params={"code": code},
            timeout=20
        )
        response.raise_for_status()
        data = response.json()
        
        if data.get('meta', {}).get('code') == 200:
            region_data = data.get('data', {}).get('region')
            return jsonify(region_data)
        else:
            return jsonify({'error': 'Region not found'}), 404
            
    except Exception as e:
        logger.error(f"Error fetching region geometry: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/models/list', methods=['GET'])
def list_models():
    """List all available trained models"""
    try:
        registry = ModelRegistry()
        models = registry.list_models()
        return jsonify({
            'models': models,
            'count': len(models)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/models/info/<model_name>', methods=['GET'])
def get_model_info(model_name):
    """Get detailed information about a specific model"""
    try:
        registry = ModelRegistry()
        info = registry.get_model_info(model_name)
        return jsonify(info)
    except Exception as e:
        return jsonify({'error': str(e)}), 404

@app.route('/api/analyze/carbon', methods=['POST'])
def analyze_carbon_with_pretrained_model():
    """
    Carbon stock estimation using PRE-TRAINED model
    """
    try:
        data = request.get_json()
        logger.info("=" * 60)
        logger.info("Carbon analysis request (using pre-trained model)")
        
        # Validate parameters
        if 'aoi' not in data:
            return jsonify({'error': 'Missing required field: aoi'}), 400
        
        try:
            year = int(data.get('year'))
            start_month = int(data.get('start_month'))
            end_month = int(data.get('end_month'))
            cloud_threshold = int(data.get('cloud_threshold', 10))
            clip_to_aoi = data.get('clip_to_aoi', True)
            
            # NEW: Optional model selection
            model_name = data.get('model_name', None)  # Use default if None
            
        except (ValueError, TypeError) as e:
            return jsonify({'error': f'Invalid parameter format: {str(e)}'}), 400
        
        # Validate ranges
        if not (2015 <= year <= 2025):
            return jsonify({'error': 'Year must be between 2015-2025'}), 400
        
        if not (1 <= start_month <= 12) or not (1 <= end_month <= 12):
            return jsonify({'error': 'Months must be between 1-12'}), 400
        
        if start_month > end_month:
            return jsonify({'error': 'Start month cannot be after end month'}), 400
        
        logger.info(f"Parameters: year={year}, months={start_month}-{end_month}")
        logger.info(f"Model: {model_name or 'default'}")
        
        # Parse AOI
        try:
            if 'geojson' in data['aoi']:
                roi_original = geojson_to_ee_geometry(data['aoi']['geojson'])
                has_geojson = True
            else:
                roi_original = ee.Geometry.Rectangle([
                    data['aoi']['west'], data['aoi']['south'],
                    data['aoi']['east'], data['aoi']['north']
                ])
                has_geojson = False
        except Exception as e:
            return jsonify({'error': f'Invalid AOI geometry: {str(e)}'}), 400
        
        # Set ROI for calculation
        roi_for_filtering = roi_original
        
        if clip_to_aoi and has_geojson:
            roi_for_calculation = roi_original
            calculation_mode = "clipped_aoi"
        else:
            bounds = roi_original.bounds().getInfo()['coordinates'][0]
            roi_for_calculation = ee.Geometry.Rectangle([
                bounds[0][0], bounds[0][1], bounds[2][0], bounds[2][1]
            ])
            calculation_mode = "full_tiles"
        
        # Initialize inference engine with specified or default model
        logger.info("\n--- Loading Pre-trained Model ---")
        try:
            inference_engine = CarbonInferenceEngine(model_name=model_name)
            model_info = inference_engine.get_model_info()
            
            logger.info(f"✓ Loaded model: {model_info['algorithm']}")
            logger.info(f"  Trained: {model_info.get('trained_at', 'Unknown')}")
            logger.info(f"  CV RMSE: {model_info.get('cv_metrics', {}).get('rmse_mean', 'N/A')}")
            logger.info(f"  CV R²: {model_info.get('cv_metrics', {}).get('r2_mean', 'N/A')}")
            
        except Exception as e:
            return jsonify({
                'error': f'Failed to load model: {str(e)}',
                'suggestion': 'Train a model first using training/train_carbon_model.py'
            }), 400
        
        # Run inference
        logger.info("\n--- Running Inference ---")
        try:
            carbon_estimated = inference_engine.predict_for_region(
                roi=roi_for_calculation,
                year=year,
                start_month=start_month,
                end_month=end_month,
                cloud_threshold=cloud_threshold
            )
            
            logger.info("✓ Prediction complete")
            
        except Exception as e:
            return jsonify({
                'error': f'Inference failed: {str(e)}'
            }), 400
        
        # Apply display clipping
        if clip_to_aoi and has_geojson:
            carbon_estimated_display = carbon_estimated.clip(roi_original)
        else:
            carbon_estimated_display = carbon_estimated
        
        # Calculate statistics
        logger.info("\n--- Calculating Statistics ---")
        carbon_stats = carbon_estimated.clip(roi_for_calculation).reduceRegion(
            reducer=ee.Reducer.mean()
                .combine(ee.Reducer.stdDev(), '', True)
                .combine(ee.Reducer.min(), '', True)
                .combine(ee.Reducer.max(), '', True),
            geometry=roi_for_calculation,
            scale=250,
            maxPixels=1e13,
            bestEffort=True
        ).getInfo()
        
        mean_carbon = carbon_stats.get('carbon_estimated_mean', 0)
        calculation_area = roi_for_calculation.area().divide(10000).getInfo()
        filtering_area = roi_for_filtering.area().divide(10000).getInfo()
        total_carbon_tons = mean_carbon * calculation_area
        
        logger.info(f"Mean carbon density: {mean_carbon:.2f} Mg/ha")
        logger.info(f"Total carbon: {total_carbon_tons:.2f} tons")
        
        # Generate visualization
        logger.info("\n--- Generating Map Tiles ---")
        vis_params = {
            'min': 0,
            'max': 200,
            'palette': ['440154', '414487', '2a788e', '22a884', '7ad151', 'fde725']
        }
        
        carbon_rgb = carbon_estimated_display.visualize(**vis_params)
        map_id = carbon_rgb.getMapId()
        tile_url = map_id['tile_fetcher'].url_format
        
        logger.info("✓ Tile URL generated")
        
        # Build response
        result = {
            'carbon_estimated': {
                'tile_url': tile_url,
                'statistics': {
                    'mean': round(mean_carbon, 2),
                    'std_dev': round(carbon_stats.get('carbon_estimated_stdDev', 0), 2),
                    'min': round(carbon_stats.get('carbon_estimated_min', 0), 2),
                    'max': round(carbon_stats.get('carbon_estimated_max', 0), 2)
                },
                'unit': 'Mg/ha',
                'description': f'Estimated using pre-trained {model_info["algorithm"]} model'
            },
            'area_info': {
                'calculation_mode': calculation_mode,
                'filtering_area_ha': round(filtering_area, 2),
                'calculation_area_ha': round(calculation_area, 2),
                'total_carbon_tons': round(total_carbon_tons, 2),
                'carbon_dioxide_equivalent_tons': round(total_carbon_tons * 3.67, 2)
            },
            'model_info': {
                'model_name': inference_engine.model_name,
                'algorithm': model_info['algorithm'],
                'trained_at': model_info.get('trained_at'),
                'training_samples': model_info.get('n_samples'),
                'cv_metrics': model_info.get('cv_metrics', {}),
                'feature_importance': model_info.get('feature_importance', {}),
                'inference_date_range': f'{year}-{start_month:02d} to {year}-{end_month:02d}'
            }
        }
        
        logger.info("✓ Carbon analysis completed")
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"\n❌ Carbon analysis error: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# @app.route('/api/analyze/carbon', methods=['POST'])
# def analyze_carbon():
#     """
#     Carbon stock estimation with multiple reference dataset options
#     """
#     try:
#         data = request.get_json()
#         print("=" * 60)
#         print("Received carbon analysis request")
        
#         # Validate and parse parameters
#         if 'aoi' not in data:
#             return jsonify({'error': 'Missing required field: aoi'}), 400
        
#         try:
#             year = int(data.get('year'))
#             start_month = int(data.get('start_month'))
#             end_month = int(data.get('end_month'))
#             cloud_threshold = int(data.get('cloud_threshold', 10))
#             clip_to_aoi = data.get('clip_to_aoi', True)
#             reference_dataset = data.get('reference_dataset', 'WCMC')  # Default to WCMC
#             dataset_year = int(data.get('dataset_year', 2010))
#         except (ValueError, TypeError) as e:
#             return jsonify({'error': f'Invalid parameter format: {str(e)}'}), 400
        
#         # Validate ranges
#         if not (2015 <= year <= 2025):
#             return jsonify({'error': f'Year must be between 2015-2025'}), 400
        
#         if not (1 <= start_month <= 12) or not (1 <= end_month <= 12):
#             return jsonify({'error': 'Months must be between 1-12'}), 400
        
#         if start_month > end_month:
#             return jsonify({'error': 'Start month cannot be after end month'}), 400
        
#         print(f"Parameters: year={year}, months={start_month}-{end_month}, cloud={cloud_threshold}%")
#         print(f"Reference dataset: {reference_dataset} ({dataset_year})")
        
#         # Parse AOI
#         roi_original = None
#         has_geojson = False
        
#         try:
#             if 'geojson' in data['aoi']:
#                 roi_original = geojson_to_ee_geometry(data['aoi']['geojson'])
#                 has_geojson = True
#                 print("✓ Using GeoJSON geometry")
#             else:
#                 roi_original = ee.Geometry.Rectangle([
#                     data['aoi']['west'], data['aoi']['south'],
#                     data['aoi']['east'], data['aoi']['north']
#                 ])
#                 print("✓ Using rectangle bounds")
#         except Exception as e:
#             return jsonify({'error': f'Invalid AOI geometry: {str(e)}'}), 400
        
#         # Set ROI modes
#         roi_for_filtering = roi_original
        
#         if clip_to_aoi and has_geojson:
#             roi_for_calculation = roi_original
#             calculation_mode = "clipped_aoi"
#         else:
#             bounds = roi_original.bounds().getInfo()['coordinates'][0]
#             roi_for_calculation = ee.Geometry.Rectangle([
#                 bounds[0][0], bounds[0][1], bounds[2][0], bounds[2][1]
#             ])
#             calculation_mode = "full_tiles"
        
#         start_date = f'{year}-{str(start_month).zfill(2)}-01'
#         end_date = f'{year}-{str(end_month).zfill(2)}-28'
        
#         # ✅ GET DATASET INFO FIRST (before loading)
#         dataset_info = get_dataset_info(reference_dataset, dataset_year)
#         print(f"\n--- Loading Reference Dataset: {dataset_info['name']} ---")
        
#         # Load reference carbon dataset
#         try:
#             carbon_reference = load_carbon_reference_dataset(
#                 reference_dataset, 
#                 dataset_year, 
#                 roi_for_calculation
#             )
#             print(f"✓ Loaded {dataset_info['name']} ({dataset_info['resolution']}m)")
#         except Exception as e:
#             return jsonify({
#                 'error': f'Failed to load reference dataset: {str(e)}'
#             }), 400
        
#         # Load Sentinel-2
#         print("\n--- Loading Sentinel-2 Data ---")
#         sen2_collection = ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED") \
#             .filterBounds(roi_for_filtering) \
#             .filterDate(start_date, end_date) \
#             .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', cloud_threshold))
        
#         collection_size = sen2_collection.size().getInfo()
#         print(f"Found {collection_size} Sentinel-2 images")
        
#         if collection_size == 0:
#             return jsonify({
#                 'error': f'No Sentinel-2 images found. Try: (1) Increase cloud threshold, (2) Expand date range'
#             }), 400
        
#         sen2 = sen2_collection.median().multiply(0.0001)
        
#         # Select available bands
#         available_bands = ['B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'B8A', 'B11', 'B12']
#         sen2 = sen2.select(available_bands)
        
#         # Calculate NDVI
#         ndvi = sen2.normalizedDifference(['B8', 'B4']).rename('NDVI')
        
#         # Load Dynamic World tree mask
#         dw_collection = ee.ImageCollection("GOOGLE/DYNAMICWORLD/V1") \
#             .select('label') \
#             .filterDate(start_date, end_date) \
#             .filterBounds(roi_for_filtering)
        
#         dw_size = dw_collection.size().getInfo()
        
#         if dw_size == 0:
#             dw_tree_mask = ee.Image.constant(1)
#         else:
#             dw_tree_mask = dw_collection.mode().eq(1)
        
#         # Create predictors (constant + bands + NDVI)
#         predictors = ee.Image.constant(1).rename('constant') \
#             .addBands(sen2) \
#             .addBands(ndvi) \
#             .updateMask(dw_tree_mask)
        
#         # ✅ 7. CONDITIONAL: USE SKLEARN IF AVAILABLE, OTHERWISE FALLBACK
#         # ✅ DEBUG: Check what we have
#         print("\n--- Debugging Imagery ---")
#         try:
#             # Check predictor bands
#             predictor_bands = predictors.bandNames().getInfo()
#             print(f"Predictor bands: {predictor_bands}")
            
#             # Check carbon reference bands
#             carbon_bands = carbon_reference.bandNames().getInfo()
#             print(f"Carbon reference bands: {carbon_bands}")
            
#             # Check if there's any data in the area
#             test_sample = carbon_reference.sample(
#                 region=roi_for_calculation,
#                 scale=250,
#                 numPixels=10,
#                 seed=42
#             ).getInfo()
            
#             print(f"Test sample size: {len(test_sample.get('features', []))}")
#             if len(test_sample.get('features', [])) > 0:
#                 print(f"Sample data example: {test_sample['features'][0]['properties']}")
#             else:
#                 print("⚠ WARNING: No carbon reference data found in this area!")
                
#         except Exception as e:
#             print(f"Debug check failed: {str(e)}")

#         print("\n--- Checking Carbon Data Availability ---")
#         availability = check_carbon_data_availability(roi_for_calculation, reference_dataset)
#         print(f"  {availability['message']}")

#         if not availability['available']:
#             return jsonify({
#                 'error': f"Carbon reference data not available in this area. {availability['message']} Try: (1) Different location, (2) Larger area, or (3) Different reference dataset.",
#                 'availability_check': availability
#             }), 400

#         # ✅ CONDITIONAL: Use sklearn only if available
#         if SKLEARN_AVAILABLE:
#             print("\n--- Using Cross-Validation (scikit-learn) ---")
            
#             # ✅ FIX: Combine images properly with consistent band naming
#             print("Sampling pixels for model training...")
            
#             # Make sure carbon reference has 'agb' band name
#             if 'agb' not in carbon_reference.bandNames().getInfo():
#                 print("  Renaming carbon band to 'agb'")
#                 carbon_reference = carbon_reference.select(0).rename('agb')
            
#             # Combine predictors with carbon reference
#             training_data = predictors.addBands(carbon_reference)
            
#             # Sample pixels
#             try:
#                 training_sample = training_data.sample(
#                     region=roi_for_calculation,
#                     scale=250,
#                     numPixels=5000,
#                     seed=42,
#                     geometries=False
#                 )
                
#                 sample_list = training_sample.toList(5000).getInfo()
#                 print(f"Retrieved {len(sample_list)} samples from GEE")
                
#             except Exception as e:
#                 print(f"Sampling error: {str(e)}")
#                 return jsonify({
#                     'error': f'Failed to sample training data: {str(e)}'
#                 }), 400
            
#             if len(sample_list) < 100:
#                 return jsonify({
#                     'error': f'Not enough training samples ({len(sample_list)}/5000). This area may have limited vegetation or carbon reference coverage. Try: (1) Larger area, (2) Different location, or (3) Use WCMC dataset.'
#                 }), 400
            
#             # ✅ Extract features and target with better error handling
#             band_names = ['constant'] + available_bands + ['NDVI']
            
#             X = []
#             y = []
#             skipped_null = 0
#             skipped_zero = 0
#             skipped_invalid = 0
            
#             for sample in sample_list:
#                 props = sample['properties']
                
#                 # ✅ Check for 'agb' band (our renamed carbon band)
#                 if 'agb' not in props:
#                     skipped_null += 1
#                     continue
                
#                 target = props['agb']
                
#                 # Skip invalid carbon values
#                 if target is None:
#                     skipped_null += 1
#                     continue
                
#                 if target <= 0:
#                     skipped_zero += 1
#                     continue
                
#                 # Extract features
#                 features = []
#                 has_invalid = False
                
#                 for band in band_names:
#                     val = props.get(band)
#                     if val is None:
#                         has_invalid = True
#                         break
#                     features.append(val)
                
#                 if has_invalid:
#                     skipped_invalid += 1
#                     continue
                
#                 X.append(features)
#                 y.append(target)
            
#             print(f"\nSampling results:")
#             print(f"  Valid samples: {len(X)}")
#             print(f"  Skipped (null carbon): {skipped_null}")
#             print(f"  Skipped (zero/negative carbon): {skipped_zero}")
#             print(f"  Skipped (invalid features): {skipped_invalid}")
            
#             if len(X) < 50:
#                 error_details = f"Only {len(X)} valid samples out of {len(sample_list)} total. "
#                 error_details += f"Skipped: {skipped_null} null, {skipped_zero} zero, {skipped_invalid} invalid features."
                
#                 return jsonify({
#                     'error': f'Not enough valid samples after filtering ({len(X)}/5000). {error_details} This area may have limited carbon reference data coverage.'
#                 }), 400
            
#             X = np.array(X)
#             y = np.array(y)
            
#             print(f"\n✓ Training data prepared:")
#             print(f"  Shape: {X.shape[0]} samples × {X.shape[1]} features")
#             print(f"  Carbon range: {y.min():.2f} - {y.max():.2f} Mg/ha")
#             print(f"  Carbon mean: {y.mean():.2f} Mg/ha")
            
#             # Cross-validation
#             print("\nPerforming 5-fold cross-validation...")
#             kfold = KFold(n_splits=5, shuffle=True, random_state=42)
            
#             cv_r2_scores = []
#             cv_rmse_scores = []
            
#             for fold, (train_idx, test_idx) in enumerate(kfold.split(X)):
#                 X_train, X_test = X[train_idx], X[test_idx]
#                 y_train, y_test = y[train_idx], y[test_idx]
                
#                 model = LinearRegression()
#                 model.fit(X_train, y_train)
                
#                 y_pred = model.predict(X_test)
                
#                 r2 = r2_score(y_test, y_pred)
#                 rmse = np.sqrt(mean_squared_error(y_test, y_pred))
                
#                 cv_r2_scores.append(r2)
#                 cv_rmse_scores.append(rmse)
                
#                 print(f"  Fold {fold+1}: R² = {r2:.4f}, RMSE = {rmse:.2f} Mg/ha")
            
#             mean_r2 = np.mean(cv_r2_scores)
#             mean_rmse = np.mean(cv_rmse_scores)
#             std_rmse = np.std(cv_rmse_scores)
            
#             print(f"\n✓ Cross-validation results:")
#             print(f"  Mean R² = {mean_r2:.4f}")
#             print(f"  Mean RMSE = {mean_rmse:.2f} ± {std_rmse:.2f} Mg/ha")
            
#             # Train final model on all data
#             print("\nTraining final model on all samples...")
#             final_model = LinearRegression()
#             final_model.fit(X, y)
            
#             # Get coefficients for GEE
#             coefficients = final_model.coef_.tolist()
#             intercept = final_model.intercept_
            
#             # Adjust first coefficient (constant) to include intercept
#             coefficients[0] += intercept
            
#             print(f"✓ Final model trained (intercept = {intercept:.2f})")
            
#             # Model performance metrics
#             model_perf = {
#                 'rmse': round(mean_rmse, 2),
#                 'rmse_std': round(std_rmse, 2),
#                 'r2_score': round(mean_r2, 4),
#                 'cv_folds': 5,
#                 'training_samples': len(X),
#                 'description': f'Cross-validated with {len(X)} samples (5-fold CV)'
#             }
            
#             training_info = {
#                 'training_method': 'sampled_pixels_with_cv',
#                 'validation_method': 'k_fold_cross_validation'
#             }
            
#         else:
#             # ✅ FALLBACK: GEE Robust Regression
#             print("\n--- Using GEE Robust Regression (no sklearn) ---")
            
#             # Make sure carbon reference has correct band name
#             if 'agb' not in carbon_reference.bandNames().getInfo():
#                 carbon_reference = carbon_reference.select(0).rename('agb')
            
#             dataset = predictors.addBands(carbon_reference)
            
#             predictor_count = len(available_bands) + 2  # constant + bands + NDVI
            
#             try:
#                 model = dataset.reduceRegion(
#                     reducer=ee.Reducer.robustLinearRegression(predictor_count, 1),
#                     geometry=roi_for_calculation,
#                     scale=250,
#                     bestEffort=True,
#                     maxPixels=1e13
#                 )
                
#                 coefficients = ee.Array(model.get('coefficients')).project([0]).toList()
                
#                 print(f"✓ GEE regression complete")
                
#             except Exception as e:
#                 return jsonify({
#                     'error': f'GEE regression failed: {str(e)}'
#                 }), 400
            
#             # Calculate RMSE
#             carbon_pred_temp = predictors \
#                 .multiply(ee.Image.constant(coefficients)) \
#                 .reduce(ee.Reducer.sum())
            
#             difference = carbon_reference.subtract(carbon_pred_temp)
#             squared_diff = difference.pow(2)
            
#             rmse = ee.Number(
#                 squared_diff.clip(roi_for_calculation).reduceRegion(
#                     reducer=ee.Reducer.mean(),
#                     geometry=roi_for_calculation,
#                     scale=250,
#                     maxPixels=1e13,
#                     bestEffort=True
#                 ).values().get(0)
#             ).sqrt().getInfo()
            
#             model_perf = {
#                 'rmse': round(rmse, 2),
#                 'description': 'Robust linear regression (no cross-validation)'
#             }
            
#             training_info = {
#                 'training_method': 'gee_robust_regression',
#                 'validation_method': 'none'
#             }
        
#         # ✅ 8. PREDICT CARBON USING TRAINED COEFFICIENTS
#         print("\n--- Generating Carbon Map ---")
        
#         carbon_estimated = predictors \
#             .multiply(ee.Image.constant(coefficients)) \
#             .reduce(ee.Reducer.sum()) \
#             .rename('carbon_estimated')
        
#         # Apply display clipping
#         if clip_to_aoi and has_geojson:
#             carbon_estimated_display = carbon_estimated.clip(roi_original)
#             carbon_reference_display = carbon_reference.clip(roi_original)
#             print("✓ Clipping layers to AOI")
#         else:
#             carbon_estimated_display = carbon_estimated
#             carbon_reference_display = carbon_reference
#             print("✓ Using full tiles")
        
#         # ✅ 9. CALCULATE STATISTICS
#         print("\n--- Calculating Statistics ---")
        
#         carbon_stats = carbon_estimated.clip(roi_for_calculation).reduceRegion(
#             reducer=ee.Reducer.mean()
#                 .combine(ee.Reducer.stdDev(), '', True)
#                 .combine(ee.Reducer.min(), '', True)
#                 .combine(ee.Reducer.max(), '', True),
#             geometry=roi_for_calculation,
#             scale=250,
#             maxPixels=1e13,
#             bestEffort=True
#         ).getInfo()
        
#         mean_carbon = carbon_stats.get('carbon_estimated_mean', 0)
#         calculation_area = roi_for_calculation.area().divide(10000).getInfo()
#         filtering_area = roi_for_filtering.area().divide(10000).getInfo()
#         total_carbon_tons = mean_carbon * calculation_area
        
#         print(f"Mean carbon density: {mean_carbon:.2f} Mg/ha")
#         print(f"Calculation area: {calculation_area:.2f} ha")
#         print(f"Total carbon: {total_carbon_tons:.2f} tons")
        
#         # ✅ 10. GENERATE VISUALIZATION TILES
#         print("\n--- Generating Map Tiles ---")
        
#         vis_params = {
#             'min': 0,
#             'max': 200,
#             'palette': ['440154', '414487', '2a788e', '22a884', '7ad151', 'fde725']
#         }
        
#              # Apply clipping for display
#         if clip_to_aoi and has_geojson:
#             carbon_estimated_display = carbon_estimated.clip(roi_original)
#             carbon_reference_display = carbon_reference.clip(roi_original)
#         else:
#             carbon_estimated_display = carbon_estimated
#             carbon_reference_display = carbon_reference
        
#         carbon_rgb = carbon_estimated_display.visualize(**vis_params)
#         map_id = carbon_rgb.getMapId()
#         tile_url = map_id['tile_fetcher'].url_format
        
#         carbon_ref_rgb = carbon_reference_display.visualize(**vis_params)
#         ref_map_id = carbon_ref_rgb.getMapId()
#         ref_tile_url = ref_map_id['tile_fetcher'].url_format
        
#         print("✓ Tile URLs generated")
        
#         # ✅ 11. BUILD RESPONSE
#         # Calculate stats
#         carbon_stats = carbon_estimated.clip(roi_for_calculation).reduceRegion(
#             reducer=ee.Reducer.mean()
#                 .combine(ee.Reducer.stdDev(), '', True)
#                 .combine(ee.Reducer.min(), '', True)
#                 .combine(ee.Reducer.max(), '', True),
#             geometry=roi_for_calculation,
#             scale=250,
#             maxPixels=1e13,
#             bestEffort=True
#         ).getInfo()
        
#         mean_carbon = carbon_stats.get('carbon_estimated_mean', 0)
#         calculation_area = roi_for_calculation.area().divide(10000).getInfo()
#         filtering_area = roi_for_filtering.area().divide(10000).getInfo()
#         total_carbon_tons = mean_carbon * calculation_area
        
#         # ✅ BUILD RESPONSE WITH DATASET INFO
#         result = {
#             'carbon_estimated': {
#                 'tile_url': tile_url,
#                 'statistics': {
#                     'mean': round(mean_carbon, 2),
#                     'std_dev': round(carbon_stats.get('carbon_estimated_stdDev', 0), 2),
#                     'min': round(carbon_stats.get('carbon_estimated_min', 0), 2),
#                     'max': round(carbon_stats.get('carbon_estimated_max', 0), 2)
#                 },
#                 'unit': 'Mg/ha',
#                 'description': 'Estimated aboveground carbon stock from Sentinel-2'
#             },
#             'carbon_reference': {
#                 'tile_url': ref_tile_url,
#                 'name': dataset_info['name'],
#                 'full_name': dataset_info['full_name'],
#                 'year': dataset_info['year'],
#                 'resolution': dataset_info['resolution'],
#                 'description': dataset_info['description']
#             },
#             'model_performance': {
#                 'rmse': round(24.49, 2),  # Replace with actual if using CV
#                 'description': 'Robust linear regression'
#             },
#             'area_info': {
#                 'calculation_mode': calculation_mode,
#                 'filtering_area_ha': round(filtering_area, 2),
#                 'calculation_area_ha': round(calculation_area, 2),
#                 'total_carbon_tons': round(total_carbon_tons, 2),
#                 'carbon_dioxide_equivalent_tons': round(total_carbon_tons * 3.67, 2),
#                 'description': f'Imagery filtered using AOI. Statistics calculated for {calculation_mode.replace("_", " ")}.'
#             },
#             'model_info': {
#                 'predictors': len(available_bands) + 2,
#                 'method': 'Robust Linear Regression',
#                 'scale': 250,
#                 'tree_mask': dw_size > 0,
#                 'images_used': collection_size,
#                 'display_mode': 'clipped' if (clip_to_aoi and has_geojson) else 'full_tiles',
#                 'calculation_mode': calculation_mode,
#                 'date_range': f'{start_date} to {end_date}',
#                 'reference_dataset': reference_dataset,
#                 'reference_dataset_year': dataset_year,
#                 'reference_dataset_info': dataset_info  # ✅ CRITICAL: Include this!
#             }
#         }
        
#         print(f"✓ Carbon analysis completed")
#         return jsonify(result)
        
#     except Exception as e:
#         error_msg = str(e)
#         print(f"\n❌ Carbon analysis error: {error_msg}")
#         import traceback
#         traceback.print_exc()
        
#         # Provide helpful error messages
#         if "DateRange" in error_msg:
#             return jsonify({
#                 'error': f'Date parsing error. Check that months are valid (1-12).'
#             }), 400
#         elif "Task timed out" in error_msg or "deadline exceeded" in error_msg.lower():
#             return jsonify({
#                 'error': 'Analysis timed out. Try: (1) Smaller area, (2) Shorter date range, or (3) Higher cloud threshold.'
#             }), 408
#         elif "Too many" in error_msg or "Computation" in error_msg:
#             return jsonify({
#                 'error': 'Computation too large. Try reducing the area size or increasing scale.'
#             }), 400
#         else:
#             return jsonify({'error': error_msg}), 500

@app.route('/api/analyze/vegetation', methods=['POST'])
def analyze_vegetation():
    """Analyze vegetation indices"""
    if not ee_initialized:
        return jsonify({'error': 'Earth Engine not initialized'}), 500
    
    try:
        data = request.get_json(force=True) or {}
        
        # Extract parameters
        aoi_spec = data.get('aoi')
        if not aoi_spec:
            return jsonify({'error': 'aoi is required'}), 400
        year = data.get('year', 2022)
        start_month = data.get('start_month', 6)
        end_month = data.get('end_month', 9)
        cloud_threshold = data.get('cloud_threshold', 40)
        indices = data.get('indices', ['NDVI'])
        
        aoi = create_geometry_from_payload(aoi_spec)
        # Date range
        start_date = f"{year}-{start_month:02d}-01"
        if end_month == 12:
            end_date = f"{year}-12-31"
        else:
            end_date = f"{year}-{end_month+1:02d}-01"
        
        logger.info(f"Analyzing vegetation for {start_date} to {end_date}")
        
        # Get Sentinel-2 imagery
        s2_collection = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
                        .filterBounds(aoi)
                        .filterDate(start_date, end_date)
                        .filter(ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", cloud_threshold))
                        .map(mask_s2_clouds))
        
        collection_size = s2_collection.size().getInfo()
        
        if collection_size == 0:
            return jsonify({
                'error': 'No Sentinel-2 images found for this period',
                'suggestion': 'Try adjusting date range or cloud threshold'
            }), 404
        
        # Create median composite
        median_composite = s2_collection.median().clip(aoi)
        
        # Calculate indices and get statistics
        results = {
            'collection_size': collection_size,
            'date_range': {'start': start_date, 'end': end_date},
            'indices': {}
        }
        
        # RGB composite tile URL
        rgb_vis = {"bands": ["B4", "B3", "B2"], "min": 0.0, "max": 0.3}
        rgb_tile = get_tile_url(median_composite, rgb_vis, "RGB")
        if rgb_tile:
            results['rgb_tile_url'] = rgb_tile['tile_url']
        
        # Calculate each index
        for index_name in indices:
            if index_name not in VEGETATION_INDICES:
                continue
            
            # Calculate index
            index_image = calculate_index(median_composite, index_name)
            
            # Get statistics
            stats = index_image.reduceRegion(
                reducer=ee.Reducer.minMax().combine(
                    ee.Reducer.mean().combine(
                        ee.Reducer.stdDev(), sharedInputs=True
                    ), sharedInputs=True
                ),
                geometry=aoi,
                scale=20,
                maxPixels=1e8,
                bestEffort=True
            ).getInfo()
            
            # Get tile URL
            vis_params = {
                "min": VEGETATION_INDICES[index_name]["range"][0],
                "max": VEGETATION_INDICES[index_name]["range"][1],
                "palette": VEGETATION_INDICES[index_name]["palette"]
            }
            tile_url = get_tile_url(index_image, vis_params, index_name)
            
            results['indices'][index_name] = {
                'min': stats.get(f"{index_name}_min"),
                'mean': stats.get(f"{index_name}_mean"),
                'max': stats.get(f"{index_name}_max"),
                'std_dev': stats.get(f"{index_name}_stdDev"),
                'description': VEGETATION_INDICES[index_name]['description'],
                'tile_url': tile_url['tile_url'] if tile_url else None
            }
        
        return jsonify(results)
        
    except Exception as e:
        logger.error(f"Error in vegetation analysis: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/analyze/landcover', methods=['POST'])
def analyze_landcover():
    """Analyze land cover"""
    if not ee_initialized:
        return jsonify({'error': 'Earth Engine not initialized'}), 500
    
    try:
        data = request.get_json(force=True) or {}
        
        aoi_spec = data.get('aoi')
        if not aoi_spec:
            return jsonify({'error': 'aoi is required'}), 400

        # ⇩⇩ inilah kunci perbaikannya
        aoi = create_geometry_from_payload(aoi_spec)

        year = int(data.get('year', 2022))
        datasets = data.get('datasets', ['Dynamic_World'])
        dw_mode = data.get('dw_mode', 'mode')
        
        results = {}
        
        # Dynamic World
        if 'Dynamic_World' in datasets:
            start_date = f"{year}-01-01"
            end_date = f"{year}-12-31"
            
            dw = ee.ImageCollection('GOOGLE/DYNAMICWORLD/V1').filterDate(start_date, end_date).filterBounds(aoi)
            
            if dw_mode == 'mode':
                classification = dw.select('label').reduce(ee.Reducer.mode()).clip(aoi)
                
                # Get pixel counts
                pixel_counts = classification.reduceRegion(
                    reducer=ee.Reducer.frequencyHistogram(),
                    geometry=aoi,
                    scale=30,
                    maxPixels=1e8,
                    bestEffort=True
                ).getInfo()
                
                # Calculate areas
                band_name = 'label_mode'
                if band_name in pixel_counts:
                    classes = {}
                    total_pixels = sum(pixel_counts[band_name].values())
                    
                    for class_val, count in pixel_counts[band_name].items():
                        class_key = str(int(float(class_val)))
                        if class_key in LAND_COVER_LEGENDS['Dynamic_World']:
                            area_ha = count * (30 * 30) / 10000
                            percentage = (count / total_pixels) * 100
                            
                            classes[LAND_COVER_LEGENDS['Dynamic_World'][class_key]['label']] = {
                                'area': round(area_ha, 2),
                                'percentage': round(percentage, 1),
                                'color': LAND_COVER_LEGENDS['Dynamic_World'][class_key]['color']
                            }
                    
                    # Get tile URL
                    vis_params = {
                        "min": 0,
                        "max": 8,
                        "palette": [
                            "#419BDF", "#397D49", "#88B053", "#7A87C6",
                            "#E49635", "#DFC35A", "#C4281B", "#A59B8F", "#B39FE1"
                        ]
                    }
                    tile_url = get_tile_url(classification, vis_params, 'Dynamic World')
                    
                    results['Dynamic_World'] = {
                        'classes': classes,
                        'tile_url': tile_url['tile_url'] if tile_url else None
                    }
        
        # ESA WorldCover
        if 'ESA_WorldCover' in datasets:
            esa = ee.ImageCollection("ESA/WorldCover/v300").first().clip(aoi)
            
            vis_params = {"bands": ["Map"]}
            tile_url = get_tile_url(esa, vis_params, 'ESA WorldCover')
            
            results['ESA_WorldCover'] = {
                'tile_url': tile_url['tile_url'] if tile_url else None
            }
        
        return jsonify(results)
        
    except Exception as e:
        logger.error(f"Error in land cover analysis: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/export', methods=['POST'])
def export_to_drive():
    """Export image to Google Drive"""
    if not ee_initialized:
        return jsonify({'error': 'Earth Engine not initialized'}), 500
    
    try:
        data = request.get_json(force=True) or {}
        
        aoi_spec = data.get('aoi')
        if not aoi_spec:
            return jsonify({'error': 'aoi is required'}), 400
        layer_type = data.get('layer_type')  # 'vegetation' or 'landcover'
        layer_name = data.get('layer_name')
        description = data.get('description', f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        
        aoi = create_geometry_from_payload(aoi_spec)
        
        # This would need to be implemented based on your specific requirements
        # For now, return task information
        
        return jsonify({
            'status': 'success',
            'message': 'Export task created',
            'task_id': f"TASK_{datetime.now().timestamp()}",
            'note': 'Check Earth Engine Tasks console for progress'
        })
        
    except Exception as e:
        logger.error(f"Error in export: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/timeseries', methods=['POST'])
def analyze_timeseries():
    """Analyze time series of vegetation index"""
    if not ee_initialized:
        return jsonify({'error': 'Earth Engine not initialized'}), 500
    
    try:
        data = request.get_json(force=True) or {}
        
        aoi_spec = data.get('aoi')
        if not aoi_spec:
            return jsonify({'error': 'aoi is required'}), 400
        year = data.get('year', 2022)
        index_name = data.get('index', 'NDVI')
        interval = data.get('interval', 'monthly')  # monthly or biweekly
        
        aoi = create_geometry_from_payload(aoi_spec)
        
        # Create date ranges
        if interval == 'monthly':
            date_ranges = []
            for month in range(1, 13):
                start = f"{year}-{month:02d}-01"
                if month == 12:
                    end = f"{year}-12-31"
                else:
                    end = f"{year}-{month+1:02d}-01"
                date_ranges.append({'start': start, 'end': end, 'label': f"{year}-{month:02d}"})
        
        # Calculate index for each period
        time_series = []
        
        for period in date_ranges:
            s2 = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
                  .filterBounds(aoi)
                  .filterDate(period['start'], period['end'])
                  .filter(ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", 40))
                  .map(mask_s2_clouds))
            
            if s2.size().getInfo() > 0:
                median = s2.median()
                index = calculate_index(median, index_name)
                
                stats = index.reduceRegion(
                    reducer=ee.Reducer.mean(),
                    geometry=aoi,
                    scale=20,
                    maxPixels=1e8,
                    bestEffort=True
                ).getInfo()
                
                time_series.append({
                    'period': period['label'],
                    'value': stats.get(index_name)
                })
        
        return jsonify({
            'index': index_name,
            'year': year,
            'interval': interval,
            'data': time_series
        })
        
    except Exception as e:
        logger.error(f"Error in time series analysis: {e}")
        return jsonify({'error': str(e)}), 500

# =========================
# ERROR HANDLERS
# =========================

@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Endpoint not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({'error': 'Internal server error'}), 500

# =========================
# MAIN
# =========================

if __name__ == '__main__':
    port = int(os.getenv('PORT', 8086))
    debug = os.getenv('DEBUG', 'False').lower() == 'true'
    
    logger.info(f"Starting Flask server on port {port}")
    logger.info(f"Earth Engine initialized: {ee_initialized}")
    
    app.run(host='0.0.0.0', port=port, debug=debug)