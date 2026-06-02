"""Token -> USD cost estimation for managed sessions.

Estimate at published per-million-token tier rates, plus Anthropic prompt-cache
pricing (cache writes 1.25x input, cache reads 0.10x input). This mirrors the
methodology in the managed-agents-experiment repo so the numbers are comparable.
Rates are estimates; treat as directional, not billing-accurate.
"""
from __future__ import annotations

from dataclasses import dataclass

# $ per 1M tokens (input, output). Extend as new tiers ship.
PRICE: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-opus-4-6": (15.00, 75.00),
    "claude-opus-4-8": (15.00, 75.00),
}
_DEFAULT_PRICE = (1.00, 5.00)


def price_for(model: str) -> tuple[float, float]:
    if model in PRICE:
        return PRICE[model]
    for key, rate in PRICE.items():
        if model.startswith(key):
            return rate
    # family fallbacks
    if "opus" in model:
        return (15.00, 75.00)
    if "sonnet" in model:
        return (3.00, 15.00)
    if "haiku" in model:
        return (1.00, 5.00)
    return _DEFAULT_PRICE


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_creation: int = 0


def estimate_cost(usage: Usage, model: str) -> float:
    pin, pout = price_for(model)
    return (
        usage.input_tokens * pin
        + usage.output_tokens * pout
        + usage.cache_creation * pin * 1.25
        + usage.cache_read * pin * 0.10
    ) / 1e6


def usage_from_session(session_usage) -> Usage:
    """Pull a Usage out of a managed-session usage object (best effort across
    SDK shapes)."""
    if session_usage is None:
        return Usage()

    def g(name, default=0):
        if isinstance(session_usage, dict):
            return session_usage.get(name, default) or default
        return getattr(session_usage, name, default) or default

    cc = g("cache_creation", None)
    cache_creation = 0
    if cc is not None:
        if isinstance(cc, dict):
            cache_creation = (cc.get("ephemeral_5m_input_tokens", 0) or 0) + (
                cc.get("ephemeral_1h_input_tokens", 0) or 0
            )
        else:
            cache_creation = (getattr(cc, "ephemeral_5m_input_tokens", 0) or 0) + (
                getattr(cc, "ephemeral_1h_input_tokens", 0) or 0
            )
    return Usage(
        input_tokens=g("input_tokens"),
        output_tokens=g("output_tokens"),
        cache_read=g("cache_read_input_tokens"),
        cache_creation=cache_creation,
    )
