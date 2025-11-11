"""
Prepare training data from Google Earth Engine
"""

import ee
import numpy as np
from typing import Dict, List
import logging

logger = logging.getLogger(__name__)


def calculate_indices(image):
    """Calculate vegetation indices"""
    # NDVI
    ndvi = image.normalizedDifference(['B8', 'B4']).rename('NDVI')
    
    # NDWI
    ndwi = image.normalizedDifference(['B3', 'B8']).rename('NDWI')
    
    # NDMI
    ndmi = image.normalizedDifference(['B8', 'B11']).rename('NDMI')
    
    # EVI
    evi = image.expression(
        '2.5 * ((NIR - RED) / (NIR + 6 * RED - 7.5 * BLUE + 1))',
        {
            'NIR': image.select('B8'),
            'RED': image.select('B4'),
            'BLUE': image.select('B2')
        }
    ).rename('EVI')
    
    # SAVI
    savi = image.expression(
        '((NIR - RED) / (NIR + RED + 0.5)) * 1.5',
        {
            'NIR': image.select('B8'),
            'RED': image.select('B4')
        }
    ).rename('SAVI')
    
    return image.addBands([ndvi, ndwi, ndmi, evi, savi])


def load_carbon_reference_for_training(dataset_name: str, roi: ee.Geometry) -> ee.Image:
    """
    Load carbon reference dataset with proper band naming for training
    
    Args:
        dataset_name: 'WCMC', 'ESA_CCI', 'GEDI', or 'Simard'
        roi: Region of interest (for filtering if needed)
    
    Returns:
        ee.Image with 'agb' band (Aboveground Biomass in Mg/ha)
    """
    
    logger.info(f"Loading reference dataset: {dataset_name}")
    
    if dataset_name == 'WCMC':
        # WCMC Carbon Density
        carbon_ref = ee.ImageCollection("WCMC/biomass_carbon_density/v1_0").first()
        
        # ✅ FIX: Correct band name
        band_names = carbon_ref.bandNames().getInfo()
        logger.info(f"Available bands in WCMC: {band_names}")
        
        # WCMC uses 'carbon_tonnes_per_ha' as band name
        if 'carbon_tonnes_per_ha' in band_names:
            carbon_band = carbon_ref.select('carbon_tonnes_per_ha').rename('agb')
            logger.info("✓ Using 'carbon_tonnes_per_ha' band")
        else:
            raise ValueError(f"Expected band not found. Available: {band_names}")
        
        return carbon_band
    
    elif dataset_name == 'ESA_CCI':
        # ESA CCI Biomass - Use Hansen proxy
        logger.info("Using Hansen Tree Cover as ESA CCI proxy")
        
        hansen = ee.Image('UMD/hansen/global_forest_change_2023_v1_11')
        tree_cover = hansen.select('treecover2000')
        
        # Convert tree cover (0-100%) to biomass estimate
        # Simplified allometric: AGB ≈ tree_cover * 2.5
        biomass = tree_cover.multiply(2.5).rename('agb')
        
        return biomass
    
    elif dataset_name == 'GEDI':
        # GEDI L4B - Not available in public GEE, use Hansen proxy
        logger.warning("GEDI not publicly available, using Hansen proxy")
        return load_carbon_reference_for_training('ESA_CCI', roi)
    
    elif dataset_name == 'Simard':
        # Simard Forest Height - Use Hansen proxy
        logger.info("Using Hansen Tree Cover as Simard proxy")
        
        hansen = ee.Image('UMD/hansen/global_forest_change_2023_v1_11')
        tree_cover = hansen.select('treecover2000')
        
        # Different conversion for height-based estimate
        biomass = tree_cover.multiply(2.0).rename('agb')
        
        return biomass
    
    else:
        raise ValueError(f"Unknown dataset: {dataset_name}. Choose from: WCMC, ESA_CCI, GEDI, Simard")


