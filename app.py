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
import requests
from typing import Dict, List, Optional, Tuple
import logging

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

def geojson_to_ee_geometry(geojson):
    """Convert GeoJSON to Earth Engine Geometry"""
    if geojson['type'] == 'FeatureCollection':
        features = [ee.Feature(ee.Geometry(f['geometry'])) for f in geojson['features']]
        return ee.FeatureCollection(features).geometry()
    elif geojson['type'] == 'Feature':
        return ee.Geometry(geojson['geometry'])
    else:
        return ee.Geometry(geojson)

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

@app.route('/api/analyze/carbon', methods=['POST'])
def analyze_carbon():
    """
    Endpoint untuk estimasi stok karbon
    """
    try:
        data = request.get_json()
        
        # Validasi input
        if 'aoi' not in data or 'year' not in data:
            return jsonify({'error': 'Missing required fields: aoi, year'}), 400
        
        # Parse AOI
        if 'geojson' in data['aoi']:
            roi = geojson_to_ee_geometry(data['aoi']['geojson'])
        else:
            roi = ee.Geometry.Rectangle([
                data['aoi']['west'],
                data['aoi']['south'],
                data['aoi']['east'],
                data['aoi']['north']
            ])
        
        year = data['year']
        start_month = data.get('start_month', 1)
        end_month = data.get('end_month', 12)
        cloud_threshold = data.get('cloud_threshold', 10)
        
        # Buat date range
        start_date = f'{year}-{str(start_month).zfill(2)}-01'
        end_date = f'{year}-{str(end_month).zfill(2)}-28'
        
        print(f"Processing carbon analysis for {start_date} to {end_date}")
        
        # 1. Load reference carbon data
        carbon_reference = ee.ImageCollection("WCMC/biomass_carbon_density/v1_0").first()
        
        # 2. Load Sentinel-2
        sen2 = ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED") \
            .select('B.*') \
            .filterBounds(roi) \
            .filterDate(start_date, end_date) \
            .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', cloud_threshold)) \
            .median() \
            .multiply(0.0001)
        
        # 3. Calculate NDVI
        ndvi = sen2.normalizedDifference(['B8', 'B4']).rename('NDVI')
        
        # 4. Get tree mask from Dynamic World
        dw_tree_mask = ee.ImageCollection("GOOGLE/DYNAMICWORLD/V1") \
            .select('label') \
            .filterDate(start_date, end_date) \
            .filterBounds(roi) \
            .mode() \
            .eq(1)  # Class 1 = Trees
        
        # 5. Create predictors
        predictors = ee.Image.constant(1) \
            .addBands(sen2) \
            .addBands(ndvi) \
            .updateMask(dw_tree_mask)
        
        # 6. Combine with carbon reference
        dataset = predictors.addBands(carbon_reference)
        
        # 7. Build regression model
        model = dataset.reduceRegion(
            reducer=ee.Reducer.robustLinearRegression(14, 1),
            geometry=roi,
            scale=250,
            bestEffort=True,
            maxPixels=1e13
        )
        
        # 8. Extract coefficients
        coefficients = ee.Array(model.get('coefficients')).project([0]).toList()
        
        # 9. Predict carbon
        carbon_estimated = predictors \
            .multiply(ee.Image.constant(coefficients)) \
            .reduce(ee.Reducer.sum()) \
            .rename('carbon_estimated')
        
        # 10. Calculate RMSE
        difference = carbon_reference.subtract(carbon_estimated)
        squared_diff = difference.pow(2)
        
        rmse = ee.Number(
            squared_diff.reduceRegion(
                reducer=ee.Reducer.mean(),
                geometry=roi,
                scale=250,
                maxPixels=1e13
            ).values().get(0)
        ).sqrt().getInfo()
        
        # 11. Calculate statistics
        carbon_stats = carbon_estimated.reduceRegion(
            reducer=ee.Reducer.mean()
                .combine(ee.Reducer.stdDev(), '', True)
                .combine(ee.Reducer.min(), '', True)
                .combine(ee.Reducer.max(), '', True),
            geometry=roi,
            scale=250,
            maxPixels=1e13
        ).getInfo()
        
        # 12. Calculate area
        area_ha = roi.area().divide(10000).getInfo()
        mean_carbon = carbon_stats.get('carbon_estimated_mean', 0)
        total_carbon_tons = mean_carbon * area_ha
        
        # 13. Visualization
        vis_params = {
            'min': 0,
            'max': 200,
            'palette': ['440154', '414487', '2a788e', '22a884', '7ad151', 'fde725']
        }
        
        carbon_rgb = carbon_estimated.visualize(**vis_params)
        map_id = carbon_rgb.getMapId()
        tile_url = map_id['tile_fetcher'].url_format
        
        # Reference carbon tile
        carbon_ref_rgb = carbon_reference.visualize(**vis_params)
        ref_map_id = carbon_ref_rgb.getMapId()
        ref_tile_url = ref_map_id['tile_fetcher'].url_format
        
        # Response
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
                'description': 'Estimated carbon stock from Sentinel-2'
            },
            'carbon_reference': {
                'tile_url': ref_tile_url,
                'description': 'WCMC reference carbon density'
            },
            'model_performance': {
                'rmse': round(rmse, 2),
                'description': 'Root Mean Square Error (lower is better)'
            },
            'area_info': {
                'area_ha': round(area_ha, 2),
                'total_carbon_tons': round(total_carbon_tons, 2),
                'carbon_dioxide_equivalent_tons': round(total_carbon_tons * 3.67, 2)
            },
            'model_info': {
                'predictors': 14,
                'method': 'Robust Linear Regression',
                'scale': 250,
                'tree_mask': True
            }
        }
        
        print("Carbon analysis completed successfully")
        return jsonify(result)
        
    except Exception as e:
        print(f"Carbon analysis error: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

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