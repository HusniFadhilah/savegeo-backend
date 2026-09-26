"""Verify that the configured local Ollama model is reachable from the API runtime.

This is intentionally a small deployment preflight. It does not send a prompt
or load a model; it only reads Ollama's local ``/api/tags`` catalogue and
checks that the configured model is installed.
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
        request = Request(url, headers={"Accept": "application/json"})
        with urlopen(request, timeout=10) as response:  # noqa: S310 - URL is deployment configuration
            import json

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

    print(f"Ollama preflight passed: {model} reachable via {base_url}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(check_ollama())
