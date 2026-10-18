"""AI provider health, Ollama model discovery and per-request model selection.

Everything that reaches the network goes through an injectable ``http`` callable
so it can be tested offline. Errors are reported as short codes and fixed
messages: raw exception text can contain internal hostnames or API keys and must
never reach the browser.
"""
from __future__ import annotations

import hashlib
import ipaddress
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

HttpCall = Callable[..., "tuple[int, Any]"]

HEALTH_TTL_SECONDS = 30.0
DEFAULT_TIMEOUT_SECONDS = 5.0

# provider -> (config key holding the API key, probe URL). Probes are free,
# read-only "who am I / list models" calls so a health check never spends tokens.
_CLOUD_PROBES: dict[str, tuple[str, str]] = {
    "openrouter": ("openrouter_api_key", "https://openrouter.ai/api/v1/auth/key"),
    "openai": ("openai_api_key", "https://api.openai.com/v1/models"),
    "deepseek": ("deepseek_api_key", "https://api.deepseek.com/models"),
    "anthropic": ("anthropic_api_key", "https://api.anthropic.com/v1/models"),
    "gemini": ("gemini_api_key", "https://generativelanguage.googleapis.com/v1beta/models"),
}

_MESSAGES = {
    "ok": "Siap digunakan.",
    "unconfigured": "Provider belum dikonfigurasi.",
    "unreachable": "Server provider tidak dapat dijangkau.",
    "timeout": "Server provider tidak merespons tepat waktu.",
    "auth_failed": "Kredensial provider ditolak.",
    "rate_limited": "Kuota provider tercapai; coba lagi nanti.",
    "model_missing": "Model belum terpasang pada server provider.",
    "invalid_response": "Respons provider tidak dikenali.",
    "http_error": "Provider mengembalikan kesalahan.",
    "invalid_url": "URL provider tidak valid.",
    "unknown_provider": "Provider tidak dikenal.",
}


class SelectionNotAllowed(ValueError):
    """The requested provider/model pair is not on the admin allowlist."""


# ── URL handling ─────────────────────────────────────────────────────────────

