#!/usr/bin/env python3
"""Adapter-level full-pipeline harness: real Router/RouteChain/CallLedger,
WorkflowEngine + FactoryAdapter/NativeAdapter, fake provider transport only.

D04: technical fallback and PAYG ambiguity through the real classifier.
D05: PAYG timeout -> UNKNOWN, single invocation, resume blocks without retry;
confirmed FAILED falls back once; manual FAILED resolution is durable and the
resume advances to the next candidate without repeating the original call.
D06: CONFIRMED result replay after crash boundaries, fingerprint changes,
missing/non-replayable artifacts, and history deduplication.
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
    from calls import CallLedger, CallLedgerError, CONFIRMED, UNKNOWN
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


class SimulatedCrash(BaseException):
    """Abrupt termination used to test resume/replay boundaries."""


class Http429(Exception):
    status_code = 429


class Http401(Exception):
    status_code = 401


class Http400(Exception):
    status_code = 400


class Http500(Exception):
    status_code = 500


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


def make_registry(mode="full"):
    models = [
        identity("gemini-plus", "gemini-3.1-pro-preview", BILLING_FREE),
        identity("gemini-plus", "gemini-3.5-flash", BILLING_FREE),
        identity("gemini-plus", "gemini-3.8-flash", BILLING_FREE),
        identity("openrouter", "nvidia/nemotron-3-ultra", BILLING_FREE),
        identity("deepseek-api", "deepseek-v4-pro", BILLING_PAYG),
    ]
    if mode == "flash":
        models = [m for m in models if m.model_id.startswith("gemini-3.")]
    elif mode == "payg":
        models = [m for m in models if m.billing == BILLING_PAYG]
    elif mode == "payg_first":
        # Fixture only: reclassify the first COMPLEX_CHAIN candidate as PAYG so
        # an ambiguous PAYG outcome has a real free candidate behind it. This
        # exercises the "never fall back silently" guarantee without changing
        # production routing order.
        models = [
            identity("gemini-plus", "gemini-3.1-pro-preview", BILLING_PAYG),
            identity("openrouter", "nvidia/nemotron-3-ultra", BILLING_FREE),
        ]
    return ModelRegistry(models=models)


class FakeHandle:
    def __init__(self, behavior, log, ref, owner):
        self.behavior = behavior
        self.log = log
        self.ref = ref
        self.owner = owner

    def invoke(self, messages):
        self.log.append(self.behavior)
        self.owner.transport.append((self.ref.model_id, self.behavior))
        if self.behavior == "429":
            raise Http429("rate limited")
        if self.behavior == "auth":
            raise Http401("unauthorized")
        if self.behavior == "bad":
            raise Http400("bad request")
        if self.behavior == "server":
            raise Http500("server error")
        if self.behavior == "connection":
            raise ConnectionError("connection refused")
        if self.behavior == "timeout":
            raise TimeoutError("timed out")
        if self.behavior == "empty":
            return types.SimpleNamespace(
                content="", usage_metadata={}, response_metadata={})
        if self.behavior == "tool":
            self.owner.last_served = self.ref
            return types.SimpleNamespace(
                content="tool-output",
                tool_calls=[{"name": "tool", "args": {}}],
                usage_metadata={}, response_metadata={})
        self.owner.last_served = self.ref
        return types.SimpleNamespace(
            content="```python\nprint('ok')\n```",
            usage_metadata={}, response_metadata={})


class PipelinePool:
    """Pool shim: real Router/RouteChain + canonical attempt/ledger wiring."""

    def __init__(self, script, registry_mode="full"):
        self.script = script
        self.calls = []
        self.transport = []
        self.telemetry = None
        self.attempt_sink = None
        self.dispatch_guard = None
        self.last_attempt_model = {}
        self.local_up = False
        self.last_served = None
        self.last_response = None
        self.crash_after_confirm = False
        self.request_overrides = None
        self.registry = make_registry(registry_mode)

        def handle_provider(ident, tag, max_tokens, thinking):
            ref = self.registry.ref_for_identity(ident)
            return FakeHandle(self.script.get(ident.model_id, "ok"),
                              self.calls, ref, self)

        self.router = Router(self.registry, handle_provider,
                             grants=GrantStore(allow_paid=True),
                             breaker=CircuitBreaker(),
                             availability=lambda i: True)
        # Inject effective generation parameters at the router boundary. The
        # Factory adapter has no knob for these, so this is harness-level
        # control of the request that both adapters actually route.
        base_route = self.router.route

        def route(request):
            if self.request_overrides:
                for key, value in self.request_overrides.items():
                    setattr(request, key, value)
            return base_route(request)

        self.router.route = route

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
        self.last_response = response
        if self.crash_after_confirm:
            self.crash_after_confirm = False
            raise SimulatedCrash("after ledger confirm, before attempt")
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


def _make_adapter(kind, cfg, pool, st):
    if kind == "factory":
        adapter = engine.FactoryAdapter(cfg, pool, lambda *a: None, st,
                                        isolate_workspace=True)
        adapter.factory.verifier = FakeVerifier()
    else:
        adapter = native_adapter.NativeAdapter(
            cfg, pool, lambda *a: None, st, verifier=FakeVerifier(),
            isolate_workspace=True)
    return adapter


def _crash_before_consume(eng):
    original = eng.call_ledger.mark_consumed

    def crash(call_id):
        eng.call_ledger.mark_consumed = original
        raise SimulatedCrash("after attempt save, before mark_consumed")

    eng.call_ledger.mark_consumed = crash


def run_pipeline(kind, script, complexity="medium", pre_ledger=None,
                 registry_mode="full", *, crash_after_confirm=False,
                 crash_before_consume=False, request_overrides=None):
    base = Path(tempfile.mkdtemp(prefix="p7pipe-"))
    project = base / "project"; project.mkdir()
    ws = base / "ws"
    cfg = settings(base)
    st = make_state(project, ws, complexity)
    if pre_ledger is not None:
        pre_ledger(project)
    pool = PipelinePool(script, registry_mode)
    pool.crash_after_confirm = crash_after_confirm
    pool.request_overrides = request_overrides
    adapter = _make_adapter(kind, cfg, pool, st)
    store = StateManager(cfg.workspace_dir, cfg.log_dir)
    eng = engine.WorkflowEngine(st, store, adapter, emit=lambda *a: None)
    eng.apply_pool(pool)
    if crash_before_consume:
        _crash_before_consume(eng)
    crashed = False
    try:
        result = eng.step()
    except SimulatedCrash:
        crashed = True
        result = "crashed"
    return {"base": base, "state": st, "engine": eng, "pool": pool,
            "ledger": eng.call_ledger, "result": result, "kind": kind,
            "transport": pool.transport, "crashed": crashed}


def resume_run(run, script=None, registry_mode="payg_first", *,
               request_overrides=None):
    """New engine/adapter over the same project ledger and canonical state."""
    base = run["base"]
    cfg = settings(base)
    st = run["state"]
    pool = PipelinePool(script or {}, registry_mode)
    pool.request_overrides = request_overrides
    adapter = _make_adapter(run["kind"], cfg, pool, st)
    store = StateManager(cfg.workspace_dir, cfg.log_dir)
    eng = engine.WorkflowEngine(st, store, adapter, emit=lambda *a: None)
    eng.apply_pool(pool)
    result = eng.step()
    return {"base": base, "state": st, "engine": eng, "pool": pool,
            "ledger": eng.call_ledger, "result": result, "kind": run["kind"],
            "transport": pool.transport}


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

    def test_d03_hard_task_candidate_order_and_exhaustion(self):
        ok_script = {"gemini-3.1-pro-preview": "429",
                     "nvidia/nemotron-3-ultra": "ok"}
        for kind in ("factory", "native"):
            run = run_pipeline(kind, ok_script, "high")
            self.assertEqual(run["result"], "ok")
            self.assertEqual(run["transport"], [
                ("gemini-3.1-pro-preview", "429"),
                ("nvidia/nemotron-3-ultra", "ok")])
            self.assertEqual(run["state"].tasks[0].delivered_by.model_id,
                             "nvidia/nemotron-3-ultra")

        dead = {"gemini-3.1-pro-preview": "429",
                "nvidia/nemotron-3-ultra": "429",
                "deepseek-v4-pro": "429"}
        for kind in ("factory", "native"):
            run = run_pipeline(kind, dead, "high")
            self.assertEqual(run["result"], "failed")
            self.assertEqual([m for m, _b in run["transport"]],
                             ["gemini-3.1-pro-preview",
                              "nvidia/nemotron-3-ultra",
                              "deepseek-v4-pro"])

    def test_d04_auth_connection_server_fallback(self):
        # Compare both adapters rather than hard-coding which candidate the
        # shared router/breaker selects after the first failure.
        for behavior in ("auth", "connection", "server"):
            script = {"gemini-3.5-flash": behavior,
                      "gemini-3.8-flash": "ok",
                      "nvidia/nemotron-3-ultra": "ok"}
            ref = run_pipeline("factory", script, "medium")
            new = run_pipeline("native", script, "medium")
            self.assertEqual(ref["result"], "ok", behavior)
            self.assertEqual(new["result"], "ok", behavior)
            self.assertEqual(ref["transport"], new["transport"], behavior)
            self.assertEqual(ref["transport"][0],
                             ("gemini-3.5-flash", behavior))
            self.assertEqual(ref["transport"][-1][1], "ok")
            self.assertEqual(ref["state"].tasks[0].delivered_by.model_id,
                             new["state"].tasks[0].delivered_by.model_id)

    def test_d04_bad_request_does_not_fall_back(self):
        script = {"gemini-3.5-flash": "bad", "gemini-3.8-flash": "ok"}
        for kind in ("factory", "native"):
            run = run_pipeline(kind, script, "medium")
            self.assertEqual(run["result"], "failed")
            self.assertEqual(run["transport"],
                             [("gemini-3.5-flash", "bad")])

    def test_d04_payg_ambiguity_blocks_without_retry_or_fallback(self):
        # The request is dispatched (ledger SUBMITTED, transport log) before
        # the ambiguous failure, so this is not a pre-invocation timeout.
        for behavior in ("connection", "server", "empty"):
            script = {"gemini-3.1-pro-preview": behavior,
                      "nvidia/nemotron-3-ultra": "ok"}
            for kind in ("factory", "native"):
                run = run_pipeline(kind, script, "high",
                                   registry_mode="payg_first")
                self.assertEqual(run["result"], "failed", (kind, behavior))
                self.assertEqual(run["state"].pause.reason, "build_failed")
                self.assertEqual(run["pool"].calls, [behavior],
                                 (kind, behavior))
                self.assertEqual(run["transport"],
                                 [("gemini-3.1-pro-preview", behavior)])
                recs = run["ledger"].unresolved("r")
                self.assertEqual(len(recs), 1, (kind, behavior))
                self.assertEqual(recs[0].state, UNKNOWN)
                self.assertEqual(recs[0].error_kind, behavior)
                run["state"].pause = s.PauseState()
                self.assertEqual(run["engine"].step(), "paused")
                self.assertEqual(run["state"].pause.reason,
                                 "call_unknown_failed")
                self.assertEqual(run["transport"],
                                 [("gemini-3.1-pro-preview", behavior)])

    def test_d04_payg_unknown_manual_resolution_persists(self):
        script = {"gemini-3.1-pro-preview": "connection",
                  "nvidia/nemotron-3-ultra": "ok"}
        for kind in ("factory", "native"):
            run = run_pipeline(kind, script, "high",
                               registry_mode="payg_first")
            rec = run["ledger"].unresolved("r")[0]
            self.assertEqual(rec.state, UNKNOWN)
            with self.assertRaises(CallLedgerError):
                run["ledger"].resolve(rec.call_id, "confirmed")
            resolved = run["ledger"].resolve(rec.call_id, "failed")
            self.assertEqual(resolved.state, "FAILED")
            self.assertEqual(resolved.resolved_by, "manual")
            path = Path(run["base"]) / "project" / ".crew" / "calls.json"
            reloaded = CallLedger(path).get(rec.call_id)
            self.assertEqual(reloaded.state, "FAILED")
            self.assertEqual(reloaded.resolved_by, "manual")

    def test_d05_confirmed_failure_falls_back_once(self):
        script = {"gemini-3.1-pro-preview": "429",
                  "nvidia/nemotron-3-ultra": "ok"}
        for kind in ("factory", "native"):
            run = run_pipeline(kind, script, "high",
                               registry_mode="payg_first")
            self.assertEqual(run["result"], "ok")
            self.assertEqual(run["transport"], [
                ("gemini-3.1-pro-preview", "429"),
                ("nvidia/nemotron-3-ultra", "ok")])
            self.assertEqual(run["pool"].calls, ["429", "ok"])
            recs = run["ledger"].all()
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0].state, "FAILED")
            self.assertEqual(recs[0].error_kind, "rate_limit")
            self.assertEqual(recs[0].resolved_by, "")
            self.assertEqual(run["ledger"].unresolved("r"), [])
            self.assertEqual(run["state"].tasks[0].delivered_by.model_id,
                             "nvidia/nemotron-3-ultra")

    def test_d05_manual_failed_resume_advances_without_repeat(self):
        script = {"gemini-3.1-pro-preview": "connection",
                  "nvidia/nemotron-3-ultra": "ok"}
        for kind in ("factory", "native"):
            run = run_pipeline(kind, script, "high",
                               registry_mode="payg_first")
            rec = run["ledger"].unresolved("r")[0]
            self.assertEqual(rec.state, UNKNOWN)
            resolved = run["ledger"].resolve(rec.call_id, "failed")
            self.assertEqual(resolved.state, "FAILED")
            self.assertEqual(resolved.resolved_by, "manual")
            path = Path(run["base"]) / "project" / ".crew" / "calls.json"
            reloaded = CallLedger(path).get(rec.call_id)
            self.assertEqual(reloaded.state, "FAILED")
            self.assertEqual(reloaded.resolved_by, "manual")

            run["state"].pause = s.PauseState()
            result = run["engine"].step()
            self.assertIn(result, ("ok", "done"), kind)
            self.assertEqual(run["transport"], [
                ("gemini-3.1-pro-preview", "connection"),
                ("nvidia/nemotron-3-ultra", "ok")], kind)
            self.assertIn(run["state"].tasks[0].status, ("done", "verified"))
            self.assertEqual(run["state"].tasks[0].delivered_by.model_id,
                             "nvidia/nemotron-3-ultra")
            self.assertEqual(len(run["ledger"].all()), 1)

    def test_d05_failed_ref_requires_full_identity(self):
        # Same credential_profile + model_id, different provider/endpoint: the
        # failed record must not suppress the current candidate.
        def seed(project):
            led = CallLedger(project / ".crew" / "calls.json")
            other = ModelIdentity(
                provider="other-provider", endpoint="other-endpoint",
                model_id="gemini-3.1-pro-preview", family="other",
                credential_profile="gemini-plus", billing=BILLING_PAYG,
                capabilities=frozenset({"text"}), defined=True)
            ref = ModelRegistry(models=[]).ref_for_identity(other)
            rec = led.intent(run_id="r", role="programming", ref=ref,
                             task="a", tag="DEV")
            led.submitted(rec.call_id)
            led.fail(rec.call_id, "server error", "server", ambiguous=False)

        for kind in ("factory", "native"):
            run = run_pipeline(kind, {"gemini-3.1-pro-preview": "ok"},
                               "high", pre_ledger=seed,
                               registry_mode="payg_first")
            self.assertEqual(run["result"], "ok", kind)
            self.assertEqual(run["transport"],
                             [("gemini-3.1-pro-preview", "ok")], kind)
            self.assertEqual(run["state"].tasks[0].delivered_by.model_id,
                             "gemini-3.1-pro-preview")
            self.assertTrue(any(
                rec.state == "FAILED" and rec.provider == "other-provider"
                for rec in run["ledger"].all()))

    def test_d05_resolution_guards_reject_incompatible(self):
        script = {"gemini-3.1-pro-preview": "connection",
                  "nvidia/nemotron-3-ultra": "ok"}
        for kind in ("factory", "native"):
            run = run_pipeline(kind, script, "high",
                               registry_mode="payg_first")
            path = Path(run["base"]) / "project" / ".crew" / "calls.json"
            rec = run["ledger"].unresolved("r")[0]
            self.assertEqual(rec.state, UNKNOWN)
            with self.assertRaises(CallLedgerError):
                run["ledger"].resolve(rec.call_id, "confirmed")

            # Both incompatible resolutions are still rejected after a
            # restart. The artifact is restored after reload, so the load-time
            # reconcile does not promote the record before the guard runs.
            restarted = CallLedger(path)
            with self.assertRaises(CallLedgerError):
                restarted.resolve(rec.call_id, "confirmed")
            restarted._write_result(rec, "recovered text", True)
            with self.assertRaises(CallLedgerError):
                restarted.resolve(rec.call_id, "failed")
            confirmed = restarted.resolve(rec.call_id, "confirmed")
            self.assertEqual(confirmed.state, "CONFIRMED")
            self.assertEqual(confirmed.resolved_by, "manual")
            self.assertEqual(CallLedger(path).get(rec.call_id).state,
                             "CONFIRMED")

    def test_d05_pending_and_submitted_block_without_calls(self):
        for pending_state in ("PENDING", "SUBMITTED"):
            def seed(project, state=pending_state):
                led = CallLedger(project / ".crew" / "calls.json")
                ref = make_registry().ref_for_identity(
                    identity("deepseek-api", "deepseek-v4-pro", BILLING_PAYG))
                rec = led.intent(run_id="r", role="programming", ref=ref,
                                 task="a", tag="DEV")
                if state == "SUBMITTED":
                    led.submitted(rec.call_id)

            for kind in ("factory", "native"):
                run = run_pipeline(kind, {}, "medium", pre_ledger=seed)
                self.assertEqual(run["result"], "paused")
                self.assertEqual(run["transport"], [])
                self.assertEqual(run["state"].pause.reason,
                                 "call_unknown_failed")

    def test_d05_payg_timeout_unknown_and_resume_blocks(self):
        script = {"deepseek-v4-pro": "timeout"}
        for kind in ("factory", "native"):
            run = run_pipeline(kind, script, "high", registry_mode="payg")
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

    def _crash_after_confirm_run(self, kind, behavior="ok", overrides=None):
        script = {"gemini-3.1-pro-preview": behavior,
                  "nvidia/nemotron-3-ultra": "ok"}
        run = run_pipeline(kind, script, "high",
                           registry_mode="payg_first",
                           crash_after_confirm=True,
                           request_overrides=overrides)
        self.assertTrue(run["crashed"], kind)
        return run, script

    def test_d06_confirmed_replays_without_new_invocation(self):
        for kind in ("factory", "native"):
            run, _script = self._crash_after_confirm_run(kind)
            rec = run["ledger"].all()[0]
            self.assertEqual(rec.state, CONFIRMED)
            self.assertFalse(rec.consumed)
            self.assertIsNotNone(run["ledger"].result_text(rec.call_id))

            resumed = resume_run(run, {})
            self.assertIn(resumed["result"], ("ok", "done"), kind)
            self.assertEqual(resumed["transport"], [], kind)
            self.assertEqual(resumed["pool"].calls, [])
            rec2 = resumed["ledger"].get(rec.call_id)
            self.assertEqual(rec2.state, CONFIRMED)
            self.assertTrue(rec2.consumed)
            self.assertTrue(resumed["pool"].last_response.response_metadata
                            .get("replayed"))

    def test_d06_crash_after_confirm_before_attempt(self):
        for kind in ("factory", "native"):
            run, _script = self._crash_after_confirm_run(kind)
            self.assertEqual(run["state"].tasks[0].attempts, [])
            resumed = resume_run(run, {})
            self.assertEqual(resumed["transport"], [], kind)
            self.assertEqual(len(resumed["state"].tasks[0].attempts), 1)
            self.assertTrue(resumed["ledger"].all()[0].consumed)

    def test_d06_crash_after_attempt_before_consume_dedupes(self):
        for kind in ("factory", "native"):
            script = {"gemini-3.1-pro-preview": "ok",
                      "nvidia/nemotron-3-ultra": "ok"}
            run = run_pipeline(kind, script, "high",
                               registry_mode="payg_first",
                               crash_before_consume=True)
            self.assertTrue(run["crashed"], kind)
            attempts = run["state"].tasks[0].attempts
            self.assertEqual(len(attempts), 1)
            call_id = attempts[0].call_id
            self.assertTrue(call_id)
            rec = run["ledger"].get(call_id)
            self.assertEqual(rec.state, CONFIRMED)
            self.assertFalse(rec.consumed)

            resumed = resume_run(run, {})
            self.assertEqual(resumed["transport"], [], kind)
            self.assertEqual(len(resumed["state"].tasks[0].attempts), 1)
            self.assertEqual(resumed["state"].tasks[0].attempts[0].call_id,
                             call_id)
            self.assertTrue(resumed["ledger"].get(call_id).consumed)
            history = [e for e in resumed["state"].history
                       if e.get("kind") == "attempt"
                       and e.get("attempt", {}).get("call_id") == call_id]
            self.assertEqual(history, [], kind)

    def test_d06_changed_fingerprint_forces_new_call(self):
        cases = [
            ("max_tokens", {"max_tokens": 2048}),
            ("thinking", {"thinking": True}),
            ("tools", {"tools": [{"name": "x"}]}),
            ("response_format", {"response_format": {"type": "json_object"}}),
        ]
        for name, overrides in cases:
            for kind in ("factory", "native"):
                run, script = self._crash_after_confirm_run(kind)
                old = run["ledger"].all()[0]
                self.assertEqual(old.state, CONFIRMED)
                self.assertFalse(old.consumed)

                resumed = resume_run(run, script,
                                     request_overrides=overrides)
                self.assertEqual(resumed["transport"],
                                 [("gemini-3.1-pro-preview", "ok")],
                                 (kind, name))
                records = resumed["ledger"].all()
                self.assertEqual(len(records), 2, (kind, name))
                self.assertEqual(
                    resumed["ledger"].get(old.call_id).state, CONFIRMED)
                self.assertFalse(
                    resumed["ledger"].get(old.call_id).consumed)
                fresh = [r for r in records if r.call_id != old.call_id][0]
                self.assertEqual(fresh.state, CONFIRMED)
                self.assertTrue(fresh.consumed)

    def test_d06_missing_artifact_fails_closed(self):
        for kind in ("factory", "native"):
            run, _script = self._crash_after_confirm_run(kind)
            rec = run["ledger"].all()[0]
            path = (Path(run["base"]) / "project" / ".crew" /
                    "call_results" / f"{rec.call_id}.json")
            path.unlink()
            resumed = resume_run(run, {})
            self.assertEqual(resumed["result"], "paused", kind)
            self.assertEqual(resumed["state"].pause.reason,
                             "call_recovery_failed")
            self.assertEqual(resumed["transport"], [])
            self.assertFalse(resumed["ledger"].get(rec.call_id).consumed)

    def test_d06_tool_calls_are_not_replayable(self):
        for kind in ("factory", "native"):
            run, _script = self._crash_after_confirm_run(kind, "tool")
            rec = run["ledger"].all()[0]
            self.assertEqual(rec.state, CONFIRMED)
            self.assertFalse(rec.replayable)
            self.assertIsNone(run["ledger"].result_text(rec.call_id))
            resumed = resume_run(run, {})
            self.assertEqual(resumed["result"], "paused", kind)
            self.assertEqual(resumed["state"].pause.reason,
                             "call_recovery_failed")
            self.assertEqual(resumed["transport"], [])
            self.assertFalse(resumed["ledger"].get(rec.call_id).consumed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
