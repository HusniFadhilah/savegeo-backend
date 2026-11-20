from copy import deepcopy
from unittest.mock import MagicMock, patch

import pytest

from app.services import carbon_pool_service as service
from app.services import carbon_annual_store as store


@pytest.mark.parametrize('year', [2025, 2026])
def test_database_reload_keeps_selected_year_and_regenerates_map_ids(year):
    model = {'name': 'reference_national', 'version': '1', 'algorithm': 'RF', 'description': 'annual',
             'metadata_json': {'method': 'saved_reference_ensemble', 'compatible_reference_datasets': ['SPAWN']}}
    meta = {'year': 2010, 'resolution': 300, 'unit': 'Mg C/ha', 'target_pool': 'aboveground_biomass_carbon',
            'name': 'reference', 'full_name': 'reference', 'description': 'baseline'}
    summary = {'statistics': {'mean': 55.2, 'min': 0, 'max': 100, 'std_dev': 10},
               'area_info': {'calculation_mode': 'clipped_aoi', 'total_carbon_tons': 123}, 'coverage': 95}
    image = MagicMock()
    image.updateMask.return_value = image
    image.clip.return_value = image
    image.getMapId.return_value = {'tile_fetcher': type('Tile', (), {'url_format': 'fresh-map-url'})()}
    roi = MagicMock()
    roi.bounds.return_value = roi
    roi.area.return_value.getInfo.return_value = 2e12
    data = {'aoi': {'west': 95, 'south': -6, 'east': 106, 'north': 6}, 'reference_dataset': 'SPAWN',
            'dataset_year': 2010, 'year': year, 'model_name': model['name'], 'simple_mode': True, 'scale': 2000}
    with patch.object(service, 'ee'), patch.object(service, '_geometry', return_value=roi), \
         patch.object(service, 'get_dataset_meta', return_value=deepcopy(meta)), \
         patch.object(service, 'load_carbon_reference_ee', return_value=image), \
         patch.object(service, 'get_builtin_carbon_model', return_value=model), \
         patch.object(service.config_service, 'get_analysis_defaults', return_value={
             'max_pixels': 1000000000, 'carbon_vis_min': 0, 'carbon_vis_max': 100,
             'carbon_vis_palette': ['green'], 'carbon_co2_factor': 3.67}), \
         patch('app.services.carbon_saved_models.saved_model_image', return_value=(image, {'covariate_year': year})) as predict, \
         patch.object(store, 'load_summary', return_value=deepcopy(summary)), patch.object(store, 'save_summary') as save:
        result = service.analyze_carbon_pool(MagicMock(), data)
    assert predict.call_args.kwargs['analysis_year'] == year
    assert result['model_info']['analysis_year'] == year
    assert result['model_info']['reference_year'] == 2010
    assert result['model_info']['analysis_year_mode'] == 'annual_covariate_inference'
    assert result['model_info']['annual_summary_source'] == 'database'
    assert result['carbon_estimated']['statistics']['mean'] == 55.2
    assert result['carbon_estimated']['tile_url'] == 'fresh-map-url'
    image.reduceRegion.assert_not_called()
    save.assert_not_called()


def test_simple_builtin_reuses_db_summary_for_large_aoi():
    year = 2026
    model = {'name': 'pool_rf_spawn', 'version': '1.0', 'algorithm': 'Random Forest', 'description': 'AOI calibrated',
             'metadata_json': {'builtin': True, 'training_mode': 'reference_raster',
                               'compatible_reference_datasets': ['SPAWN']}}
    meta = {'year': 2010, 'resolution': 1000, 'unit': 'Mg C/ha', 'target_pool': 'aboveground_biomass_carbon',
            'name': 'reference', 'full_name': 'reference', 'description': 'baseline'}
    summary = {'statistics': {'mean': 55.2, 'min': 0, 'max': 100, 'std_dev': 10},
               'area_info': {'calculation_mode': 'clipped_aoi', 'total_carbon_tons': 123}, 'coverage': 95,
               'model_info': {'n_samples': 1200, 'validation_samples': 400,
                              'validation_metrics': {'r2': 0.62, 'rmse': 18.1}}}
    image = MagicMock()
    image.clip.return_value = image
    image.getMapId.return_value = {'tile_fetcher': type('Tile', (), {'url_format': 'fresh-map-url'})()}
    roi = MagicMock()
    roi.bounds.return_value = roi
    roi.area.return_value.getInfo.return_value = 5e12
    data = {'aoi': {'west': 94, 'south': -11, 'east': 142, 'north': 6}, 'reference_dataset': 'SPAWN',
            'dataset_year': 2010, 'year': year, 'model_name': model['name'], 'simple_mode': True, 'scale': 4500}
    with patch.object(service, 'ee'), patch.object(service, '_geometry', return_value=roi), \
         patch.object(service, 'get_dataset_meta', return_value=deepcopy(meta)), \
         patch.object(service, 'load_carbon_reference_ee', return_value=image), \
         patch.object(service, 'get_builtin_carbon_model', return_value=model), \
         patch.object(service.config_service, 'get_analysis_defaults', return_value={
             'max_pixels': 1000000000, 'carbon_vis_min': 0, 'carbon_vis_max': 100,
             'carbon_vis_palette': ['green'], 'carbon_co2_factor': 3.67}), \
         patch.object(service, '_random_forest', return_value=(image, {'validation_metrics': summary['model_info']['validation_metrics']})) as fit, \
         patch('app.services.carbon_annual_store.load_summary', return_value=deepcopy(summary)), \
         patch('app.services.carbon_annual_store.save_summary') as save:
        result = service.analyze_carbon_pool(MagicMock(), data)
    assert fit.call_args.kwargs['allow_large'] is True
    assert fit.call_args.kwargs['cached_model_info'] == summary['model_info']
    assert result['model_info']['analysis_year'] == year
    assert result['model_info']['annual_summary_source'] == 'database'
    assert result['carbon_estimated']['statistics']['mean'] == 55.2
    image.reduceRegion.assert_not_called()
    save.assert_not_called()
