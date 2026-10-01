"""Verify that the configured local Ollama model is reachable from the API runtime.

This is intentionally a small deployment preflight. It always reads Ollama's
local ``/api/tags`` catalogue and checks that the configured model is installed.
Deploy workflows additionally enable a short OpenAI-compatible inference probe
so a deployment verifies that the model can answer, not only that it exists.
"""
from __future__ import annotations

import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - production dependencies include python-dotenv
    load_dotenv = None


def _tags_url(base_url: str) -> str:
    parsed = urlsplit(base_url.strip())
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return urlunsplit((parsed.scheme, parsed.netloc, f"{path}/api/tags", "", ""))


def _chat_url(base_url: str) -> str:
    parsed = urlsplit(base_url.strip())
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1"):
        path = f"{path}/v1" if path else "/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, f"{path}/chat/completions", "", ""))


def check_ollama() -> int:
    if load_dotenv:
        load_dotenv()

    provider = os.getenv("AI_PROVIDER", "anthropic").strip().lower()
    if provider != "ollama":
        print(f"Ollama preflight skipped (AI_PROVIDER={provider or 'unset'}).")
        return 0

    # Keep the preflight aligned with the chatbot's provider default. Set
    # AI_MODEL (or OLLAMA_MODEL) when the server uses a different local model.
    model = os.getenv("AI_MODEL", "").strip() or os.getenv("OLLAMA_MODEL", "").strip() or "llama3.2"

    base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1").strip()
    if not base_url:
        print("OLLAMA_BASE_URL must be set when AI_PROVIDER=ollama.", file=sys.stderr)
        return 1

    url = _tags_url(base_url)
    try:
        import json

        request = Request(url, headers={"Accept": "application/json"})
        with urlopen(request, timeout=10) as response:  # noqa: S310 - URL is deployment configuration
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        print(f"Ollama is not reachable at {base_url}: {exc}", file=sys.stderr)
        return 1

    models = payload.get("models") if isinstance(payload, dict) else None
    installed = {
        str(item.get("name"))
        for item in (models or [])
        if isinstance(item, dict) and item.get("name")
    }
    if model not in installed:
        available = ", ".join(sorted(installed)) or "(none)"
        print(f"Ollama is reachable, but model '{model}' is not installed. Available: {available}", file=sys.stderr)
        return 1

    probe_enabled = os.getenv("OLLAMA_PREFLIGHT_INFERENCE", "").strip().lower() in {"1", "true", "yes", "on"}
    if probe_enabled:
        try:
            timeout_raw = os.getenv("OLLAMA_PREFLIGHT_TIMEOUT_SECONDS", "120").strip()
            timeout = max(5, float(timeout_raw))
        except ValueError:
            timeout = 120
        try:
            probe_payload = json.dumps(
                {
                    "model": model,
                    "messages": [{"role": "user", "content": "Reply with exactly OK."}],
                    "stream": False,
                    "max_tokens": 8,
                    "temperature": 0,
                }
            ).encode("utf-8")
            request = Request(
                _chat_url(base_url),
                data=probe_payload,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL is deployment configuration
                completion = json.load(response)
            choices = completion.get("choices") if isinstance(completion, dict) else None
            content = ((choices or [{}])[0].get("message") or {}).get("content") if choices else None
            if not isinstance(content, str) or not content.strip():
                print(f"Ollama model '{model}' returned no assistant content.", file=sys.stderr)
                return 1
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            print(f"Ollama model '{model}' is installed but inference failed: {exc}", file=sys.stderr)
            return 1
        print(f"Ollama inference probe passed: {model} returned assistant content.")
    else:
        print(f"Ollama preflight passed: {model} reachable via {base_url}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(check_ollama())
