import json
import tomllib
from pathlib import Path

from app.core.version import get_product_version, get_public_version_metadata
from scripts.validate_release_manifest import validate_manifest


def test_product_version_comes_from_pyproject():
    with (Path(__file__).parents[1] / "pyproject.toml").open("rb") as handle:
        assert get_product_version() == tomllib.load(handle)["project"]["version"]


def test_public_version_metadata_has_no_configuration_or_secret_fields(monkeypatch):
    monkeypatch.setenv("SAVEGEO_COMMIT_SHA", "a" * 40)
    metadata = get_public_version_metadata()
    assert metadata["product_version"] == get_product_version()
    assert metadata["commit_sha"] == "a" * 40
    assert "database_url" not in metadata
    assert not any("secret" in key.lower() or "token" in key.lower() for key in metadata)


def test_bootstrap_manifest_allows_unknown_workflow_fields():
    manifest = validate_manifest(Path(__file__).parents[1] / "release" / "v1.0.0.json")
    assert manifest["frontend"]["build_number"] == 1
    assert manifest["frontend"]["commit_sha"] is None


def test_current_release_manifest_matches_product_version():
    version = get_product_version()
    manifest = validate_manifest(Path(__file__).parents[1] / "release" / f"v{version}.json")
    assert manifest["product_version"] == version
    assert manifest["frontend"]["version"] == version
    assert manifest["backend"]["version"] == version


def test_manifest_rejects_bad_sha(tmp_path):
    source = json.loads((Path(__file__).parents[1] / "release" / "v1.0.0.json").read_text(encoding="utf-8"))
    source["backend"]["commit_sha"] = "not-a-sha"
    target = tmp_path / "manifest.json"
    target.write_text(json.dumps(source), encoding="utf-8")
    try:
        validate_manifest(target)
    except ValueError as exc:
        assert "backend.commit_sha" in str(exc)
    else:
        raise AssertionError("invalid SHA was accepted")
