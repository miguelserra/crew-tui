#!/usr/bin/env python3
"""P7.2b.3 V01: PAYG budget accounting integrity regression tests.

These tests lock down the accounting model introduced for P7.2b.3:

* estimated / reserved / uncertain / confirmed spend / overrun are distinct;
* commit and release are idempotent;
* commit-after-release is rejected;
* actual cost is never truncated to the reservation;
* confirmed overspend blocks further authorization;
* ambiguous outcomes keep exposure as uncertain (never silently released);
* the compatibility authorize() lifecycle stays safe.
"""

import sys
import types
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import registry as reg
import router as r


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


class GrantAccountingTests(unittest.TestCase):
    def setUp(self):
        self.ident = _payg_identity()
        self.req = r.RouteRequest("programming", task_id="t", run_id="r")

    def _store(self, max_cost):
        grants = r.GrantStore()
        grants.approve_task(self.req, {r.identity_key(self.ident)},
                            max_cost=Decimal(str(max_cost)))
        return grants

    def test_actual_cost_greater_than_reservation_records_overrun(self):
        grants = self._store("10")
        reservation = grants.reserve(self.req, self.ident, amount=Decimal("6"))
        self.assertIsNotNone(reservation)
        grants.commit(reservation, _cost(Decimal("9")))
        exp = grants.exposure(self.req, self.ident)
        self.assertEqual(exp["spent"], Decimal("9"))       # actual, not truncated
        self.assertEqual(exp["overrun"], Decimal("3"))     # 9 - 6
        self.assertEqual(exp["reserved"], Decimal("0"))
        self.assertEqual(exp["uncertain"], Decimal("0"))

    def test_confirmed_overspend_blocks_further_authorization(self):
        grants = self._store("10")
        reservation = grants.reserve(self.req, self.ident, amount=Decimal("6"))
        grants.commit(reservation, _cost(Decimal("12")))  # actual > max_cost
        self.assertFalse(grants.allows(self.req, self.ident))
        self.assertIsNone(grants.reserve(self.req, self.ident, amount=Decimal("1")))

    def test_duplicate_commit_is_idempotent(self):
        grants = self._store("10")
        reservation = grants.reserve(self.req, self.ident, amount=Decimal("6"))
        grants.commit(reservation, _cost(Decimal("4")))
        grants.commit(reservation, _cost(Decimal("4")))
        self.assertEqual(grants.exposure(self.req, self.ident)["spent"],
                         Decimal("4"))

    def test_duplicate_release_is_idempotent(self):
        grants = self._store("10")
        reservation = grants.reserve(self.req, self.ident, amount=Decimal("6"))
        grants.release(reservation)
        grants.release(reservation)
        self.assertEqual(grants.exposure(self.req, self.ident)["reserved"],
                         Decimal("0"))
        self.assertIsNotNone(grants.reserve(self.req, self.ident,
                                            amount=Decimal("6")))

    def test_commit_after_release_is_rejected(self):
        grants = self._store("10")
        reservation = grants.reserve(self.req, self.ident, amount=Decimal("6"))
        grants.release(reservation)
        with self.assertRaises(r.GrantAccountingError):
            grants.commit(reservation, _cost(Decimal("4")))

    def test_ambiguous_hold_moves_reserved_to_uncertain(self):
        grants = self._store("10")
        reservation = grants.reserve(self.req, self.ident, amount=Decimal("6"))
        grants.hold(reservation)
        exp = grants.exposure(self.req, self.ident)
        self.assertEqual(exp["reserved"], Decimal("0"))
        self.assertEqual(exp["uncertain"], Decimal("6"))
        # uncertain exposure still blocks re-authorization of the same budget
        self.assertIsNone(grants.reserve(self.req, self.ident, amount=Decimal("5")))

    def test_release_of_held_reservation_requires_explicit_reconciliation(self):
        grants = self._store("10")
        reservation = grants.reserve(self.req, self.ident, amount=Decimal("6"))
        grants.hold(reservation)
        grants.release(reservation)  # explicit manual resolution
        exp = grants.exposure(self.req, self.ident)
        self.assertEqual(exp["uncertain"], Decimal("0"))
        self.assertIsNotNone(grants.reserve(self.req, self.ident,
                                            amount=Decimal("6")))

    def test_technical_failure_release_returns_budget(self):
        grants = self._store("10")
        reservation = grants.reserve(self.req, self.ident, amount=Decimal("6"))
        grants.release(reservation)
        self.assertIsNotNone(grants.reserve(self.req, self.ident,
                                            amount=Decimal("6")))


