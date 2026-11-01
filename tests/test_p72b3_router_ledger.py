#!/usr/bin/env python3
"""P7.2b.3.1 F8: real Router + real CallLedger ambiguous-outcome integration.

Unlike ``test_p04_calls.RouteChainLedgerTests`` (which use a ``FakeRouter`` that
only implements ``authorize``/``charge``), these tests wire the *production*
``Router`` and ``CallLedger`` together so the reservation lifecycle
(ACTIVE -> HELD) and the durable ledger record are exercised end to end.

A deterministic fake provider raises an ambiguous timeout; no network is used.
"""

import sys
import tempfile
import types
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import registry as reg
import router as r
from calls import CallLedger, CallOutcomeUnknown, CONFIRMED, UNKNOWN


def _payg_identity():
    price = reg.ModelPrice(currency="USD",
                           input_per_million=Decimal("1"),
                           output_per_million=Decimal("1"),
                           source="test", updated="test")
    return reg.ModelIdentity(
        provider="deepseek-api", endpoint="deepseek-api",
        model_id="deepseek-v4-pro", family="deepseek",
        credential_profile="deepseek-api", billing=reg.BILLING_PAYG,
        capabilities=frozenset({"text"}), context_window=100000,
        price=price, defined=True)


def _cost(total):
    return reg.CostEstimate("USD", Decimal("0"), total, total,
                            "test", True, False, True)


class RouterLedgerIntegrationTests(unittest.TestCase):
    def _build(self, handle, td, *, max_cost="10", estimate="10"):
        ident = _payg_identity()
        registry = reg.ModelRegistry(models=[ident])
        registry.estimate = lambda identity, i, o: _cost(Decimal(estimate))
        req = r.RouteRequest("programming", complexity="high",
                             task_id="t", run_id="r", tag="DEV",
                             max_tokens=100)
        grants = r.GrantStore()
        grants.approve_task(req, {r.identity_key(ident)},
                            max_cost=Decimal(max_cost))
        ledger = CallLedger(Path(td) / "calls.json")
        router = r.Router(registry, lambda *a: handle, grants=grants,
                          breaker=r.CircuitBreaker(),
                          availability=lambda i: True)
        router.call_ledger = ledger
        return router, req, ident, grants, ledger

    def test_ambiguous_timeout_holds_reservation_and_records_unknown(self):
        calls = {"n": 0}

        class TimeoutHandle:
            def invoke(self, messages):
                calls["n"] += 1
                raise TimeoutError("timed out")

        with tempfile.TemporaryDirectory() as td:
            router, req, ident, grants, ledger = self._build(
                TimeoutHandle(), td)
            with self.assertRaises(CallOutcomeUnknown):
                router.route(req).chain.invoke(["hello"])

            # exactly one submission: ambiguous PAYG is never retried/fallback
            self.assertEqual(calls["n"], 1)

            # durable evidence: one ambiguous UNKNOWN record, no result artifact
            records = ledger.all()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].state, UNKNOWN)
            self.assertIsNone(ledger.result_text(records[0].call_id))

            # reservation moved ACTIVE -> HELD, exposure retained as uncertain
            exp = grants.exposure(req, ident)
            self.assertEqual(exp["reserved"], Decimal("0"))
            self.assertEqual(exp["uncertain"], Decimal("10"))
            self.assertEqual(exp["spent"], Decimal("0"))  # no fabricated charge

            # the held exposure cannot be spent by a fresh authorization
            self.assertIsNone(grants.reserve(req, ident, amount=Decimal("1")))

    def test_confirmed_replay_does_not_charge_twice(self):
        calls = {"n": 0}

        class OkHandle:
            def invoke(self, messages):
                calls["n"] += 1
                return types.SimpleNamespace(content="ok", usage_metadata={})

        with tempfile.TemporaryDirectory() as td:
            router, req, ident, grants, ledger = self._build(OkHandle(), td)
            first = router.route(req).chain.invoke(["hello"])
            self.assertEqual(first.content, "ok")
            self.assertEqual(calls["n"], 1)
            spent_once = grants.exposure(req, ident)["spent"]
            self.assertEqual(spent_once, Decimal("10"))
            self.assertEqual([r.state for r in ledger.all()], [CONFIRMED])

            # A second identical invocation must replay from the ledger: no new
            # provider call and no second charge.
            second = router.route(req).chain.invoke(["hello"])
            self.assertEqual(second.content, "ok")
            self.assertTrue(second.response_metadata.get("replayed"))
            self.assertEqual(calls["n"], 1)
            self.assertEqual(grants.exposure(req, ident)["spent"], spent_once)

    def test_budget_scope_is_process_local_after_resume(self):
        """Characterisation test: budget accounting is process-scoped.

        This documents the *current* limitation (not a guarantee): a resumed
        workflow in a new process gets a fresh ``GrantStore``. The durable
        ``CallLedger`` still remembers the confirmed call, but confirmed and
        uncertain spend are not reconstructed from it. If a caller configured
        ``max_cost`` expecting a workflow-lifetime ceiling, a resume would reset
        it and could allow cumulative spend beyond that ceiling.
        """
        class OkHandle:
            def invoke(self, messages):
                return types.SimpleNamespace(content="ok", usage_metadata={})

        with tempfile.TemporaryDirectory() as td:
            router1, req, ident, grants1, ledger1 = self._build(OkHandle(), td)
            router1.route(req).chain.invoke(["hello"])
            self.assertEqual(grants1.exposure(req, ident)["spent"],
                             Decimal("10"))

            # "process 2": fresh GrantStore, durable ledger reloaded from disk.
            router2, req2, ident2, grants2, ledger2 = self._build(OkHandle(), td)
            self.assertEqual(len(ledger2.all()), 1)          # durable evidence
            self.assertEqual(grants2.exposure(req2, ident2)["spent"],
                             Decimal("0"))                   # reset in-process
            # and it would authorise another full max_cost reservation
            self.assertIsNotNone(
                grants2.reserve(req2, ident2, amount=Decimal("10")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
