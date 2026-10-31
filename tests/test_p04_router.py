#!/usr/bin/env python3
"""P4: routing policy, error classification, breaker and PAYG grants."""

import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import registry as reg
import router as r
from budget import BudgetSuspended


class FakeHandle:
    def __init__(self, name="m", error=None, successes=0):
        self.name = name
        self.error = error
        self.successes = successes
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        if self.error is not None and self.calls > self.successes:
            raise self.error
        return SimpleNamespace(content="ok", usage_metadata={},
                               response_metadata={"model_name": self.name})


def make_router(handles=None, availability=None, grants=None, breaker=None,
                request_grant=None, registry=None):
    registry = registry or reg.ModelRegistry()
    handles = handles or {}

    def provider(identity, tag, max_tokens, thinking):
        return handles.get(identity.model_id) or FakeHandle(identity.model_id)

    return r.Router(registry, provider,
                    breaker=breaker or r.CircuitBreaker(),
                    grants=grants or r.GrantStore(),
                    availability=availability or (lambda i: None),
                    request_grant=request_grant)


class ErrorClassificationTests(unittest.TestCase):
    def test_rate_limit_vs_quota(self):
        self.assertEqual(r.classify_error(RuntimeError("429 rate limit")).kind,
                         "rate_limit")
        self.assertEqual(r.classify_error(RuntimeError(
            "429 insufficient_quota: exceeded your current quota")).kind,
            "quota_exhausted")
        self.assertNotEqual(r.classify_error(RuntimeError("quota exceeded")).kind,
                            r.classify_error(RuntimeError("rate limit")).kind)

    def test_other_classes(self):
        self.assertEqual(r.classify_error(TimeoutError("timed out")).kind, "timeout")
        self.assertEqual(r.classify_error(ConnectionError("connection refused")).kind,
                         "connection")
        self.assertEqual(r.classify_error(RuntimeError("503 service unavailable")).kind,
                         "server")
        self.assertEqual(r.classify_error(RuntimeError("401 invalid api key")).kind,
                         "auth")
        self.assertEqual(r.classify_error(RuntimeError("400 bad request")).kind,
                         "bad_request")
        self.assertEqual(r.classify_error(RuntimeError("empty response from model")).kind,
                         "empty")

    def test_retry_after_header(self):
        class Error(Exception):
            def __init__(self):
                super().__init__("429")
                self.response = SimpleNamespace(headers={"Retry-After": "120"})

        info = r.classify_error(Error())
        self.assertEqual(info.retry_after, 120)


class BreakerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "router_state.json"
        self.now = 1000.0
        self.clock = lambda: self.now
        self.identity = reg.ModelRegistry().identity_for("gemini-plus",
                                                         "gemini-3.5-flash")

    def tearDown(self):
        self.tmp.cleanup()

    def _breaker(self):
        return r.CircuitBreaker(self.path, clock=self.clock)

    def test_three_rate_limits_open_and_persist(self):
        b = self._breaker()
        info = r.ErrorInfo("rate_limit")
        for _ in range(3):
            b.record_failure(self.identity, info)
        self.assertFalse(b.allow(self.identity))
        again = self._breaker()
        self.assertFalse(again.allow(self.identity))
        self.now += 901
        self.assertTrue(again.allow(self.identity))  # half-open probe allowed
        again.record_success(self.identity)
        self.assertTrue(again.allow(self.identity))

    def test_quota_is_not_rate_limit(self):
        b = self._breaker()
        b.record_failure(self.identity, r.ErrorInfo("quota_exhausted"))
        self.assertFalse(b.allow(self.identity))
        snap = b.snapshot()
        entry = next(iter(snap.values()))
        self.assertGreater(entry["quota_until"], self.now)

    def test_retry_after_extends_cooldown(self):
        b = self._breaker()
        b.record_failure(self.identity, r.ErrorInfo("rate_limit", retry_after=2000))
        for _ in range(2):
            b.record_failure(self.identity, r.ErrorInfo("rate_limit", retry_after=2000))
        self.now += 901
        self.assertFalse(b.allow(self.identity))
        self.now += 1100
        self.assertTrue(b.allow(self.identity))