class RouterAmbiguousExposureTests(unittest.TestCase):
    def test_routechain_holds_reservation_on_ambiguous_outcome(self):
        ident = _payg_identity()
        registry = reg.ModelRegistry(models=[ident])
        registry.estimate = lambda identity, i, o: _cost(Decimal("4"))
        req = r.RouteRequest("programming", complexity="high",
                             task_id="t", run_id="r", max_tokens=100)
        grants = r.GrantStore()
        grants.approve_task(req, {r.identity_key(ident)},
                            max_cost=Decimal("10"))

        class TimeoutHandle:
            def invoke(self, messages):
                raise TimeoutError("timed out")

        def provider(identity, tag, max_tokens, thinking):
            return TimeoutHandle()

        router = r.Router(registry, provider, grants=grants,
                          breaker=r.CircuitBreaker(),
                          availability=lambda i: True)
        with self.assertRaises(Exception):
            router.route(req).chain.invoke([])
        exp = grants.exposure(req, ident)
        # timeout is ambiguous: the submitted request may have been charged, so
        # the exposure must be retained as uncertain, not zeroed.
        self.assertEqual(exp["uncertain"], Decimal("4"))
        self.assertEqual(exp["spent"], Decimal("0"))


class AuthorizeLifecycleTests(unittest.TestCase):
    def test_authorize_is_idempotent_per_candidate(self):
        ident = _payg_identity()
        registry = reg.ModelRegistry(models=[ident])
        registry.estimate = lambda identity, i, o: _cost(Decimal("4"))
        req = r.RouteRequest("programming", complexity="high",
                             task_id="t", run_id="r", max_tokens=100)
        grants = r.GrantStore()
        grants.approve_task(req, {r.identity_key(ident)},
                            max_cost=Decimal("10"))
        router = r.Router(registry, lambda *a: None, grants=grants)
        candidate = r.RouteCandidate(ident, registry.ref_for_identity(ident), None)
        self.assertTrue(router.authorize(req, candidate))
        self.assertTrue(router.authorize(req, candidate))  # idempotent
        self.assertEqual(grants.exposure(req, ident)["reserved"], Decimal("4"))


