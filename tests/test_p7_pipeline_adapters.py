#!/usr/bin/env python3
"""Adapter-level full-pipeline harness: real Router/RouteChain/CallLedger,
WorkflowEngine + FactoryAdapter/NativeAdapter, fake provider transport only.

D04: technical fallback through the real classifier.
D05: PAYG timeout -> UNKNOWN, single invocation, resume blocks without retry.
"""

import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import main
    import engine
    import native_adapter
    import state as s
    from calls import CallLedger, CONFIRMED, UNKNOWN
    from registry import (BILLING_FREE, BILLING_PAYG, ModelIdentity,
                          ModelRegistry)
    from router import CircuitBreaker, GrantStore, RouteRequest, Router
    from attempts import AttemptEvent
    from store import StateManager
except Exception as exc:  # pragma: no cover
    DEPS_OK = False
    DEPS_ERR = str(exc)
else:
    DEPS_OK = True
    DEPS_ERR = ""


class Http429(Exception):
    status_code = 429


class FakeVerifier:
    def run_oracle(self, task_id, artifact, run_id=None, suffix=""):
        return types.SimpleNamespace(passed=True, exit_code=0)

    def run_critic(self, producer, task, code, run_id=None, review=False):
        return {"verdict": "PASS", "critic": {"ok": True},
                "producer_family": "fake", "critic_family": "critic",
                "producer_known": True, "objections": []}

    def run_test_oracle(self, *a, **k):
        return {"verdict": "SKIP", "advisory": True}


def identity(profile, model_id, billing):
    return ModelIdentity(provider=profile, endpoint=profile,
                         model_id=model_id, family=profile.split("-")[0],
                         credential_profile=profile, billing=billing,
                         capabilities=frozenset({"text"}), defined=True)


def make_registry():
    return ModelRegistry(models=[
        identity("gemini-plus", "gemini-3.5-flash", BILLING_FREE),
        identity("gemini-plus", "gemini-3.8-flash", BILLING_FREE),
        identity("deepseek-api", "deepseek-v4-pro", BILLING_PAYG),
    ])


class FakeHandle:
    def __init__(self, behavior, log, ref, owner):
        self.behavior = behavior
        self.log = log
        self.ref = ref
        self.owner = owner

    def invoke(self, messages):
        self.log.append(self.behavior)
        if self.behavior == "429":
            raise Http429("rate limited")
        if self.behavior == "timeout":
            raise TimeoutError("timed out")
        self.owner.last_served = self.ref
        return types.SimpleNamespace(
            content="```python\nprint('ok')\n```",
            usage_metadata={}, response_metadata={})


class PipelinePool:
    """Pool shim: real Router/RouteChain + canonical attempt/ledger wiring."""

    def __init__(self, script):
        self.script = script
        self.calls = []
        self.telemetry = None
        self.attempt_sink = None
        self.dispatch_guard = None
        self.last_attempt_model = {}
        self.local_up = False
        self.last_served = None
        self.registry = make_registry()

        def handle_provider(ident, tag, max_tokens, thinking):
            ref = self.registry.ref_for_identity(ident)
            return FakeHandle(self.script.get(ident.model_id, "ok"),
                              self.calls, ref, self)

        self.router = Router(self.registry, handle_provider,
                             grants=GrantStore(allow_paid=True),
                             breaker=CircuitBreaker(),
                             availability=lambda i: True)

    def is_peak(self):
        return False

    def refresh_peak(self):
        return None

    def team_llm(self, task, run_id=""):
        request = RouteRequest(role="programming",
                               complexity=getattr(task, "complexity", "medium"),
                               required_capabilities=frozenset({"text"}),
                               task_id=task.id, run_id=run_id, tag="DEV")
        routed = self.router.route(request)
        if not routed.candidates:
            return None, "DEV"
        return routed.chain, "DEV"

    def complete(self, llm, messages, meta=None):
        from calls import (clear_call_context, last_call_id,
                           last_served_ref)
        meta = meta or {}
        self.last_served = None
        clear_call_context()
        try:
            response = llm.invoke(messages)
        except Exception:
            clear_call_context()
            raise
        text = getattr(response, "content", response)
        text = text if isinstance(text, str) else str(text)
        served = last_served_ref() or self.last_served
        if served is None:
            refs = list(getattr(llm, "model_refs", []) or [])
            served = refs[0] if refs else None
        task_id = str(meta.get("task") or "")
        if served is not None and task_id:
            self.last_attempt_model[task_id] = served
            attempt = s.TaskAttempt(
                attempt_id="att_1", model=served, outcome="ok",
                call_id=last_call_id() or "")
            if self.attempt_sink is not None:
                self.attempt_sink(AttemptEvent(
                    attempt=attempt, task=task_id,
                    tag=str(meta.get("tag") or "DEV"),
                    run_id=str(meta.get("run") or "")))
        clear_call_context()
        return text