class GrantTests(unittest.TestCase):
    def test_task_grant_does_not_authorize_other_task(self):
        reg_ = reg.ModelRegistry()
        grants = r.GrantStore()
        ident = reg_.identity_for("deepseek-api", "deepseek-v4-pro")
        req1 = r.RouteRequest("programming", task_id="t1", run_id="run")
        req2 = r.RouteRequest("programming", task_id="t2", run_id="run")
        self.assertFalse(grants.allows(req1, ident))
        grants.approve_task(req1, {"deepseek-v4-pro"})
        self.assertTrue(grants.allows(req1, ident))
        self.assertFalse(grants.allows(req2, ident))

    def test_allow_paid_is_run_scope(self):
        grants = r.GrantStore(allow_paid=True)
        ident = reg.ModelRegistry().identity_for("deepseek-api", "deepseek-v4-pro")
        self.assertTrue(grants.allows(
            r.RouteRequest("programming", task_id="t1", run_id="r"), ident))
        self.assertTrue(grants.allows(
            r.RouteRequest("programming", task_id="t2", run_id="other"), ident))


class RoutePolicyTests(unittest.TestCase):
    def setUp(self):
        self.reg = reg.ModelRegistry()

    def test_hard_task_starts_complex(self):
        router = make_router()
        result = router.route(r.RouteRequest("programming", complexity="high"))
        self.assertEqual(result.candidates[0].identity.model_id,
                         "gemini-3.1-pro-preview")

    def test_normal_task_starts_flash(self):
        router = make_router()
        result = router.route(r.RouteRequest("programming", complexity="medium"))
        self.assertEqual(result.candidates[0].identity.model_id,
                         "gemini-3.5-flash")

    def test_fixer_big_pickle_only_when_available(self):
        handles = {"big-pickle": FakeHandle("big-pickle")}
        router = make_router(handles=handles, availability=lambda i: True)
        result = router.route(r.RouteRequest("fixer"))
        self.assertEqual(result.candidates[0].identity.model_id, "big-pickle")
        router = make_router(handles=handles, availability=lambda i: False)
        result = router.route(r.RouteRequest("fixer"))
        self.assertNotEqual(result.candidates[0].identity.model_id, "big-pickle")

    def test_visual_requires_vision(self):
        router = make_router()
        result = router.route(r.RouteRequest(
            "programming", required_capabilities=frozenset({"text", "vision"})))
        self.assertTrue(result.candidates)
        for candidate in result.candidates:
            self.assertIn("vision", candidate.identity.capabilities)

    def test_escalation_uses_stronger_chain_than_fallback(self):
        router = make_router()
        normal = router.route(r.RouteRequest("programming", complexity="medium"))
        producer = normal.candidates[0].ref
        escalation = router.route(r.RouteRequest(
            "programming", complexity="high", escalation=True, producer=producer))
        self.assertEqual(normal.candidates[0].identity.model_id,
                         "gemini-3.5-flash")
        self.assertEqual(escalation.candidates[0].identity.model_id,
                         "gemini-3.1-pro-preview")

    def test_text_only_registry_has_no_visual_candidate(self):
        only_text = reg.ModelRegistry()
        only_text.models = {k: v for k, v in only_text.models.items()
                            if "vision" not in v.capabilities}
        router = make_router(registry=only_text)
        result = router.route(r.RouteRequest(
            "visual", required_capabilities=frozenset({"vision"})))
        self.assertEqual(result.candidates, [])

    def test_escalation_excludes_producer(self):
        router = make_router()
        producer = self.reg.ref_for_identity(
            self.reg.identity_for("gemini-plus", "gemini-3.1-pro-preview"))
        result = router.route(r.RouteRequest(
            "programming", complexity="high", escalation=True, producer=producer))
        self.assertNotEqual(result.candidates[0].identity.model_id,
                            "gemini-3.1-pro-preview")


