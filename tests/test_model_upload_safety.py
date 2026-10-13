from app.api.routes.admin import _safe_model_stem, _ensure_model_execution_permission
from fastapi import HTTPException
from types import SimpleNamespace
import pytest
from app.repositories import uploaded_model_repo


def test_model_stem_cannot_escape_model_directory():
    stem = _safe_model_stem("..\\..\\outside/model")
    assert stem == "outside_model"
    assert "\\" not in stem
    assert "/" not in stem


def test_model_stem_has_safe_fallback():
    assert _safe_model_stem("...---") == "model"


@pytest.mark.parametrize("name", ["../outside", "..\\outside", "/absolute", "C:\\outside"])
def test_model_lookup_cannot_escape_model_directory(tmp_path, monkeypatch, name):
    root = tmp_path / "models"
    root.mkdir()
    (tmp_path / "outside.pkl").write_bytes(b"untrusted")
    monkeypatch.setattr(uploaded_model_repo, "get_settings", lambda: SimpleNamespace(model_path=root))
    query = SimpleNamespace(first=lambda: None)
    query.filter_by = lambda **_kwargs: query
    db = SimpleNamespace(query=lambda _model: query)
    assert uploaded_model_repo.get_active_model_path(db, "carbon", name) is None


def test_model_lookup_accepts_only_files_under_model_root(tmp_path, monkeypatch):
    root = tmp_path / "models"
    root.mkdir()
    model = root / "valid.pkl"
    model.write_bytes(b"trusted")
    monkeypatch.setattr(uploaded_model_repo, "get_settings", lambda: SimpleNamespace(model_path=root))
    query = SimpleNamespace(first=lambda: None)
    query.filter_by = lambda **_kwargs: query
    db = SimpleNamespace(query=lambda _model: query)
    assert uploaded_model_repo.get_active_model_path(db, "carbon", "valid") == str(model.resolve())


def test_registered_model_outside_root_cannot_be_loaded(tmp_path, monkeypatch):
    root = tmp_path / "models"
    root.mkdir()
    outside = tmp_path / "outside.pkl"
    outside.write_bytes(b"untrusted")
    row = SimpleNamespace(filepath=str(outside), filename="../outside.pkl")
    query = SimpleNamespace(first=lambda: row)
    query.filter_by = lambda **_kwargs: query
    monkeypatch.setattr(uploaded_model_repo, "get_settings", lambda: SimpleNamespace(model_path=root))
    db = SimpleNamespace(query=lambda _model: query)
    assert uploaded_model_repo.get_active_model_path(db, "carbon") is None


def test_catalog_editor_cannot_upload_executable_model():
    editor = SimpleNamespace(role_id=1, role=SimpleNamespace(permissions=[SimpleNamespace(code="model.write")]))
    with pytest.raises(HTTPException) as error:
        _ensure_model_execution_permission(editor)
    assert error.value.status_code == 403
    _ensure_model_execution_permission(SimpleNamespace(role_id=None, role=None))
