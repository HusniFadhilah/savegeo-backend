"""Conservative, shared daily cost guard for paid AI provider calls.

The budget is opt-in and configured by a superadmin. Prices are keyed by the
exact provider/model pair because provider prices change and model aliases are
not interchangeable. Each call reserves its worst-case input/output estimate
before network access; there is deliberately no refund after a request starts.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Any

MICRO_USD = Decimal(1_000_000)
MAX_DAILY_BUDGET_USD = Decimal("2000")  # fits the shared INTEGER counter at micro-dollar resolution
_LOCAL_PROVIDERS = {"ollama", "opencode"}
_CONFIG_KEYS = {"ai.daily_budget_usd", "ai.model_pricing_json"}


class AICostBudgetError(RuntimeError):
    """Safe user-facing failure raised before a provider request is sent."""

    def __init__(self, status_code: int, message: str, retry_after: int = 5) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


def _decimal(value: Any, *, maximum: Decimal) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("AI cost setting is not numeric") from exc
    if not parsed.is_finite() or parsed < 0 or parsed > maximum:
        raise ValueError("AI cost setting is outside the allowed range")
    return parsed


def estimate_cost_micro_usd(
    provider: str,
    model: str,
    request_payload: Any,
    *,
    max_output_tokens: int = 2048,
    pricing: dict[str, Any],
) -> int:
    """Return a conservative micro-USD reservation for one provider request.

    UTF-8 request bytes are used as an upper-bound token estimate. This may
    over-reserve, but it cannot silently undercount the prompt as a tokenizer
    approximation can. Model prices are USD per million tokens.
    """
    if provider in _LOCAL_PROVIDERS and f"{provider}:{model}" not in pricing:
        return 0
    rate = pricing.get(f"{provider}:{model}")
    if not isinstance(rate, dict):
        raise ValueError("Pricing is not configured for this provider/model")
    input_rate = _decimal(rate.get("input_per_million_usd"), maximum=Decimal("1000"))
    output_rate = _decimal(rate.get("output_per_million_usd"), maximum=Decimal("1000"))
    if type(max_output_tokens) is not int or not 0 <= max_output_tokens <= 8192:
        raise ValueError("Invalid output token reservation")
    try:
        encoded = json.dumps(request_payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("AI request cannot be measured") from exc
    input_bytes = len(encoded)
    # USD / 1e6 tokens, converted to micro-USD, is numerically the rate times tokens.
    micro_usd = Decimal(input_bytes) * input_rate + Decimal(max_output_tokens) * output_rate
    return int(micro_usd.to_integral_value(rounding=ROUND_CEILING))


def load_budget_config() -> tuple[Decimal, dict[str, Any]]:
    """Read the admin-managed budget and rates without retaining ORM objects."""
    from app.db.models.system_config import SystemConfig
    from app.db.session import SessionLocal

    try:
        with SessionLocal() as db:
            rows = db.query(SystemConfig).filter(SystemConfig.key.in_(_CONFIG_KEYS)).all()
            values = {row.key: row.value for row in rows}
    except Exception as exc:  # noqa: BLE001 - fail closed when spending cannot be checked
        raise AICostBudgetError(503, "Pemeriksaan plafon biaya AI sedang tidak tersedia.") from exc

    raw_budget = values.get("ai.daily_budget_usd") or "0"
    raw_pricing = values.get("ai.model_pricing_json") or "{}"
    try:
        budget = _decimal(raw_budget, maximum=MAX_DAILY_BUDGET_USD)
        if budget == 0:
            return budget, {}
        pricing = json.loads(raw_pricing)
        if not isinstance(pricing, dict):
            raise ValueError("AI model pricing must be an object")
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise AICostBudgetError(503, "Konfigurasi plafon biaya AI tidak valid.") from exc
    return budget, pricing


def reserve_call(provider: str, model: str, request_payload: Any, *, max_output_tokens: int = 2048) -> int:
    """Atomically reserve one provider call against the global daily budget.

    Returns the reserved amount in micro-USD. A zero budget disables monetary
    accounting; the existing request, input, token, and wall-time limits remain
    active. If accounting is enabled, missing prices fail closed.
    """
    budget, pricing = load_budget_config()
    if budget == 0:
        return 0
    try:
        units = estimate_cost_micro_usd(
            provider, model, request_payload,
            max_output_tokens=max_output_tokens, pricing=pricing,
        )
    except ValueError as exc:
        raise AICostBudgetError(503, "Harga provider/model chatbot belum valid di konfigurasi admin.") from exc
    limit = int((budget * MICRO_USD).to_integral_value(rounding=ROUND_FLOOR))
    if units <= 0:
        return 0
    if units > limit:
        raise AICostBudgetError(429, "Perkiraan biaya permintaan ini melampaui plafon AI harian.", 86400)

    from app.core.shared_rate_limit import consume

    try:
        retry_after = consume("ai:daily-cost:global", limit, 86400, units=units)
    except Exception as exc:  # noqa: BLE001 - a cost cap must not fail open
        raise AICostBudgetError(503, "Pemeriksaan plafon biaya AI sedang tidak tersedia.") from exc
    if retry_after:
        raise AICostBudgetError(429, "Plafon biaya AI harian tercapai.", retry_after)
    return units