class HeldReservationReleaseTests(unittest.TestCase):
    """F1: a HELD (ambiguous) reservation must survive a later definitive failure."""

    def _build(self, handle, *, max_cost="10", estimate="4", ledger=False, td=None):
        ident = _payg_identity()
        registry = reg.ModelRegistry(models=[ident])
        registry.estimate = lambda identity, i, o: _cost(Decimal(estimate))
        req = r.RouteRequest("programming", complexity="high",
                             task_id="t", run_id="r", tag="DEV",
                             max_tokens=100)
        grants = r.GrantStore()
        grants.approve_task(req, {r.identity_key(ident)},
                            max_cost=Decimal(max_cost))
        router = r.Router(registry, lambda *a: handle, grants=grants,
                          breaker=r.CircuitBreaker(),
                          availability=lambda i: True)
        if ledger:
            from calls import CallLedger
            router.call_ledger = CallLedger(Path(td) / "calls.json")
        return router, req, ident, grants

    def test_active_reservation_released_on_definitive_failure(self):
        class Handle:
            def invoke(self, messages):
                raise RuntimeError("429 rate limit")

        router, req, ident, grants = self._build(Handle())
        with self.assertRaises(Exception):
            router.route(req).chain.invoke([])
        exp = grants.exposure(req, ident)
        self.assertEqual(exp["reserved"], Decimal("0"))
        self.assertEqual(exp["uncertain"], Decimal("0"))
        self.assertEqual(exp["spent"], Decimal("0"))

    def test_held_reservation_survives_subsequent_definitive_failure(self):
        calls = {"n": 0}

        class Handle:
            def invoke(self, messages):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise TimeoutError("timed out")   # ambiguous -> HELD
                raise RuntimeError("429 rate limit")  # definitive -> must not release

        # No ledger: the timeout retries once, then hits the definitive failure.
        router, req, ident, grants = self._build(Handle())
        with self.assertRaises(Exception):
            router.route(req).chain.invoke([])
        self.assertEqual(calls["n"], 2)
        exp = grants.exposure(req, ident)
        self.assertEqual(exp["reserved"], Decimal("0"))
        self.assertEqual(exp["uncertain"], Decimal("4"))  # exposure retained
        self.assertEqual(exp["spent"], Decimal("0"))
        # the retained uncertain exposure still reduces availability
        self.assertIsNone(grants.reserve(req, ident, amount=Decimal("7")))
        self.assertIsNotNone(grants.reserve(req, ident, amount=Decimal("6")))

    def test_held_reservation_survives_repeated_failure_handling(self):
        calls = {"n": 0}

        class Handle:
            def invoke(self, messages):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise TimeoutError("timed out")
                raise RuntimeError("503 service unavailable")

        router, req, ident, grants = self._build(Handle())
        with self.assertRaises(Exception):
            router.route(req).chain.invoke([])
        # a second invoke attempt on the same (still uncertain) grant must not
        # clear the earlier exposure
        with self.assertRaises(Exception):
            router.route(req).chain.invoke([])
        exp = grants.exposure(req, ident)
        self.assertGreaterEqual(exp["uncertain"], Decimal("4"))
        self.assertEqual(exp["spent"], Decimal("0"))


class CompatibilityChargeTests(unittest.TestCase):
    """F2: repeated compatibility charge() must not double-count."""

    def _setup(self, *, max_cost="10", estimate="4"):
        ident = _payg_identity()
        registry = reg.ModelRegistry(models=[ident])
        registry.estimate = lambda identity, i, o: _cost(Decimal(estimate))
        req = r.RouteRequest("programming", complexity="high",
                             task_id="t", run_id="r", max_tokens=100)
        grants = r.GrantStore()
        grants.approve_task(req, {r.identity_key(ident)},
                            max_cost=Decimal(max_cost))
        router = r.Router(registry, lambda *a: None, grants=grants)
        candidate = r.RouteCandidate(ident, registry.ref_for_identity(ident), None)
        return router, req, ident, grants, candidate

    def test_authorize_then_two_charges_count_once(self):
        router, req, ident, grants, candidate = self._setup()
        self.assertTrue(router.authorize(req, candidate))
        response = types.SimpleNamespace(usage_metadata={})
        router.charge(req, candidate, response)
        router.charge(req, candidate, response)  # repeat: idempotent
        self.assertEqual(grants.exposure(req, ident)["spent"], Decimal("4"))

    def test_concurrent_repeated_charge_counts_once(self):
        import threading
        router, req, ident, grants, candidate = self._setup()
        self.assertTrue(router.authorize(req, candidate))
        response = types.SimpleNamespace(usage_metadata={})
        barrier = threading.Barrier(8)
        errors = []

        def worker():
            barrier.wait()
            try:
                router.charge(req, candidate, response)
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(grants.exposure(req, ident)["spent"], Decimal("4"))

    def test_standalone_charge_without_authorization_still_counts(self):
        router, req, ident, grants, candidate = self._setup()
        self.assertIsNone(getattr(candidate, "_reservation", None))
        router.charge(req, candidate, types.SimpleNamespace(usage_metadata={}))
        self.assertEqual(grants.exposure(req, ident)["spent"], Decimal("4"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
