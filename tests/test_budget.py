#!/usr/bin/env python3
"""Dependency-free tests for the financial circuit breaker (budget.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import budget as b


class FakeLLM:
    def __init__(self, billing=None, name="m"):
        if billing is not None:
            self._billing = billing
        self.model_name = name


def test_billing_defaults():
    assert b.llm_billing(FakeLLM()) == b.BILLING_FIXED
    assert not b.bills_per_token(FakeLLM())
    assert b.llm_billing(FakeLLM(b.BILLING_PAYG)) == b.BILLING_PAYG
    assert b.bills_per_token(FakeLLM(b.BILLING_PAYG))
    assert b.llm_billing(FakeLLM(b.BILLING_LOCAL)) == b.BILLING_LOCAL


def test_mark_billing_bypasses_validation():
    class Strict:
        def __setattr__(self, name, value):
            raise AssertionError("normal setattr must not be used")

    llm = Strict()
    assert b.mark_billing(llm, b.BILLING_PAYG) is llm
    assert b.bills_per_token(llm)


def test_openrouter_free():
    assert b.openrouter_is_free("deepseek/deepseek-chat:free")
    assert b.openrouter_is_free("nvidia/nemotron-3-ultra", "nvidia/nemotron-3-ultra")
    assert not b.openrouter_is_free("openai/gpt-4o")
    assert not b.openrouter_is_free("deepseek/deepseek-chat")


def test_payg_options():
    opts = b.payg_options(True, "deepseek-v4-pro", True, "acme/paid-model",
                          "acme/free:free", extra="x/y, deepseek-v4-pro")
    assert opts == ["x/y", "deepseek-v4-pro", "acme/paid-model"]
    assert b.payg_options(False, "", True, "acme/free:free", "acme/free:free") == []


def test_budget_suspended_is_exception():
    assert issubclass(b.BudgetSuspended, Exception)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("BUDGET_TESTS_OK")
