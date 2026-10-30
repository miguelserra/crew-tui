"""Financial circuit-breaker primitives (stdlib only).

The breaker needs an explicit billing class per constructed model so that
subscriptions/quota (OpenCode Go, Gemini Plus, free tiers) are never mistaken
for pay-as-you-go endpoints. The Model Registry is the source of truth; this
module keeps only the low-level breaker primitives.

Nothing here performs I/O, so it is safe to import in dependency-free tests.
"""

from __future__ import annotations

from typing import Any

BILLING_LOCAL = "local"
BILLING_FREE = "free_quota"
BILLING_SUBSCRIPTION = "subscription"
BILLING_FIXED = BILLING_SUBSCRIPTION  # backward-compatible alias
BILLING_PAYG = "payg"


class BudgetSuspended(Exception):
    """Raised when a pay-as-you-go model is needed without explicit consent.

    Raised by the pool *outside* the LangChain ``with_fallbacks`` chains (which
    are built without pay-as-you-go models unless authorized), so a free-tier
    429 can never drain the balance silently.
    """


def llm_billing(llm: Any) -> str:
    return getattr(llm, "_billing", BILLING_SUBSCRIPTION)


def bills_per_token(llm: Any) -> bool:
    return llm_billing(llm) == BILLING_PAYG


def mark_billing(llm: Any, kind: str) -> Any:
    """Attach a billing class without tripping pydantic's field validation."""
    if llm is not None:
        try:
            object.__setattr__(llm, "_billing", kind)
        except Exception:
            pass
    return llm


def openrouter_is_free(model: str, free_model: str = "") -> bool:
    """Legacy helper; the Model Registry owns OpenRouter billing classification."""
    m = (model or "").strip().lower()
    return bool(m) and (m.endswith(":free") or m == (free_model or "").strip().lower())


def payg_options(deepseek_enabled: bool, deepseek_model: str,
                 openrouter_enabled: bool, openrouter_model: str,
                 openrouter_free_model: str = "", extra: str = "") -> list[str]:
    """Models a user may authorize at the breaker modal."""
    out: list[str] = []
    for raw in (extra or "").split(","):
        m = raw.strip()
        if m and m not in out:
            out.append(m)
    if deepseek_enabled and deepseek_model and deepseek_model not in out:
        out.append(deepseek_model)
    if (openrouter_enabled and openrouter_model
            and not openrouter_is_free(openrouter_model, openrouter_free_model)
            and openrouter_model not in out):
        out.append(openrouter_model)
    return out
