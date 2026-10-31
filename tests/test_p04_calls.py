#!/usr/bin/env python3
"""PAYG call ledger: durable states, no silent retry, resume gate."""

import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calls import (CallLedger, CallLedgerError, CallOutcomeUnknown,
                   CONFIRMED, FAILED, PENDING, SUBMITTED, UNKNOWN)
from registry import BILLING_LOCAL, BILLING_PAYG, ModelIdentity, ModelRef
from router import RouteCandidate, RouteChain, RouteRequest, RouteResult
import engine
import state as s
from store import StateManager


def identity(billing, model="m1", family="f1"):
    return ModelIdentity(provider="p", endpoint="e", model_id=model,
                         family=family, credential_profile="cp",
                         billing=billing)


def ref_for(ident):
    return ModelRef(provider=ident.provider, endpoint=ident.endpoint,
                    model_id=ident.model_id, family=ident.family,
                    billing=ident.billing,
                    credential_profile=ident.credential_profile)


class FakeBreaker:
    def allow(self, ident):
        return True

    def record_failure(self, ident, info):
        return None

    def record_success(self, ident):
        return None


class FakeRouter:
    def __init__(self, ledger=None):
        self.breaker = FakeBreaker()
        self.call_ledger = ledger
        self.charged = []

    def authorize(self, request, candidate):
        return True

    def charge(self, request, candidate, response):
        self.charged.append(candidate.ref.model_id)


def candidate(ident, handler):
    return RouteCandidate(identity=ident, ref=ref_for(ident),
                          handle=types.SimpleNamespace(invoke=handler))


class Header429(Exception):
    status_code = 429


class LedgerTests(unittest.TestCase):
    def test_state_machine_and_reload(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "calls.json"
            led = CallLedger(path)
            rec = led.intent(run_id="r", role="programming",
                             ref=ref_for(identity(BILLING_PAYG)),
                             task="t", tag="DEV")
            self.assertEqual(rec.state, PENDING)
            self.assertTrue(rec.idempotency_key.startswith("crew-"))
            led.submitted(rec.call_id)
            led.fail(rec.call_id, "timeout", "timeout", ambiguous=True)
            self.assertEqual(led.get(rec.call_id).state, UNKNOWN)
            led.resolve(rec.call_id, "confirmed")
            self.assertEqual(led.get(rec.call_id).state, CONFIRMED)
            self.assertEqual(led.get(rec.call_id).resolved_by, "manual")

            again = CallLedger(path)
            self.assertEqual(again.get(rec.call_id).state, CONFIRMED)
            self.assertEqual(again.unresolved("r"), [])

    def test_corrupt_ledger_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "calls.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(CallLedgerError):
                CallLedger(path)


class RouteChainLedgerTests(unittest.TestCase):
    def test_payg_timeout_is_unknown_and_not_retried(self):
        with tempfile.TemporaryDirectory() as td:
            led = CallLedger(Path(td) / "calls.json")
            calls = {"n": 0}

            def handler(messages):
                calls["n"] += 1
                raise TimeoutError("timed out")

            ident = identity(BILLING_PAYG)
            result = RouteResult(
                request=RouteRequest(role="programming", run_id="r",
                                     task_id="t", tag="DEV"),
                candidates=[candidate(ident, handler)],
                router=FakeRouter(led))
            with self.assertRaises(CallOutcomeUnknown):
                result.chain.invoke([])
            self.assertEqual(calls["n"], 1)
            recs = led.unresolved("r")
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0].state, UNKNOWN)
            self.assertEqual(recs[0].model_id, "m1")

    def test_payg_429_is_failed_then_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            led = CallLedger(Path(td) / "calls.json")
            first = identity(BILLING_PAYG, model="m1")
            second = identity(BILLING_PAYG, model="m2")

            def fail(messages):
                raise Header429("rate limited")

            def ok(messages):
                return types.SimpleNamespace(content="ok")

            router = FakeRouter(led)
            result = RouteResult(
                request=RouteRequest(role="programming", run_id="r",
                                     task_id="t", tag="DEV"),
                candidates=[candidate(first, fail), candidate(second, ok)],
                router=router)
            response = result.chain.invoke([])
            self.assertEqual(response.content, "ok")
            self.assertEqual(router.charged, ["m2"])
            records = led.all()
            self.assertEqual([r.state for r in records], [FAILED, CONFIRMED])

    def test_non_payg_timeout_keeps_retry(self):
        with tempfile.TemporaryDirectory() as td:
            led = CallLedger(Path(td) / "calls.json")
            calls = {"n": 0}

            def handler(messages):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise TimeoutError("timed out")
                return types.SimpleNamespace(content="ok")

            ident = identity(BILLING_LOCAL, model="local")
            result = RouteResult(
                request=RouteRequest(role="programming", run_id="r",
                                     task_id="t", tag="DEV"),
                candidates=[candidate(ident, handler)],
                router=FakeRouter(led))
            response = result.chain.invoke([])
            self.assertEqual(response.content, "ok")
            self.assertEqual(calls["n"], 2)
            self.assertEqual(led.unresolved("r"), [])


class DummyAdapter:
    def step(self, phase):
        return True

    def sync_to_project(self):
        return None

    def on_last_ok(self, task_id, ref):
        return None


class EngineCallGateTests(unittest.TestCase):
    def test_unresolved_call_pauses_run(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            store = StateManager(base / "ws", base / "logs")
            st = s.ProjectState(run_id="r", goal="x", phase="BUILD",
                                engine_kind=s.ENGINE_UNIFIED)
            eng = engine.WorkflowEngine(st, store, DummyAdapter())
            led = CallLedger(base / "calls.json")
            rec = led.intent(run_id="r", role="programming",
                             ref=ref_for(identity(BILLING_PAYG)),
                             task="t", tag="DEV")
            led.submitted(rec.call_id)
            led.fail(rec.call_id, "timed out", "timeout", ambiguous=True)
            eng.call_ledger = led
            self.assertEqual(eng.step(), "paused")
            self.assertEqual(st.pause.reason, "call_unknown_failed")
            led.resolve(rec.call_id, "failed")
            eng.resume()
            self.assertEqual(st.pause.reason, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
