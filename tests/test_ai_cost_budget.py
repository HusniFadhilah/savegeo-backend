from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.agentic import cost_budget


PRICING = {
    "anthropic:claude-test": {"input_per_million_usd": 2, "output_per_million_usd": 10},
    "openai:gpt-test": {"input_per_million_usd": 2, "output_per_million_usd": 10},
    "openrouter:route/test": {"input_per_million_usd": 2, "output_per_million_usd": 10},
    "deepseek:chat-test": {"input_per_million_usd": 2, "output_per_million_usd": 10},
    "gemini:flash-test": {"input_per_million_usd": 2, "output_per_million_usd": 10},
}


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("anthropic", "claude-test"),
        ("openai", "gpt-test"),
        ("openrouter", "route/test"),
        ("deepseek", "chat-test"),
        ("gemini", "flash-test"),
    ],
)
def test_paid_provider_cost_estimate_uses_model_rates(provider, model):
    payload = {"messages": [{"role": "user", "content": "hello"}]}
    measured_bytes = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    assert cost_budget.estimate_cost_micro_usd(provider, model, payload, pricing=PRICING) == measured_bytes * 2 + 2048 * 10


@pytest.mark.parametrize("provider", ["ollama", "opencode"])
def test_local_provider_has_no_saas_charge_without_rate(provider):
    assert cost_budget.estimate_cost_micro_usd(provider, "local-model", {"prompt": "x"}, pricing={}) == 0


def test_unknown_paid_model_fails_closed_when_pricing_is_missing():
    with pytest.raises(ValueError, match="Pricing is not configured"):
        cost_budget.estimate_cost_micro_usd("openai", "unlisted-model", {"prompt": "x"}, pricing=PRICING)


def test_enabled_budget_fails_closed_if_selected_model_rate_is_missing(monkeypatch):
    monkeypatch.setattr(cost_budget, "load_budget_config", lambda: (Decimal("1"), PRICING))

    with pytest.raises(cost_budget.AICostBudgetError) as error:
        cost_budget.reserve_call("openai", "unlisted-model", {"prompt": "x"})
    assert error.value.status_code == 503


def test_budget_reserves_worst_case_units_in_shared_daily_bucket(monkeypatch):
    calls = {}
    monkeypatch.setattr(cost_budget, "load_budget_config", lambda: (Decimal("1"), PRICING))
    monkeypatch.setattr(
        "app.core.shared_rate_limit.consume",
        lambda key, limit, window, *, units: calls.update(key=key, limit=limit, window=window, units=units) or 0,
    )

    reserved = cost_budget.reserve_call("openai", "gpt-test", {"messages": ["hello"]})

    assert reserved == calls["units"]
    assert calls == {
        "key": "ai:daily-cost:global",
        "limit": 1_000_000,
        "window": 86400,
        "units": reserved,
    }


def test_budget_exhaustion_rejects_before_provider_request(monkeypatch):
    monkeypatch.setattr(cost_budget, "load_budget_config", lambda: (Decimal("0.001"), PRICING))
    monkeypatch.setattr(
        "app.core.shared_rate_limit.consume",
        lambda *_args, **_kwargs: pytest.fail("request larger than cap must be rejected before persistence"),
    )

    with pytest.raises(cost_budget.AICostBudgetError) as error:
        cost_budget.reserve_call("openai", "gpt-test", {"messages": ["hello"]})
    assert error.value.status_code == 429
    assert error.value.retry_after == 86400


def test_shared_budget_exhaustion_returns_retry_after(monkeypatch):
    monkeypatch.setattr(cost_budget, "load_budget_config", lambda: (Decimal("1"), PRICING))
    monkeypatch.setattr("app.core.shared_rate_limit.consume", lambda *_args, **_kwargs: 37)

    with pytest.raises(cost_budget.AICostBudgetError) as error:
        cost_budget.reserve_call("openai", "gpt-test", {"messages": ["hello"]})
    assert error.value.status_code == 429
    assert error.value.retry_after == 37