def sample_training_data(
    roi: ee.Geometry,
    reference_dataset: str = 'WCMC',
    n_samples: int = 10000,
    year: int = 2022,
    scale: int = 250,
    month_range: tuple = (1, 12),
    min_carbon: float = 0.1  # Minimum carbon threshold to avoid zeros
) -> Dict:
    """
    Sample training data from Google Earth Engine
    
    Args:
        roi: Region of interest
        reference_dataset: Reference biomass dataset name
        n_samples: Number of samples to collect
        year: Year for Sentinel-2 imagery
        scale: Spatial resolution in meters
        min_carbon: Minimum carbon value threshold (Mg/ha)
    
    Returns:
        Dict with 'features', 'target', 'feature_names'
    """
    
    logger.info(f"Sampling {n_samples} training points from {reference_dataset}")
    
    # Load reference carbon dataset
    carbon_band = load_carbon_reference_for_training(reference_dataset, roi)
    
    # Load Sentinel-2 (median composite for the year)
    start_month, end_month = month_range
    start_date = f'{year}-{start_month:02d}-01'
    end_date = f'{year}-{end_month:02d}-28'
    
    logger.info(f"Loading Sentinel-2 imagery ({start_date} to {end_date})...")
    
    sen2_collection = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
                      .filterBounds(roi)
                      .filterDate(start_date, end_date)
                      .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 30)))
    
    collection_size = sen2_collection.size().getInfo()
    logger.info(f"Found {collection_size} Sentinel-2 images")
    
    if collection_size == 0:
        raise ValueError(
            f"No Sentinel-2 images found for {year}. "
            "Try: (1) Different year, (2) Larger region, (3) Higher cloud threshold"
        )
    
    sen2 = sen2_collection.median().multiply(0.0001)
    
    # Select spectral bands
    bands = ['B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'B8A', 'B11', 'B12']
    sen2 = sen2.select(bands)
    
    # Calculate indices
    sen2 = calculate_indices(sen2)
    
    # Combine predictors
    predictors = ee.Image.constant(1).rename('constant').addBands(sen2)
    
    # Combine with target
    training_image = predictors.addBands(carbon_band)
    
    # Sample pixels
    logger.info("Sampling pixels from Earth Engine...")
    samples = training_image.sample(
        region=roi,
        scale=scale,
        numPixels=n_samples,
        seed=42,
        geometries=False
    )
    
    # Convert to list and download
    logger.info("Downloading samples...")
    sample_list = samples.toList(n_samples).getInfo()
    logger.info(f"Downloaded {len(sample_list)} samples")
    
    # Extract features and target
    feature_names = ['constant'] + bands + ['NDVI', 'NDWI', 'NDMI', 'EVI', 'SAVI']
    
    X = []
    y = []
    
    skipped_null = 0
    skipped_zero = 0
    skipped_invalid = 0
    
    for sample in sample_list:
        props = sample['properties']
        
        # Check for valid carbon value
        if 'agb' not in props:
            skipped_null += 1
            continue
        
        carbon_val = props['agb']
        
        if carbon_val is None:
            skipped_null += 1
            continue
        
        # Skip very low/zero carbon values
        if carbon_val < min_carbon:
            skipped_zero += 1
            continue
        
        # Extract features
        features = []
        valid = True
        for feat_name in feature_names:
            if feat_name not in props or props[feat_name] is None:
                valid = False
                break
            features.append(props[feat_name])
        
        if not valid:
            skipped_invalid += 1
            continue
        
        X.append(features)
        y.append(carbon_val)
    
    logger.info(f"\nSampling results:")
    logger.info(f"  Valid samples: {len(X)}")
    logger.info(f"  Skipped (null carbon): {skipped_null}")
    logger.info(f"  Skipped (zero/low carbon): {skipped_zero}")
    logger.info(f"  Skipped (invalid features): {skipped_invalid}")
    
    if len(X) < 100:
        raise ValueError(
            f"Insufficient valid samples: {len(X)} (minimum 100 required). "
            f"Try: (1) Larger region, (2) More samples, (3) Lower min_carbon threshold"
        )
    
    X_array = np.array(X)
    y_array = np.array(y)
    
    logger.info(f"\n✓ Training data prepared:")
    logger.info(f"  Shape: {X_array.shape[0]} samples × {X_array.shape[1]} features")
    logger.info(f"  Carbon range: {y_array.min():.2f} - {y_array.max():.2f} Mg/ha")
    logger.info(f"  Carbon mean: {y_array.mean():.2f} Mg/ha")
    logger.info(f"  Carbon median: {np.median(y_array):.2f} Mg/ha")
    
    return {
        'features': X_array,
        'target': y_array,
        'feature_names': feature_names,
        'metadata': {
            'reference_dataset': reference_dataset,
            'year': year,
            'scale': scale,
            'n_samples_requested': n_samples,
            'n_samples_valid': len(X),
            'carbon_stats': {
                'min': float(y_array.min()),
                'max': float(y_array.max()),
                'mean': float(y_array.mean()),
                'median': float(np.median(y_array)),
                'std': float(y_array.std())
            }
        }
    }


if __name__ == '__main__':
    # Test data preparation
    import ee
    
    try:
        ee.Initialize()
        print("✓ Earth Engine initialized")
        
        # Test with small sample
        roi = ee.Geometry.Rectangle([110, -7, 111, -6])  # Small area in Indonesia
        
        print("\nTesting WCMC dataset sampling...")
        data = sample_training_data(
            roi=roi,
            reference_dataset='WCMC',
            n_samples=100,
            year=2022
        )
        
        print(f"\n✓ Test successful!")
        print(f"  Samples: {len(data['features'])}")
        print(f"  Features: {len(data['feature_names'])}")
        print(f"  Carbon range: {data['target'].min():.2f} - {data['target'].max():.2f} Mg/ha")
        
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()