def validate_provider_url(url: str) -> str:
    """Return ``url`` without a trailing slash, or raise ``ValueError``.

    Admin-configured URLs are fetched server-side, so reject anything that is not
    plain http(s), carries credentials, or targets link-local/unspecified
    addresses (cloud metadata endpoints). Loopback, private and public hosts stay
    allowed because Ollama normally runs on the same host or LAN.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("empty url")
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("unsupported url")
    if parts.username or parts.password:
        raise ValueError("credentials in url")
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        address = None
    if address is not None and (address.is_link_local or address.is_unspecified or address.is_multicast):
        raise ValueError("blocked address")
    return url.strip().rstrip("/")


def normalize_ollama_urls(base_url: str) -> tuple[str, str]:
    """Return ``(openai_compatible_base, native_root)`` for a configured Ollama URL."""
    parts = urlsplit(base_url.strip())
    path = parts.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    root = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    return f"{root}/v1", root


# ── HTTP ─────────────────────────────────────────────────────────────────────

def _default_http(method: str, url: str, *, headers: dict | None = None, json_body: Any = None,
                  timeout: float = DEFAULT_TIMEOUT_SECONDS) -> tuple[int, Any]:
    import requests

    response = requests.request(
        method, url, headers=headers, json=json_body, timeout=timeout, allow_redirects=False
    )
    try:
        payload: Any = response.json()
    except ValueError:
        payload = None
    return response.status_code, payload


def _error_code(exc: Exception) -> str:
    try:
        import requests

        if isinstance(exc, requests.Timeout):
            return "timeout"
    except ImportError:  # pragma: no cover
        pass
    return "timeout" if isinstance(exc, TimeoutError) else "unreachable"


# ── Ollama ───────────────────────────────────────────────────────────────────

def list_ollama_models(base_url: str, http: HttpCall | None = None,
                       timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Probe ``/api/tags``. Never raises; failures come back as ``error`` codes."""
    result: dict[str, Any] = {"reachable": False, "models": [], "latency_ms": None, "error": None}
    try:
        _, root = normalize_ollama_urls(validate_provider_url(base_url))
    except ValueError:
        result["error"] = "invalid_url"
        return result

    call = http or _default_http
    started = time.monotonic()
    try:
        status, payload = call("GET", f"{root}/api/tags", timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - mapped to a code; raw text may contain hostnames
        result["error"] = _error_code(exc)
        return result
    result["latency_ms"] = int((time.monotonic() - started) * 1000)

    if status in (401, 403):
        result["error"] = "auth_failed"
        return result
    if status != 200:
        result["error"] = "http_error"
        return result
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        result["error"] = "invalid_response"
        return result

    cleaned = []
    for item in models:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
            continue
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        cleaned.append(
            {
                "name": item["name"],
                "size_bytes": item["size"] if isinstance(item.get("size"), int) else None,
                "parameter_size": details.get("parameter_size") if isinstance(details.get("parameter_size"), str) else None,
                "family": details.get("family") if isinstance(details.get("family"), str) else None,
            }
        )
    result["reachable"] = True
    result["models"] = sorted(cleaned, key=lambda m: m["name"])
    return result


def _ollama_supports_tools(root: str, model: str, http: HttpCall, timeout: float) -> bool | None:
    try:
        status, payload = http("POST", f"{root}/api/show", json_body={"model": model}, timeout=timeout)
    except Exception:  # noqa: BLE001 - capability is best-effort
        return None
    if status != 200 or not isinstance(payload, dict) or not isinstance(payload.get("capabilities"), list):
        return None
    return "tools" in payload["capabilities"]


# ── Health ───────────────────────────────────────────────────────────────────

_cache: dict[tuple, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def clear_health_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _health(provider: str, model: str, status: str, **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "status": status,
        "configured": status != "unconfigured",
        "reachable": status in ("ok", "auth_failed", "rate_limited", "model_missing"),
        "model_available": None,
        "tool_calling": None,
        "latency_ms": None,
        "available_models": [],
        "message": _MESSAGES.get(status, _MESSAGES["http_error"]),
        "checked_at": int(time.time()),
    }
    data.update(extra)
    return data


def _cache_key(provider: str, cfg: dict, model: str) -> tuple:
    if provider == "ollama":
        scope = str(cfg.get("ollama_base_url") or "")
    elif provider == "opencode":
        scope = str(cfg.get("opencode_base_url") or "")
    else:
        key_field = _CLOUD_PROBES.get(provider, ("", ""))[0]
        scope = hashlib.sha256(str(cfg.get(key_field) or "").encode()).hexdigest()[:12]
    return (provider, scope, model)


def check_provider_health(provider: str, cfg: dict, model: str, http: HttpCall | None = None,
                          force: bool = False, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Actively probe ``provider``. Results (including failures) are cached briefly."""
    provider = (provider or "").lower()
    if provider not in _CLOUD_PROBES and provider not in ("ollama", "opencode"):
        return _health(provider, model, "unknown_provider")

    key = _cache_key(provider, cfg, model)
    now = time.monotonic()
    if not force:
        with _cache_lock:
            hit = _cache.get(key)
        if hit and now - hit[0] < HEALTH_TTL_SECONDS:
            return dict(hit[1])

    call = http or _default_http
    result = _probe(provider, cfg, model, call, timeout)
    with _cache_lock:
        _cache[key] = (now, result)
    return dict(result)


def peek_cached_health(provider: str, cfg: dict, model: str) -> dict[str, Any] | None:
    """Return a still-fresh cached result without touching the network."""
    key = _cache_key((provider or "").lower(), cfg, model)
    with _cache_lock:
        hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < HEALTH_TTL_SECONDS:
        return dict(hit[1])
    return None


def mark_unavailable(provider: str, cfg: dict, model: str, status: str = "unreachable") -> None:
    """Record a failure seen during a real call so the next requests skip this provider briefly."""
    provider = (provider or "").lower()
    with _cache_lock:
        _cache[_cache_key(provider, cfg, model)] = (time.monotonic(), _health(provider, model, status))


def _probe(provider: str, cfg: dict, model: str, call: HttpCall, timeout: float) -> dict[str, Any]:
    if provider == "ollama":
        base = str(cfg.get("ollama_base_url") or "").strip()
        if not base:
            return _health(provider, model, "unconfigured")
        catalog = list_ollama_models(base, http=call, timeout=timeout)
        if catalog["error"]:
            return _health(provider, model, catalog["error"], latency_ms=catalog["latency_ms"])
        names = [m["name"] for m in catalog["models"]]
        if model and model not in names:
            return _health(provider, model, "model_missing", model_available=False,
                           latency_ms=catalog["latency_ms"], available_models=names)
        _, root = normalize_ollama_urls(validate_provider_url(base))
        tools = _ollama_supports_tools(root, model, call, timeout) if model else None
        return _health(provider, model, "ok", model_available=True if model else None, tool_calling=tools,
                       latency_ms=catalog["latency_ms"], available_models=names)

    if provider == "opencode":
        base = str(cfg.get("opencode_base_url") or "").strip()
        try:
            base = validate_provider_url(base)
        except ValueError:
            return _health(provider, model, "invalid_url" if base else "unconfigured")
        return _timed_get(provider, model, f"{base}/models", {}, call, timeout, tool_calling=None)

    key_field, url = _CLOUD_PROBES[provider]
    api_key = str(cfg.get(key_field) or "").strip()
    if not api_key:
        return _health(provider, model, "unconfigured")
    if provider == "anthropic":
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    elif provider == "gemini":
        headers = {"x-goog-api-key": api_key}
    else:
        headers = {"Authorization": f"Bearer {api_key}"}
    return _timed_get(provider, model, url, headers, call, timeout, tool_calling=True)


def _timed_get(provider: str, model: str, url: str, headers: dict, call: HttpCall, timeout: float,
               tool_calling: bool | None) -> dict[str, Any]:
    started = time.monotonic()
    try:
        status, _payload = call("GET", url, headers=headers, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - mapped to a code; raw text may contain URLs/keys
        return _health(provider, model, _error_code(exc))
    latency = int((time.monotonic() - started) * 1000)
    if status == 200:
        return _health(provider, model, "ok", tool_calling=tool_calling, latency_ms=latency)
    if status in (401, 403):
        return _health(provider, model, "auth_failed", latency_ms=latency)
    if status == 429:
        return _health(provider, model, "rate_limited", latency_ms=latency)
    return _health(provider, model, "http_error", latency_ms=latency)


# ── Selection & fallback ─────────────────────────────────────────────────────

def _allowed_pairs(cfg: dict) -> set[tuple[str, str]]:
    pairs = {(str(cfg.get("provider") or "").lower(), str(cfg.get("model") or ""))}
    for item in cfg.get("allowed_models") or []:
        if isinstance(item, dict) and isinstance(item.get("provider"), str) and isinstance(item.get("model"), str):
            pairs.add((item["provider"].lower(), item["model"]))
    return pairs


def resolve_selection(cfg: dict, requested: Any) -> tuple[str, str]:
    """Return the ``(provider, model)`` to use; reject anything off the allowlist."""
    default = (str(cfg.get("provider") or "").lower(), str(cfg.get("model") or ""))
    if requested is None:
        return default
    if (
        not isinstance(requested, dict)
        or not isinstance(requested.get("provider"), str)
        or not isinstance(requested.get("model"), str)
    ):
        raise SelectionNotAllowed("Pilihan provider/model tidak valid.")
    pair = (requested["provider"].lower(), requested["model"])
    if pair not in _allowed_pairs(cfg):
        raise SelectionNotAllowed("Provider/model tidak diizinkan oleh administrator.")
    return pair


def fallback_selection(cfg: dict, primary: tuple[str, str]) -> tuple[str, str] | None:
    provider = str(cfg.get("fallback_provider") or "").strip().lower()
    if not provider:
        return None
    pair = (provider, str(cfg.get("fallback_model") or ""))
    if pair == primary or pair not in _allowed_pairs(cfg):
        return None
    return pair


_AVAILABILITY_TOKENS = (
    "503", "502", "504", "529", "unavailable", "overloaded", "429", "rate limit", "rate_limit",
    "quota", "timed out", "timeout", "connection error", "connection refused", "connect",
)


def is_availability_error(exc: BaseException) -> bool:
    """True for outages/rate limits where trying another provider can help."""
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    text = str(exc).lower()
    return any(token in text for token in _AVAILABILITY_TOKENS)


# ── Admin config validation ──────────────────────────────────────────────────

KNOWN_PROVIDERS = frozenset(_CLOUD_PROBES) | {"ollama", "opencode"}
_URL_KEYS = ("ai.ollama_base_url", "ai.custom_base_url", "ai.opencode_base_url")


def validate_ai_config_update(updates: dict[str, Any], current: Callable[[str], str],
                              http: HttpCall | None = None) -> tuple[dict[str, str], list[str]]:
    """Check a batch of ``ai.*`` settings before they are stored.

    Returns ``(errors_by_key, warnings)``. A key in ``errors`` must not be saved.
    An unreachable Ollama only warns, so an admin can pre-configure a server that
    is still starting; a reachable one that lacks the model is a hard error.
    """
    import json

    errors: dict[str, str] = {}
    warnings: list[str] = []

    for key in _URL_KEYS:
        value = updates.get(key)
        if isinstance(value, str) and value.strip():
            try:
                validate_provider_url(value)
            except ValueError:
                errors[key] = "URL tidak valid atau tidak diizinkan (hanya http/https, tanpa kredensial)."

    for key in ("ai.provider", "ai.fallback_provider"):
        value = updates.get(key)
        if isinstance(value, str) and value.strip() and value.strip().lower() not in KNOWN_PROVIDERS:
            errors[key] = "Provider tidak dikenal."

    raw_allowed = updates.get("ai.allowed_models")
    if isinstance(raw_allowed, str) and raw_allowed.strip():
        valid = False
        try:
            parsed = json.loads(raw_allowed)
            valid = (
                isinstance(parsed, list)
                and all(
                    isinstance(i, dict)
                    and isinstance(i.get("provider"), str)
                    and i["provider"].lower() in KNOWN_PROVIDERS
                    and isinstance(i.get("model"), str)
                    for i in parsed
                )
            )
        except ValueError:
            valid = False
        if not valid:
            errors["ai.allowed_models"] = (
                'Harus berupa JSON array berisi {"provider": ..., "model": ...} dengan provider yang dikenal.'
            )

    def effective(key: str) -> str:
        value = updates.get(key)
        return (value if isinstance(value, str) else current(key) or "").strip()

    if any(k in updates for k in ("ai.provider", "ai.model", "ai.ollama_base_url")) and not (
        {"ai.provider", "ai.model", "ai.ollama_base_url"} & set(errors)
    ):
        provider, model = effective("ai.provider").lower(), effective("ai.model")
        if provider == "ollama" and model:
            catalog = list_ollama_models(effective("ai.ollama_base_url"), http=http)
            if catalog["error"]:
                warnings.append(f"Ollama belum diverifikasi: {_MESSAGES.get(catalog['error'], _MESSAGES['http_error'])}")
            else:
                names = [m["name"] for m in catalog["models"]]
                if model not in names:
                    message = f"Model '{model}' belum terpasang di Ollama. Tersedia: {', '.join(names) or '(kosong)'}."
                    for key in ("ai.provider", "ai.model"):
                        if key in updates:
                            errors[key] = message
    return errors, warnings


# ── User-facing catalog ──────────────────────────────────────────────────────

PROVIDER_LABELS = {
    "anthropic": "Anthropic Claude", "openai": "OpenAI", "openrouter": "OpenRouter",
    "deepseek": "DeepSeek", "gemini": "Google Gemini", "ollama": "Ollama (lokal)", "opencode": "OpenCode",
}


def public_catalog(cfg: dict, http: HttpCall | None = None) -> dict[str, Any]:
    """Allowed provider/model choices with live status, safe to show to any signed-in user.

    Deliberately omits base URLs, keys and raw errors.
    """
    from concurrent.futures import ThreadPoolExecutor

    default = (str(cfg.get("provider") or "").lower(), str(cfg.get("model") or ""))
    pairs: list[tuple[str, str]] = [default]
    for item in cfg.get("allowed_models") or []:
        if isinstance(item, dict) and isinstance(item.get("provider"), str) and isinstance(item.get("model"), str):
            pair = (item["provider"].lower(), item["model"])
            if pair not in pairs:
                pairs.append(pair)
    pairs = pairs[:10]
    fallback = fallback_selection(cfg, default)

    with ThreadPoolExecutor(max_workers=min(4, len(pairs))) as pool:
        healths = list(pool.map(lambda pair: check_provider_health(pair[0], cfg, pair[1], http=http), pairs))

    entries = []
    for (provider, model), health in zip(pairs, healths):
        entries.append(
            {
                "id": f"{provider}:{model}",
                "provider": provider,
                "label": PROVIDER_LABELS.get(provider, provider),
                "model": model,
                "is_default": (provider, model) == default,
                "is_fallback": fallback == (provider, model),
                "status": health["status"],
                "available": health["status"] == "ok",
                "tool_calling": health.get("tool_calling"),
                "latency_ms": health.get("latency_ms"),
                "message": health["message"],
            }
        )
    return {
        "active": {"provider": default[0], "model": default[1]},
        "selectable": len(entries) > 1,
        "fallback": {"provider": fallback[0], "model": fallback[1]} if fallback else None,
        "providers": entries,
    }