class RouteChainTests(unittest.TestCase):
    def test_429_falls_back_to_next_candidate(self):
        handles = {"gemini-3.5-flash": FakeHandle("flash", RuntimeError("429 rate limit")),
                   "gemini-3.8-flash": FakeHandle("flash2")}
        router = make_router(handles=handles)
        result = router.route(r.RouteRequest("programming", complexity="medium"))
        response = result.chain.invoke([])
        self.assertEqual(response.content, "ok")
        self.assertEqual(handles["gemini-3.8-flash"].calls, 1)

    def test_timeout_retries_once(self):
        class Flaky(FakeHandle):
            def invoke(self, messages):
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError("timed out")
                return SimpleNamespace(content="ok", usage_metadata={},
                                       response_metadata={"model_name": "flash"})

        h = Flaky("flash")
        handles = {"gemini-3.5-flash": h}
        router = make_router(handles=handles)
        result = router.route(r.RouteRequest("programming", complexity="medium"))
        response = result.chain.invoke([])
        self.assertEqual(response.content, "ok")
        self.assertEqual(h.calls, 2)

    def test_timeout_twice_falls_back_to_next(self):
        flash = FakeHandle("flash", TimeoutError("timed out"))
        next_handle = FakeHandle("flash2")
        handles = {"gemini-3.5-flash": flash,
                   "gemini-3.8-flash": next_handle}
        router = make_router(handles=handles)
        result = router.route(r.RouteRequest("programming", complexity="medium"))
        response = result.chain.invoke([])
        self.assertEqual(response.content, "ok")
        self.assertEqual(flash.calls, 2)
        self.assertEqual(next_handle.calls, 1)

    def test_bad_request_does_not_fall_back(self):
        handles = {"gemini-3.5-flash": FakeHandle("flash", RuntimeError("400 bad request")),
                   "gemini-3.8-flash": SimpleNamespace(invoke=lambda m: FakeHandle("x").invoke(m))}
        router = make_router(handles=handles)
        result = router.route(r.RouteRequest("programming", complexity="medium"))
        with self.assertRaises(RuntimeError):
            result.chain.invoke([])

    def test_cooldown_skips_invocation(self):
        identity = reg.ModelRegistry().identity_for("gemini-plus", "gemini-3.5-flash")
        breaker = r.CircuitBreaker()
        now = [1000.0]
        breaker.clock = lambda: now[0]
        for _ in range(3):
            breaker.record_failure(identity, r.ErrorInfo("rate_limit"))
        handles = {"gemini-3.5-flash": FakeHandle("flash"),
                   "gemini-3.8-flash": FakeHandle("flash2")}
        router = make_router(handles=handles, breaker=breaker)
        result = router.route(r.RouteRequest("programming", complexity="medium"))
        response = result.chain.invoke([])
        self.assertEqual(response.content, "ok")
        self.assertEqual(handles["gemini-3.5-flash"].calls, 0)

    def test_payg_without_grant_is_blocked(self):
        handles = {m: FakeHandle(m, RuntimeError("429 rate limit"))
                   for m in ("gemini-3.1-pro-preview", "nvidia/nemotron-3-ultra",
                             "kimi-k3.1-pro", "gemini-4-argon")}
        handles["deepseek-v4-pro"] = FakeHandle("deepseek-v4-pro")
        router = make_router(handles=handles)
        result = router.route(r.RouteRequest("programming", complexity="high",
                                             task_id="t1"))
        with self.assertRaises(BudgetSuspended):
            result.chain.invoke([])

    def test_task_grant_allows_only_that_task(self):
        handles = {m: FakeHandle(m, RuntimeError("429 rate limit"))
                   for m in ("gemini-3.1-pro-preview", "nvidia/nemotron-3-ultra",
                             "kimi-k3.1-pro", "gemini-4-argon")}
        handles["deepseek-v4-pro"] = FakeHandle("deepseek-v4-pro")
        grants = r.GrantStore()
        router = make_router(handles=handles, grants=grants)
        req1 = r.RouteRequest("programming", complexity="high", task_id="t1")
        grants.approve_task(req1, {"deepseek-v4-pro"})
        self.assertEqual(router.route(req1).chain.invoke([]).content, "ok")
        req2 = r.RouteRequest("programming", complexity="high", task_id="t2")
        with self.assertRaises(BudgetSuspended):
            router.route(req2).chain.invoke([])


if __name__ == "__main__":
    unittest.main(verbosity=2)
