from __future__ import annotations

import json
from io import BytesIO

from scripts import check_ollama


class _Response(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_check_ollama_accepts_configured_model(monkeypatch, capsys):
    monkeypatch.setenv("AI_PROVIDER", "ollama")
    monkeypatch.setenv("AI_MODEL", "qwen3:8b")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434/v1")
    monkeypatch.setattr(
        check_ollama,
        "urlopen",
        lambda request, timeout: _Response(json.dumps({"models": [{"name": "qwen3:8b"}]}).encode()),
    )

    assert check_ollama.check_ollama() == 0
    assert "preflight passed" in capsys.readouterr().out


def test_check_ollama_rejects_missing_model(monkeypatch, capsys):
    monkeypatch.setenv("AI_PROVIDER", "ollama")
    monkeypatch.setenv("AI_MODEL", "missing")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setattr(
        check_ollama,
        "urlopen",
        lambda request, timeout: _Response(json.dumps({"models": [{"name": "qwen3:8b"}]}).encode()),
    )

    assert check_ollama.check_ollama() == 1
    assert "not installed" in capsys.readouterr().err