def make_state(project, ws, complexity):
    st = s.ProjectState(run_id="r", project_root=str(project), goal="demo",
                        engine_kind=s.ENGINE_UNIFIED, phase="BUILD",
                        profile="script")
    st.requirements = s.RequirementContract(objective="demo")
    st.architecture = s.ArchitectureContract(
        approach="one file", modules=[{"path": "app.py"}],
        interfaces=[{"name": "main", "file": "app.py"}])
    st.tasks = [s.Task("a", kind="build", target_file="app.py",
                       complexity=complexity, acceptance=["runs"])]
    st.approve_architecture()
    st.extensions["workspace_root"] = str(ws)
    return st


def settings(base):
    cfg = main.Settings()
    cfg.keep_awake = False
    cfg.sleep_offpeak = False
    cfg.test_oracle = False
    cfg.allow_unverified = False
    cfg.workspace_dir = str(base / "workspaces")
    cfg.log_dir = str(base / "logs")
    return cfg


def run_pipeline(kind, script, complexity="medium"):
    base = Path(tempfile.mkdtemp(prefix="p7pipe-"))
    project = base / "project"; project.mkdir()
    ws = base / "ws"
    cfg = settings(base)
    st = make_state(project, ws, complexity)
    pool = PipelinePool(script)
    if kind == "factory":
        adapter = engine.FactoryAdapter(cfg, pool, lambda *a: None, st,
                                        isolate_workspace=True)
        adapter.factory.verifier = FakeVerifier()
    else:
        adapter = native_adapter.NativeAdapter(
            cfg, pool, lambda *a: None, st, verifier=FakeVerifier(),
            isolate_workspace=True)
    store = StateManager(cfg.workspace_dir, cfg.log_dir)
    eng = engine.WorkflowEngine(st, store, adapter, emit=lambda *a: None)
    eng.apply_pool(pool)
    result = eng.step()
    return {"base": base, "state": st, "engine": eng, "pool": pool,
            "ledger": eng.call_ledger, "result": result, "kind": kind}


@unittest.skipUnless(DEPS_OK, f"runtime deps missing: {DEPS_ERR}")
class AdapterPipelineTests(unittest.TestCase):
    def test_d04_429_fallback_matches_across_adapters(self):
        script = {"gemini-3.5-flash": "429", "gemini-3.8-flash": "ok"}
        ref = run_pipeline("factory", script, "medium")
        new = run_pipeline("native", script, "medium")
        self.assertEqual(ref["result"], "ok")
        self.assertEqual(new["result"], "ok")
        self.assertEqual(ref["pool"].calls, ["429", "ok"])
        self.assertEqual(new["pool"].calls, ["429", "ok"])
        for run in (ref, new):
            task = run["state"].tasks[0]
            self.assertIn(task.status, ("done", "verified"))
            self.assertEqual(task.delivered_by.model_id, "gemini-3.8-flash")

    def test_d05_payg_timeout_unknown_and_resume_blocks(self):
        script = {"deepseek-v4-pro": "timeout"}
        for kind in ("factory", "native"):
            run = run_pipeline(kind, script, "high")
            self.assertEqual(run["pool"].calls, ["timeout"])
            recs = run["ledger"].unresolved("r")
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0].state, UNKNOWN)
            # clear the build_failed pause and retry the step: the ledger
            # gate must pause before any new provider call.
            run["state"].pause = s.PauseState()
            result = run["engine"].step()
            self.assertEqual(result, "paused")
            self.assertEqual(run["state"].pause.reason, "call_unknown_failed")
            self.assertEqual(run["pool"].calls, ["timeout"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
