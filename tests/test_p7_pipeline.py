#!/usr/bin/env python3
"""Full-pipeline harness: real Router, RouteChain and CallLedger, fake transport.

Only the external provider boundary (``handle.invoke``) is simulated. This
proves routing/error-classification/ledger behaviour; it is not a claim of
adapter parity, which requires wiring both adapters through ``WorkflowEngine``.
"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calls import CallLedger, CallOutcomeUnknown, CONFIRMED, UNKNOWN
from registry import (BILLING_FREE, BILLING_PAYG, ModelIdentity, ModelRegistry)
from router import CircuitBreaker, GrantStore, RouteRequest, Router


class Http429(Exception):
    status_code = 429


def identity(profile, model_id, billing, family):
    return ModelIdentity(
        provider=profile, endpoint=profile, model_id=model_id,
        family=family, credential_profile=profile, billing=billing,
        capabilities=frozenset({"text"}), defined=True,
    )


def registry():
    return ModelRegistry(models=[
        identity("gemini-plus", "gemini-3.5-flash", BILLING_FREE, "gemini"),
        identity("gemini-plus", "gemini-3.8-flash", BILLING_FREE, "gemini"),
        identity("deepseek-api", "deepseek-v4-pro", BILLING_PAYG, "deepseek"),
    ])


class FakeHandle:
    def __init__(self, behavior, calls):
        self.behavior = behavior
        self.calls = calls

    def invoke(self, messages):
        self.calls.append(self.behavior)
        if self.behavior == "429":
            raise Http429("rate limited")
        if self.behavior == "timeout":
            raise TimeoutError("timed out")
        return types.SimpleNamespace(content="ok", usage_metadata={},
                                     response_metadata={})


class Pipeline:
    """Real Router + RouteChain + CallLedger with a scripted provider boundary."""

    def __init__(self, script: dict[str, str], ledger: CallLedger):
        self.script = script
        self.calls: list[str] = []

        def handle_provider(identity, tag, max_tokens, thinking):
            return FakeHandle(self.script.get(identity.model_id, "ok"),
                              self.calls)

        self.router = Router(
            registry(), handle_provider,
            grants=GrantStore(allow_paid=True),
            breaker=CircuitBreaker(),
            availability=lambda i: True,
        )
        self.router.call_ledger = ledger

    def invoke(self, role, *, run_id="r", task="t", tag="DEV", messages=None):
        request = RouteRequest(role=role, run_id=run_id, task_id=task, tag=tag,
                               max_tokens=1000, thinking=False)
        return self.router.route(request).chain.invoke(messages or ["hello"])


class FullPipelineTests(unittest.TestCase):
    def test_d04_429_falls_back_to_next_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = CallLedger(Path(td) / "calls.json")
            pipe = Pipeline({"gemini-3.5-flash": "429",
                             "gemini-3.8-flash": "ok"}, ledger)
            response = pipe.invoke("programming")
            self.assertEqual(response.content, "ok")
            self.assertEqual(pipe.calls, ["429", "ok"])
            self.assertEqual(ledger.unresolved("r"), [])

    def test_d05_timeout_payg_is_unknown_single_invocation(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = CallLedger(Path(td) / "calls.json")
            pipe = Pipeline({"deepseek-v4-pro": "timeout"}, ledger)
            with self.assertRaises(CallOutcomeUnknown):
                pipe.invoke("architecture")
            self.assertEqual(pipe.calls, ["timeout"])
            recs = ledger.unresolved("r")
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0].state, UNKNOWN)
            self.assertEqual(recs[0].model_id, "deepseek-v4-pro")

    def test_d06_confirmed_replays_without_new_invocation(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = CallLedger(Path(td) / "calls.json")
            pipe = Pipeline({"deepseek-v4-pro": "ok"}, ledger)
            self.assertEqual(pipe.invoke("architecture").content, "ok")
            self.assertEqual(pipe.calls, ["ok"])
            rec = ledger.all()[0]
            self.assertEqual(rec.state, CONFIRMED)

            again = Pipeline({"deepseek-v4-pro": "ok"}, ledger)
            response = again.invoke("architecture")
            self.assertEqual(response.content, "ok")
            self.assertTrue(response.response_metadata.get("replayed"))
            self.assertEqual(again.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
