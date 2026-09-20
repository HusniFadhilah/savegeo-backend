from app.services import carbon_service


def _dataset_info():
    return {
        "key": "TEST_REFERENCE",
        "name": "Test reference",
        "full_name": "Test reference raster",
        "year": 2020,
        "resolution": 100,
        "unit": "Mg C/ha",
        "target_pool": "aboveground_biomass_carbon",
        "description": "test",
        "provider_type": "external_raster",
    }


def test_reference_load_only_response_never_includes_calculated_values():
    result = carbon_service._reference_load_only_response(
        dataset_info=_dataset_info(),
        dataset_year=2020,
        reference_tile_url="/tiles/{z}/{x}/{y}.png",
        ref_vis_params={"min": 0, "max": 200, "palette": ["000000", "ffffff"]},
    )

    assert result["carbon_estimated"]["statistics"] is None
    assert result["carbon_reference"]["statistics"] is None
    assert result["area_info"]["calculation_area_ha"] is None
    assert result["area_info"]["total_carbon_tons"] is None
    assert result["model_info"]["load_only"] is True


def test_soilgrids_load_only_skips_rest_sampling(monkeypatch):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("REST sampling must not run in load-only mode")

    monkeypatch.setattr(carbon_service.ExternalRasterProvider, "get_stats_for_aoi", fail_if_called)
    result = carbon_service._analyze_external_reference_only(
        data={"aoi": {"west": 100, "south": -2, "east": 101, "north": -1}, "load_only": True},
        dataset_info={**_dataset_info(), "key": "SOILGRIDS_SOC_30CM", "resolution": 250},
        dataset_meta={"provider_type": "external_raster"},
        dataset_year=2017,
        db=None,
    )

    assert result["model_info"]["algorithm"] == "soilgrids_rest"
    assert result["carbon_estimated"]["tile_url"] is None
    assert result["carbon_estimated"]["statistics"] is None


def test_global_soilgrids_load_returns_metadata_instead_of_422():
    result = carbon_service.get_direct_carbon_reference_layer("SOILGRIDS_SOC_30CM", 2017)

    assert result["direct"] is True
    assert result["statistics_available"] is False
    assert result["tile_url"] is None
    assert result["load_note"]


def test_arcgis_aoi_load_only_does_not_initialize_earth_engine(monkeypatch):
    monkeypatch.setattr(
        carbon_service.config_service,
        "get_analysis_defaults",
        lambda _db: {
            "cloud_threshold": 10,
            "carbon_vis_min": 0,
            "carbon_vis_max": 300,
            "carbon_vis_palette": ["000000", "ffffff"],
            "carbon_scale": 300,
            "carbon_co2_factor": 3.67,
            "max_pixels": 1_000_000,
        },
    )
    result = carbon_service.analyze_carbon(
        None,
        {
            "aoi": {"west": 100, "south": -2, "east": 101, "north": -1},
            "year": 2020,
            "dataset_year": 2010,
            "reference_dataset": "WCMC_Carbon_ArcGIS",
            "reference_only": True,
            "load_only": True,
        },
    )

    assert result["model_info"]["algorithm"] == "arcgis_tile"
    assert result["model_info"]["load_only"] is True
    assert result["carbon_reference"]["tile_url"].startswith("/api/arcgis/tiles/")
