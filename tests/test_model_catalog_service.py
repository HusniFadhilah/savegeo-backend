from app.services import model_catalog_service


class _ModelRow:
    def __init__(self, name, metadata):
        self.name = name
        self.metadata_json = metadata

    def to_dict_public(self, include_path=False):
        return {"name": self.name, "metadata_json": dict(self.metadata_json), "format": "onnx"}


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def filter_by(self, **_kwargs):
        return self

    def filter(self, *_args):
        return self

    def order_by(self, *_args):
        return self

    def all(self):
        return self.rows


class _DB:
    def __init__(self, rows):
        self.rows = rows

    def query(self, *_args):
        return _Query(self.rows)


def test_catalog_combines_sources_deduplicates_and_keeps_legacy_models(monkeypatch):
    legacy = _ModelRow("legacy", {"format": "onnx", "compatible_reference_datasets": ["GEDI_L4A_MONTHLY"]})
    builtin = {"name": "builtin", "metadata_json": {"gee_deployable": True}}
    granite = {"name": "granite", "metadata_json": {"gee_deployable": False,
                "compatible_reference_datasets": ["GEDI_L4A_MONTHLY"]}}
    monkeypatch.setattr(model_catalog_service, "builtin_carbon_models", lambda *_a, **_k: [builtin, legacy.to_dict_public()])
    monkeypatch.setattr(model_catalog_service, "granite_biomass_model", lambda: granite)

    result = model_catalog_service.list_models(_DB([legacy]), limit=10)

    assert [model["name"] for model in result["models"]] == ["legacy", "granite", "builtin"]
    assert result["total"] == 3
    assert not result["has_more"]
    assert result["models"][0]["metadata_json"]["compatible_reference_datasets"] == ["GEDI_L4A_MONTHLY"]


def test_catalog_filters_by_dataset_and_paginates_after_combining_sources(monkeypatch):
    builtin = {"name": "b", "metadata_json": {"gee_deployable": True,
                "compatible_reference_datasets": ["GEDI_L4A_MONTHLY"]}}
    incompatible = {"name": "other", "metadata_json": {"compatible_reference_datasets": ["ORNL_BGB"]}}
    monkeypatch.setattr(model_catalog_service, "builtin_carbon_models", lambda *_a, **_k: [builtin, incompatible])
    monkeypatch.setattr(model_catalog_service, "granite_biomass_model", lambda: None)

    page = model_catalog_service.list_models(_DB([]), target_dataset="GEDI_L4A_MONTHLY", offset=0, limit=1)

    assert page["models"] == [builtin]
    assert page["total"] == 1
    assert not page["has_more"]
