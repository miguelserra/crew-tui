#!/usr/bin/env python3
"""PAYG call ledger: durable states, result recovery, no silent retry."""

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


def chain_for(led, handlers, *, task="t", tag="DEV", run="r",
              max_tokens=8192, thinking=False):
    ident = identity(BILLING_PAYG)
    candidates = [candidate(ident, h) for h in handlers]
    result = RouteResult(
        request=RouteRequest(role="programming", run_id=run,
                             task_id=task, tag=tag,
                             max_tokens=max_tokens, thinking=thinking),
        candidates=candidates, router=FakeRouter(led))
    return result.chain


class LedgerTests(unittest.TestCase):
    def test_confirmed_persists_result_and_reloads(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "calls.json"
            led = CallLedger(path)
            rec = led.intent(run_id="r", role="programming",
                             ref=ref_for(identity(BILLING_PAYG)),
                             task="t", tag="DEV", request_sha256="fp")
            self.assertEqual(rec.state, PENDING)
            led.submitted(rec.call_id)
            led.confirm(rec.call_id, served=ref_for(identity(BILLING_PAYG)),
                        text="cached-output")
            self.assertEqual(led.get(rec.call_id).state, CONFIRMED)
            self.assertEqual(led.result_text(rec.call_id), "cached-output")

            again = CallLedger(path)
            self.assertEqual(again.get(rec.call_id).state, CONFIRMED)
            self.assertEqual(again.result_text(rec.call_id), "cached-output")
            self.assertIsNotNone(again.find_replay("r", "t", "DEV", "fp"))

    def test_manual_resolution_requires_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            led = CallLedger(Path(td) / "calls.json")
            rec = led.intent(run_id="r", role="programming",
                             ref=ref_for(identity(BILLING_PAYG)),
                             task="t", tag="DEV")
            led.submitted(rec.call_id)
            led.fail(rec.call_id, "timed out", "timeout", ambiguous=True)
            with self.assertRaises(CallLedgerError):
                led.resolve(rec.call_id, "confirmed")
            self.assertEqual(led.get(rec.call_id).state, UNKNOWN)
            led.resolve(rec.call_id, "failed")
            self.assertEqual(led.get(rec.call_id).state, FAILED)

    def test_resolve_failed_rejects_persisted_result(self):
        with tempfile.TemporaryDirectory() as td:
            led = CallLedger(Path(td) / "calls.json")
            rec = led.intent(run_id="r", role="programming",
                             ref=ref_for(identity(BILLING_PAYG)),
                             task="t", tag="DEV")
            led.submitted(rec.call_id)
            led.confirm(rec.call_id, text="answer")
            led._records[rec.call_id].state = UNKNOWN
            with self.assertRaises(CallLedgerError):
                led.resolve(rec.call_id, "failed")

    def test_reconcile_promotes_submitted_with_result(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "calls.json"
            led = CallLedger(path)
            rec = led.intent(run_id="r", role="programming",
                             ref=ref_for(identity(BILLING_PAYG)),
                             task="t", tag="DEV", request_sha256="fp")
            led.submitted(rec.call_id)
            led._write_result(rec, "survived", True)
            self.assertEqual(led.get(rec.call_id).state, SUBMITTED)
            again = CallLedger(path)
            self.assertEqual(again.get(rec.call_id).state, CONFIRMED)
            self.assertEqual(again.result_text(rec.call_id), "survived")

    def test_recovery_required_when_result_missing(self):
        with tempfile.TemporaryDirectory() as td:
            led = CallLedger(Path(td) / "calls.json")
            rec = led.intent(run_id="r", role="programming",
                             ref=ref_for(identity(BILLING_PAYG)),
                             task="t", tag="DEV")
            led.submitted(rec.call_id)
            led.confirm(rec.call_id, text="answer")
            led._result_path(rec.call_id).unlink()
            self.assertEqual([r.call_id for r in led.recovery_required("r")],
                             [rec.call_id])

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

            chain = chain_for(led, [handler])
            with self.assertRaises(CallOutcomeUnknown):
                chain.invoke(["hello"])
            self.assertEqual(calls["n"], 1)
            recs = led.unresolved("r")
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0].state, UNKNOWN)
            self.assertIsNone(led.result_text(recs[0].call_id))

    def test_payg_429_is_failed_then_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            led = CallLedger(Path(td) / "calls.json")

            def fail(messages):
                raise Header429("rate limited")

            def ok(messages):
                return types.SimpleNamespace(content="ok")

            chain = chain_for(led, [fail, ok])
            response = chain.invoke(["hello"])
            self.assertEqual(response.content, "ok")
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
            response = result.chain.invoke(["hello"])
            self.assertEqual(response.content, "ok")
            self.assertEqual(calls["n"], 2)
            self.assertEqual(led.unresolved("r"), [])

    def test_confirmed_result_replays_without_new_call(self):
        with tempfile.TemporaryDirectory() as td:
            led = CallLedger(Path(td) / "calls.json")

            def first(messages):
                return types.SimpleNamespace(content="cached-output")

            chain1 = chain_for(led, [first])
            self.assertEqual(chain1.invoke(["hello"]).content, "cached-output")
            rec = led.all()[0]
            self.assertEqual(rec.state, CONFIRMED)
            self.assertFalse(rec.consumed)

            calls = {"n": 0}

            def should_not_call(messages):
                calls["n"] += 1
                raise AssertionError("provider must not be called on replay")

            chain2 = chain_for(led, [should_not_call])
            response = chain2.invoke(["hello"])
            self.assertEqual(response.content, "cached-output")
            self.assertTrue(response.response_metadata.get("replayed"))
            self.assertEqual(calls["n"], 0)

    def test_replay_requires_same_generation_contract(self):
        with tempfile.TemporaryDirectory() as td:
            led = CallLedger(Path(td) / "calls.json")

            def first(messages):
                return types.SimpleNamespace(content="one")

            chain1 = chain_for(led, [first], max_tokens=1000, thinking=False)
            self.assertEqual(chain1.invoke(["hello"]).content, "one")

            calls = {"n": 0}

            def second(messages):
                calls["n"] += 1
                return types.SimpleNamespace(content="two")

            chain2 = chain_for(led, [second], max_tokens=8000, thinking=True)
            self.assertEqual(chain2.invoke(["hello"]).content, "two")
            self.assertEqual(calls["n"], 1)

    def test_structured_response_is_not_replayable(self):
        with tempfile.TemporaryDirectory() as td:
            led = CallLedger(Path(td) / "calls.json")

            def structured(messages):
                return types.SimpleNamespace(
                    content=[{"type": "text", "text": "x"}],
                    tool_calls=[{"name": "tool", "args": {}}])

            chain1 = chain_for(led, [structured])
            chain1.invoke(["hello"])
            rec = led.all()[0]
            self.assertFalse(rec.replayable)
            self.assertIsNone(led.result_text(rec.call_id))
            self.assertIsNone(led.find_replay("r", "t", "DEV",
                                              rec.request_sha256))

            calls = {"n": 0}

            def text(messages):
                calls["n"] += 1
                return types.SimpleNamespace(content="plain")

            chain2 = chain_for(led, [text])
            self.assertEqual(chain2.invoke(["hello"]).content, "plain")
            self.assertEqual(calls["n"], 1)


