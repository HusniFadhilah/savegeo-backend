from __future__ import annotations

import sys
import types
from types import SimpleNamespace

from app.agentic import agentic_ai
from app.agentic.agentic_ai import _call_openai_compat


def test_ollama_uses_openai_compatible_base_url_and_local_dummy_key(monkeypatch):
    calls: dict[str, object] = {}

    class FakeCompletions:
        def create(self, **kwargs):
            calls["request"] = kwargs
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")]
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            calls["client"] = kwargs
            self.chat = SimpleNamespace(completions=FakeCompletions())

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    result = _call_openai_compat(
        "ollama",
        "qwen3:8b",
        "hello",
        "http://host.docker.internal:11434/v1",
    )

    assert result == "ok"
    assert calls["client"] == {
        "api_key": "ollama",
        "base_url": "http://host.docker.internal:11434/v1",
    }
    request = calls["request"]
    assert isinstance(request, dict)
    assert request["model"] == "qwen3:8b"
    assert request["messages"][-1]["content"][0]["text"] == "hello"
    assert request["reasoning_effort"] == "none"


def test_geoai_dispatches_tool_calling_to_ollama(monkeypatch):
    calls: dict[str, object] = {}

    monkeypatch.setattr(
        agentic_ai,
        "_get_ai_config",
        lambda: {
            "provider": "ollama",
            "model": "qwen3:8b",
            "custom_base_url": "",
            "ollama_base_url": "http://host.docker.internal:11434/v1",
        },
    )

    def fake_tool_call(api_key, model, user_input, base_url, history, tool_ctx):
        calls.update(api_key=api_key, model=model, base_url=base_url)
        return '{"message":"jawaban lokal","actions":[],"warnings":[],"cards":[]}'

    monkeypatch.setattr(agentic_ai, "_call_openai_compat_tools", fake_tool_call)

    response = agentic_ai.geoai_with_ai("apa kabar?", {}, db=None)

    assert response["message"] == "jawaban lokal"
    assert calls == {
        "api_key": "ollama",
        "model": "qwen3:8b",
        "base_url": "http://host.docker.internal:11434/v1",
    }
