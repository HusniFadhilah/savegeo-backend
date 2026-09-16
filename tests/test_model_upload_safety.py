from app.api.routes.admin import _safe_model_stem


def test_model_stem_cannot_escape_model_directory():
    stem = _safe_model_stem("..\\..\\outside/model")
    assert stem == "outside_model"
    assert "\\" not in stem
    assert "/" not in stem


def test_model_stem_has_safe_fallback():
    assert _safe_model_stem("...---") == "model"