class DummyAdapter:
    def step(self, phase):
        return True

    def sync_to_project(self):
        return None

    def on_last_ok(self, task_id, ref):
        return None


class EngineCallGateTests(unittest.TestCase):
    def _engine(self, base):
        store = StateManager(base / "ws", base / "logs")
        st = s.ProjectState(run_id="r", goal="x", phase="BUILD",
                            engine_kind=s.ENGINE_UNIFIED)
        st.tasks = [s.Task("t", kind="build", target_file="a.py")]
        eng = engine.WorkflowEngine(st, store, DummyAdapter())
        led = CallLedger(base / "calls.json")
        eng.call_ledger = led
        return eng, led, st

    def test_pending_and_submitted_pause(self):
        for state in (PENDING, SUBMITTED):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as td:
                eng, led, st = self._engine(Path(td))
                rec = led.intent(run_id="r", role="programming",
                                 ref=ref_for(identity(BILLING_PAYG)),
                                 task="t", tag="DEV")
                if state == SUBMITTED:
                    led.submitted(rec.call_id)
                self.assertEqual(eng.step(), "paused")
                self.assertEqual(st.pause.reason, "call_unknown_failed")

    def test_unknown_pauses_and_manual_resolution_unblocks(self):
        with tempfile.TemporaryDirectory() as td:
            eng, led, st = self._engine(Path(td))
            rec = led.intent(run_id="r", role="programming",
                             ref=ref_for(identity(BILLING_PAYG)),
                             task="t", tag="DEV")
            led.submitted(rec.call_id)
            led.fail(rec.call_id, "timed out", "timeout", ambiguous=True)
            self.assertEqual(eng.step(), "paused")
            self.assertEqual(st.pause.reason, "call_unknown_failed")
            led.resolve(rec.call_id, "failed")
            eng.resume()
            self.assertEqual(st.pause.reason, "")

    def test_confirmed_without_result_pauses_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            eng, led, st = self._engine(Path(td))
            rec = led.intent(run_id="r", role="programming",
                             ref=ref_for(identity(BILLING_PAYG)),
                             task="t", tag="DEV")
            led.submitted(rec.call_id)
            led.confirm(rec.call_id, text="answer")
            led._result_path(rec.call_id).unlink()
            self.assertEqual(eng.step(), "paused")
            self.assertEqual(st.pause.reason, "call_recovery_failed")

    def test_engine_marks_call_consumed_from_attempt(self):
        with tempfile.TemporaryDirectory() as td:
            eng, led, st = self._engine(Path(td))
            rec = led.intent(run_id="r", role="programming",
                             ref=ref_for(identity(BILLING_PAYG)),
                             task="t", tag="DEV")
            led.submitted(rec.call_id)
            led.confirm(rec.call_id, served=ref_for(identity(BILLING_PAYG)),
                        text="answer")
            event = s.TaskAttempt(attempt_id="att_1",
                                  model=ref_for(identity(BILLING_PAYG)),
                                  outcome="ok", call_id=rec.call_id)
            from attempts import AttemptEvent
            eng.on_attempt(AttemptEvent(attempt=event, task="t", tag="DEV"))
            self.assertTrue(led.get(rec.call_id).consumed)

    def test_history_reconciliation_dedups_critic_event(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = base / "proj"; proj.mkdir()
            path = proj / ".crew" / "calls.json"
            led = CallLedger(path)
            rec = led.intent(run_id="r", role="critic",
                             ref=ref_for(identity(BILLING_PAYG)),
                             task="t", tag="CRITIC")
            led.submitted(rec.call_id)
            led.confirm(rec.call_id, text="crit")

            store = StateManager(base / "ws", base / "logs")
            st = s.ProjectState(run_id="r", goal="x", phase="BUILD",
                                project_root=str(proj),
                                engine_kind=s.ENGINE_UNIFIED)
            st.history = [{
                "kind": "attempt",
                "attempt": {"call_id": rec.call_id, "attempt_id": "att_old",
                            "model": ref_for(identity(BILLING_PAYG)).to_dict(),
                            "outcome": "ok"},
                "task": "t", "tag": "CRITIC", "at": "2026-01-01T00:00:00Z",
            }]
            eng = engine.WorkflowEngine(st, store, DummyAdapter())
            pool = types.SimpleNamespace(
                router=types.SimpleNamespace(), telemetry=None,
                attempt_sink=None, dispatch_guard=None)
            eng.apply_pool(pool)

            from attempts import AttemptEvent
            event = s.TaskAttempt(attempt_id="att_new",
                                  model=ref_for(identity(BILLING_PAYG)),
                                  outcome="ok", call_id=rec.call_id)
            eng.on_attempt(AttemptEvent(attempt=event, task="t", tag="CRITIC"))
            self.assertEqual(len(st.history), 1)
            self.assertTrue(eng.call_ledger.get(rec.call_id).consumed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
