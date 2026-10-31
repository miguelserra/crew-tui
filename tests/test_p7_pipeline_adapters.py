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

import json
import os
import subprocess
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
    """Deterministic critic with explicit quality-rejection modes."""

    def __init__(self, mode="pass", critic_verdicts=None):
        self.mode = mode
        self.critic_calls = []
        self._calls = {}
        self.oracle_calls = []
        self.critic_verdicts = list(critic_verdicts or [])

    def run_oracle(self, task_id, artifact, run_id=None, suffix=""):
        # Fail if artifact contains syntactically invalid Python pattern
        is_invalid = "def broken(" in artifact
        result = types.SimpleNamespace(
            passed=not is_invalid,
            exit_code=1 if is_invalid else 0,
            stderr="syntax error" if is_invalid else "",
            stdout=""
        )
        self.oracle_calls.append({
            "task": task_id, "passed": result.passed,
            "exit_code": result.exit_code, "artifact": artifact[:500]
        })
        return result

    def run_critic(self, producer, task, code, run_id=None, review=False):
        key = getattr(task, "id", task)
        n = self._calls.get(key, 0) + 1
        self._calls[key] = n
        if self.critic_verdicts:
            verdict = self.critic_verdicts.pop(0)
            reject = verdict == "FAIL"
        else:
            reject = self.mode == "always_fail" or (
                self.mode == "fail_first" and n == 1)
            verdict = "FAIL" if reject else "PASS"
        objections = ([{"claim": "missing acceptance behaviour",
                        "evidence": "critic rejected first artifact"}]
                      if reject else [])
        self.critic_calls.append({
            "n": n, "task": key, "verdict": verdict,
            "producer": getattr(producer, "model_id", ""),
            "code": code, "objections": objections,
        })
        return {"verdict": verdict,
                "critic": {"ok": not reject,
                           "note": "quality rejected" if reject else "ok"},
                "producer_family": "fake", "critic_family": "critic",
                "producer_known": True, "objections": objections}

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
        responder = getattr(self.owner, "responder", None)
        if responder is not None:
            reply = responder(self.ref, messages)
            if isinstance(reply, BaseException):
                raise reply
            self.owner.last_served = self.ref
            return types.SimpleNamespace(
                content=str(reply), usage_metadata={}, response_metadata={})
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
        if self.behavior == "invalid":
            self.owner.last_served = self.ref
            return types.SimpleNamespace(
                content="```python\ndef broken(:\n    pass\n```",
                usage_metadata={}, response_metadata={})
        self.owner.last_served = self.ref
        return types.SimpleNamespace(
            content="```python\nprint('ok')\n```",
            usage_metadata={}, response_metadata={})


class PipelinePool:
    """Pool shim: real Router/RouteChain + canonical attempt/ledger wiring."""

    def __init__(self, script, registry_mode="full", responder=None):
        self.script = script
        self.responder = responder
        self.calls = []
        self.transport = []
        self.telemetry = None
        self.attempt_sink = None
        self.dispatch_guard = None
        self.last_attempt_model = {}
        self.local_up = False
        self.last_served = None
        self.last_response = None
        self.task_order = []
        self.crash_after_confirm = False
        self.crash_after_confirm_exit = False
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
        self.task_order.append(str(meta.get("task") or ""))
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
            if self.crash_after_confirm_exit:
                os._exit(71)
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


def make_state(project, ws, complexity, tasks=None):
    st = s.ProjectState(run_id="r", project_root=str(project), goal="demo",
                        engine_kind=s.ENGINE_UNIFIED, phase="BUILD",
                        profile="script")
    st.requirements = s.RequirementContract(objective="demo")
    st.architecture = s.ArchitectureContract(
        approach="one file", modules=[{"path": "app.py"}],
        interfaces=[{"name": "main", "file": "app.py"}])
    st.tasks = tasks if tasks is not None else [
        s.Task("a", kind="build", target_file="app.py",
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


def _make_adapter(kind, cfg, pool, st, verifier=None):
    verifier = verifier or FakeVerifier()
    if kind == "factory":
        adapter = engine.FactoryAdapter(cfg, pool, lambda *a: None, st,
                                        isolate_workspace=True)
        adapter.factory.verifier = verifier
    else:
        adapter = native_adapter.NativeAdapter(
            cfg, pool, lambda *a: None, st, verifier=verifier,
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
                 crash_before_consume=False, request_overrides=None,
                 verifier_mode="pass", tasks=None):
    base = Path(tempfile.mkdtemp(prefix="p7pipe-"))
    project = base / "project"; project.mkdir()
    ws = base / "ws"
    cfg = settings(base)
    st = make_state(project, ws, complexity, tasks)
    if pre_ledger is not None:
        pre_ledger(project)
    pool = PipelinePool(script, registry_mode)
    pool.crash_after_confirm = crash_after_confirm
    pool.request_overrides = request_overrides
    verifier = FakeVerifier(verifier_mode)
    adapter = _make_adapter(kind, cfg, pool, st, verifier)
    store = StateManager(cfg.workspace_dir, cfg.log_dir)
    eng = engine.WorkflowEngine(st, store, adapter, emit=lambda *a: None)
    eng.apply_pool(pool)
    if crash_before_consume:
        _crash_before_consume(eng)
    crashed = False
    graph_error = ""
    try:
        result = eng.step()
    except SimulatedCrash:
        crashed = True
        result = "crashed"
    except engine.EngineError as exc:
        result = "invalid_graph"
        graph_error = str(exc)
    return {"base": base, "state": st, "engine": eng, "pool": pool,
            "ledger": eng.call_ledger, "result": result, "kind": kind,
            "transport": pool.transport, "crashed": crashed,
            "graph_error": graph_error, "verifier": verifier}


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

    @staticmethod
    def _graph_task(tid, deps=(), status="pending", target=None):
        return s.Task(tid, kind="build", target_file=target or f"{tid}.py",
                      depends_on=list(deps), status=status,
                      complexity="medium", acceptance=["runs"])

    def _assert_rejected_graph(self, tasks, kind):
        run = run_pipeline(kind, {}, tasks=tasks)
        self.assertEqual(run["result"], "invalid_graph", kind)
        self.assertIn("grafo de tarefas inválido", run["graph_error"], kind)
        self.assertEqual(run["transport"], [], kind)
        self.assertEqual(run["pool"].calls, [], kind)
        self.assertEqual(run["pool"].task_order, [], kind)

    def test_d02_missing_dependency_rejected_without_calls(self):
        for kind in ("factory", "native"):
            self._assert_rejected_graph([
                self._graph_task("a"),
                self._graph_task("b", deps=["missing"]),
            ], kind)

    def test_d02_cycle_rejected_without_calls(self):
        for kind in ("factory", "native"):
            self._assert_rejected_graph([
                self._graph_task("a", deps=["b"]),
                self._graph_task("b", deps=["c"]),
                self._graph_task("c", deps=["a"]),
            ], kind)

    def test_d02_self_dependency_and_duplicate_ids_rejected(self):
        cases = {
            "self": [self._graph_task("a", deps=["a"])],
            "duplicate": [self._graph_task("a"),
                          self._graph_task("a", target="other.py")],
        }
        for name, tasks in cases.items():
            for kind in ("factory", "native"):
                with self.subTest(case=name, kind=kind):
                    self._assert_rejected_graph(tasks, kind)

    def test_d02_ready_tasks_follow_declared_order(self):
        tasks = [
            self._graph_task("b"),
            self._graph_task("c"),
            self._graph_task("a", deps=["b"]),
        ]
        for kind in ("factory", "native"):
            run = run_pipeline(kind, {}, tasks=tasks)
            self.assertEqual(run["result"], "ok", kind)
            self.assertEqual(run["pool"].task_order, ["b", "c", "a"], kind)
            for task in run["state"].tasks:
                self.assertIn(task.status, ("done", "verified"), kind)

    def test_d02_partial_resume_skips_completed_and_orders_dependents(self):
        tasks = [
            self._graph_task("a", status="done"),
            self._graph_task("b"),
            self._graph_task("c", deps=["b"]),
        ]
        for kind in ("factory", "native"):
            run = run_pipeline(kind, {}, tasks=tasks)
            self.assertEqual(run["result"], "ok", kind)
            self.assertEqual(run["pool"].task_order, ["b", "c"], kind)
            state = {t.id: t for t in run["state"].tasks}
            self.assertEqual(state["a"].status, "done", kind)
            self.assertEqual(state["a"].attempts, [], kind)
            self.assertIn(state["b"].status, ("done", "verified"), kind)
            self.assertIn(state["c"].status, ("done", "verified"), kind)

    def test_d02_failed_dependency_blocks_dependents(self):
        tasks = [self._graph_task("a"), self._graph_task("b", deps=["a"])]
        script = {"gemini-3.5-flash": "bad"}
        for kind in ("factory", "native"):
            run = run_pipeline(kind, script, tasks=tasks)
            self.assertEqual(run["result"], "failed", kind)
            self.assertEqual(run["pool"].task_order, ["a"], kind)
            state = {t.id: t for t in run["state"].tasks}
            self.assertEqual(state["a"].status, "failed", kind)
            self.assertEqual(state["b"].status, "pending", kind)
            self.assertEqual(state["b"].attempts, [], kind)

    def test_d02_skipped_dependency_does_not_satisfy(self):
        tasks = [
            self._graph_task("a", status="skipped"),
            self._graph_task("b", deps=["a"]),
        ]
        for kind in ("factory", "native"):
            run = run_pipeline(kind, {}, tasks=tasks)
            self.assertEqual(run["result"], "failed", kind)
            self.assertEqual(run["pool"].task_order, [], kind)
            state = {t.id: t for t in run["state"].tasks}
            self.assertEqual(state["b"].status, "pending", kind)
            self.assertTrue(any("dependências não satisfeitas" in err
                                for err in state["b"].errors), kind)

    def test_d03_quality_failure_escalates(self):
        script = {"gemini-3.1-pro-preview": "ok",
                  "nvidia/nemotron-3-ultra": "ok"}
        for kind in ("factory", "native"):
            run = run_pipeline(kind, script, "high",
                               verifier_mode="fail_first")
            self.assertEqual(run["result"], "ok", kind)
            self.assertEqual(run["transport"], [
                ("gemini-3.1-pro-preview", "ok"),
                ("nvidia/nemotron-3-ultra", "ok")], kind)
            self.assertEqual(run["pool"].calls, ["ok", "ok"], kind)
            self.assertEqual([c["verdict"] for c in
                              run["verifier"].critic_calls],
                             ["FAIL", "PASS"], kind)
            self.assertEqual([c["producer"] for c in
                              run["verifier"].critic_calls],
                             ["gemini-3.1-pro-preview",
                              "nvidia/nemotron-3-ultra"], kind)
            task = run["state"].tasks[0]
            self.assertIn(task.status, ("done", "verified"), kind)
            self.assertEqual(task.delivered_by.model_id,
                             "nvidia/nemotron-3-ultra", kind)

    def test_d03_quality_failure_is_not_technical_fallback(self):
        script = {"gemini-3.1-pro-preview": "ok",
                  "nvidia/nemotron-3-ultra": "ok"}
        for kind in ("factory", "native"):
            run = run_pipeline(kind, script, "high",
                               verifier_mode="fail_first")
            task = run["state"].tasks[0]
            self.assertEqual(run["pool"].calls, ["ok", "ok"], kind)
            self.assertFalse(any(word in " ".join(task.errors).lower()
                                 for word in ("timeout", "429", "connection",
                                              "rate_limit", "server")), kind)
            self.assertTrue(any(err.startswith("critic FAIL:")
                                for err in task.errors), kind)
            self.assertFalse(run["state"].pause.reason, kind)

    def test_d03_approved_response_does_not_reinvoke(self):
        script = {"gemini-3.1-pro-preview": "ok",
                  "nvidia/nemotron-3-ultra": "ok"}
        for kind in ("factory", "native"):
            run = run_pipeline(kind, script, "high")
            self.assertEqual(run["result"], "ok", kind)
            self.assertEqual(run["transport"],
                             [("gemini-3.1-pro-preview", "ok")], kind)
            self.assertEqual(len(run["verifier"].critic_calls), 1, kind)
            self.assertEqual(run["verifier"].critic_calls[0]["verdict"],
                             "PASS", kind)
            task = run["state"].tasks[0]
            self.assertEqual(len(task.attempts), 1, kind)
            rec = run["state"].verifications.get(task.id)
            self.assertIsNotNone(rec, kind)
            self.assertEqual(rec.verdict, "PASS", kind)

    def test_d03_quality_chain_exhausted_never_passes(self):
        script = {"gemini-3.1-pro-preview": "ok",
                  "nvidia/nemotron-3-ultra": "ok"}
        for kind in ("factory", "native"):
            run = run_pipeline(kind, script, "high",
                               verifier_mode="always_fail")
            self.assertEqual(run["result"], "failed", kind)
            self.assertEqual(run["transport"], [
                ("gemini-3.1-pro-preview", "ok"),
                ("nvidia/nemotron-3-ultra", "ok")], kind)
            self.assertEqual([c["verdict"] for c in
                              run["verifier"].critic_calls],
                             ["FAIL", "FAIL"], kind)
            task = run["state"].tasks[0]
            self.assertEqual(task.status, "failed", kind)
            rec = run["state"].verifications.get(task.id)
            self.assertIsNotNone(rec, kind)
            self.assertEqual(rec.verdict, "FAIL", kind)
            self.assertFalse((Path(run["base"]) / "project" / "app.py")
                             .exists(), kind)

    def test_d03_quality_billing_parity(self):
        script = {"gemini-3.1-pro-preview": "ok",
                  "nvidia/nemotron-3-ultra": "ok"}
        ref = run_pipeline("factory", script, "high",
                           verifier_mode="fail_first")
        new = run_pipeline("native", script, "high",
                           verifier_mode="fail_first")
        self.assertEqual(ref["transport"], new["transport"])
        self.assertEqual(ref["pool"].calls, new["pool"].calls)
        self.assertEqual(
            [(c["producer"], c["verdict"], c["n"]) for c in
             ref["verifier"].critic_calls],
            [(c["producer"], c["verdict"], c["n"]) for c in
             new["verifier"].critic_calls])
        self.assertEqual(ref["result"], new["result"])
        self.assertEqual(len(ref["ledger"].all()), len(new["ledger"].all()))

    def test_d03_quality_evidence_and_history(self):
        script = {"gemini-3.1-pro-preview": "ok",
                  "nvidia/nemotron-3-ultra": "ok"}
        for kind in ("factory", "native"):
            run = run_pipeline(kind, script, "high",
                               verifier_mode="fail_first")
            task = run["state"].tasks[0]
            self.assertTrue(any("missing acceptance behaviour" in err
                                for err in task.errors), kind)
            self.assertEqual([a.model.model_id for a in task.attempts],
                             ["gemini-3.1-pro-preview",
                              "nvidia/nemotron-3-ultra"], kind)
            self.assertTrue(all(a.attempt_id for a in task.attempts), kind)
            final = run["state"].verifications.get(task.id)
            self.assertIsNotNone(final, kind)
            self.assertEqual(final.verdict, "PASS", kind)
            self.assertEqual(final.critic.get("verdict"), "PASS", kind)

    def test_d07_oracle_failure_fixes_and_revalidates(self):
        # Initial artifact is syntactically invalid (def broken); both adapters
        # must reject via oracle/static check, fix via escalation, re-validate,
        # and install. Evidence must show "oracle FAIL" in task.errors.
        script = {
            "gemini-3.1-pro-preview": "invalid",  # producer - initial invalid
            "nvidia/nemotron-3-ultra": "ok",      # fixer (escalation excludes producer)
        }
        for kind in ("factory", "native"):
            run = run_pipeline(kind, script, "high")
            self.assertEqual(run["result"], "ok", kind)
            self.assertEqual(run["transport"], [
                ("gemini-3.1-pro-preview", "invalid"),
                ("nvidia/nemotron-3-ultra", "ok")], kind)
            task = run["state"].tasks[0]
            self.assertIn(task.status, ("done", "verified"), kind)
            self.assertEqual(task.delivered_by.model_id,
                             "nvidia/nemotron-3-ultra", kind)
            # Evidence: oracle FAIL recorded in task.errors
            self.assertTrue(any(err.startswith("oracle FAIL:")
                                for err in task.errors), kind)
            # Final verification PASS with critic approval
            final = run["state"].verifications.get(task.id)
            self.assertIsNotNone(final, kind)
            self.assertEqual(final.verdict, "PASS", kind)
            self.assertEqual(final.critic.get("verdict"), "PASS", kind)
            # Native oracle calls: first fails, second passes
            if kind == "native":
                self.assertEqual(len(run["verifier"].oracle_calls), 2, kind)
                self.assertFalse(run["verifier"].oracle_calls[0]["passed"])
                self.assertTrue(run["verifier"].oracle_calls[1]["passed"])
            # Factory uses static check, no oracle calls
            if kind == "factory":
                self.assertEqual(len(run["verifier"].oracle_calls), 0, kind)

    def test_d07_oracle_failure_exhausted_never_passes(self):
        # Both initial and fix artifacts invalid - both adapters must fail
        # without delivering, with oracle FAIL evidence.
        script = {
            "gemini-3.1-pro-preview": "invalid",
            "nvidia/nemotron-3-ultra": "invalid",
            "gemini-3.8-flash": "invalid",  # chief attempt for factory
        }
        for kind in ("factory", "native"):
            run = run_pipeline(kind, script, "high")
            self.assertEqual(run["result"], "failed", kind)
            task = run["state"].tasks[0]
            self.assertEqual(task.status, "failed", kind)
            # No file installed
            self.assertFalse((Path(run["base"]) / "project" / "app.py").exists(), kind)
            # Evidence of oracle failure
            self.assertTrue(any(err.startswith("oracle FAIL:")
                                for err in task.errors), kind)
            # Final verification FAIL
            final = run["state"].verifications.get(task.id)
            self.assertIsNotNone(final, kind)
            self.assertEqual(final.verdict, "FAIL", kind)

    def test_d07_allow_unverified_cannot_bypass_oracle_fail(self):
        # Even with allow_unverified=True, oracle failure must block delivery
        script = {"gemini-3.1-pro-preview": "invalid",
                  "nvidia/nemotron-3-ultra": "invalid"}
        base = Path(tempfile.mkdtemp(prefix="p7d07-"))
        project = base / "project"; project.mkdir()
        ws = base / "ws"
        for kind in ("factory", "native"):
            cfg = settings(base)
            cfg.allow_unverified = True
            st = make_state(project, ws, "high")
            pool = PipelinePool(script)
            verifier = FakeVerifier()
            if kind == "factory":
                adapter = engine.FactoryAdapter(cfg, pool, lambda *a: None, st,
                                                isolate_workspace=True)
                adapter.factory.verifier = verifier
            else:
                adapter = native_adapter.NativeAdapter(
                    cfg, pool, lambda *a: None, st, verifier=verifier,
                    isolate_workspace=True)
            store = StateManager(cfg.workspace_dir, cfg.log_dir)
            eng = engine.WorkflowEngine(st, store, adapter, emit=lambda *a: None)
            eng.apply_pool(pool)
            result = eng.step()
            self.assertEqual(result, "failed", kind)
            task = st.tasks[0]
            self.assertEqual(task.status, "failed", kind)
            self.assertTrue(any(err.startswith("oracle FAIL:")
                                for err in task.errors), kind)
            final = st.verifications.get(task.id)
            self.assertIsNotNone(final, kind)
            self.assertEqual(final.verdict, "FAIL", kind)

    def test_d07_failure_evidence_parity(self):
        # Compare the structure of failure evidence between adapters
        script = {"gemini-3.1-pro-preview": "invalid",
                  "nvidia/nemotron-3-ultra": "ok"}
        ref = run_pipeline("factory", script, "high")
        new = run_pipeline("native", script, "high")
        # Both have oracle FAIL in errors
        for kind, run in (("factory", ref), ("native", new)):
            task = run["state"].tasks[0]
            self.assertTrue(any(err.startswith("oracle FAIL:")
                                for err in task.errors), kind)
            final = run["state"].verifications.get(task.id)
            self.assertEqual(final.verdict, "PASS", kind)
            self.assertEqual(final.critic.get("verdict"), "PASS", kind)
            self.assertEqual(final.oracle.get("passed"), True, kind)

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


@unittest.skipUnless(DEPS_OK, f"runtime deps missing: {DEPS_ERR}")
class D09SignoffPipelineTests(unittest.TestCase):
    """D09: integration, fixer revalidation, sign-off approval/rejection and
    gate prevention through the real WorkflowEngine and both adapters."""

    def _responder(self, initial="print('ok')", fix="", multi_fix=None):
        def responder(ref, messages):
            blob = "\n".join(str(getattr(m, "content", m)) for m in messages)
            if "handover memory" in blob:
                return json.dumps({
                    "what_it_is": "demo",
                    "how_to_run": ["python app.py"],
                    "entrypoints": ["app.py"],
                    "architecture_notes": [],
                    "tests": [],
                    "known_issues": [],
                    "next_steps": [],
                })
            if "fix the runtime/integration defects" in blob:
                return multi_fix if multi_fix is not None else fix
            if "Deliverable file: util.py" in blob:
                return "```python\nx = 2\n```"
            return f"```python\n{initial}\n```"
        return responder

    def _tasks(self):
        return [
            s.Task("a", kind="build", target_file="app.py", acceptance=["runs"]),
            s.Task("b", kind="build", target_file="util.py", acceptance=["runs"]),
        ]

    def _run(self, kind, *, initial="print('ok')", fix="", multi_fix=None,
             tasks=None, verifier=None):
        base = Path(tempfile.mkdtemp(prefix=f"d09-{kind}-"))
        project = base / "project"; project.mkdir()
        ws = base / "ws"
        cfg = settings(base)
        cfg.max_verify_loops = 1
        cfg.visual_check = False
        st = make_state(project, ws, "medium", tasks)
        st.phase = "BUILD"
        st.profile = "script"
        pool = PipelinePool({}, responder=self._responder(initial, fix, multi_fix))
        verifier = verifier or FakeVerifier()
        adapter = _make_adapter(kind, cfg, pool, st, verifier)
        store = StateManager(cfg.workspace_dir, cfg.log_dir)
        eng = engine.WorkflowEngine(st, store, adapter, emit=lambda *a: None)
        eng.apply_pool(pool)
        return {"kind": kind, "base": base, "project": project, "ws": ws,
                "settings": cfg, "state": st, "pool": pool,
                "verifier": verifier, "adapter": adapter, "engine": eng}

    def _drive(self, run, limit=16):
        results = []
        for _ in range(limit):
            result = run["engine"].step()
            results.append(result)
            if result in ("paused", "failed", "done"):
                break
        run["results"] = results
        return results[-1] if results else ""

    def _file_hash(self, run, rel):
        root = Path(run["state"].extensions["workspace_root"])
        return s.artifact_hash((root / rel).read_text(encoding="utf-8"))

    def _canon_fixes(self, st):
        out = []
        for item in (st.integration or {}).get("fix_records") or []:
            out.append({
                "task_ids": sorted(item.get("task_ids") or []),
                "files": sorted(item.get("files") or []),
                "outcome": item.get("outcome"),
                "revalidations": sorted(
                    ({"task_id": r.get("task_id"), "verdict": r.get("verdict"),
                      "delivered": bool(r.get("delivered"))}
                     for r in item.get("revalidations") or []),
                    key=lambda r: str(r["task_id"])),
            })
        return out

    def _successful_pause(self, kind):
        run = self._run(kind)
        self.assertEqual(self._drive(run), "paused", kind)
        self.assertEqual(run["state"].phase, "SIGNOFF", kind)
        self.assertEqual(run["state"].pause.reason, "signoff_approval", kind)
        self.assertEqual(run["state"].integration.get("verdict"), "PASS", kind)
        self.assertFalse(run["state"].signoff_approved(), kind)
        self.assertNotIn("publish", run["state"].extensions, kind)
        return run

    def test_d09_01a_successful_integration_and_approval(self):
        runs = {}
        for kind in ("factory", "native"):
            run = self._successful_pause(kind)
            approval = run["engine"].approve_signoff("reviewer", "evidence checked")
            self.assertEqual(run["engine"].run(), "done", kind)
            self.assertEqual(run["state"].phase, "DONE", kind)
            self.assertTrue(run["state"].signoff_approved(), kind)
            self.assertIn("publish", run["state"].extensions, kind)
            self.assertEqual(approval.artifact_hash, run["state"].signoff_hash(), kind)
            self.assertEqual(run["state"].tasks[0].status, "done", kind)
            runs[kind] = run
        ref, new = runs["factory"], runs["native"]
        self.assertEqual(ref["state"].signoff_hash(), new["state"].signoff_hash())
        self.assertEqual(
            [a.artifact_hash for a in ref["state"].approvals if a.kind == "signoff"],
            [a.artifact_hash for a in new["state"].approvals if a.kind == "signoff"])
        self.assertEqual(ref["state"].signoff_evidence(),
                         new["state"].signoff_evidence())
        self.assertEqual(ref["state"].phase, new["state"].phase)
        self.assertEqual(ref["pool"].calls, new["pool"].calls)
        self.assertEqual(ref["pool"].transport, new["pool"].transport)
        pub_f = ref["state"].extensions["publish"]
        pub_n = new["state"].extensions["publish"]
        self.assertEqual(pub_f.get("status"), "COMMITTED")
        self.assertEqual(pub_f.get("status"), pub_n.get("status"))
        self.assertEqual(pub_f.get("entries"), pub_n.get("entries"))
        self.assertEqual(pub_f.get("durability"), pub_n.get("durability"))
        self.assertEqual((ref["project"] / "app.py").read_bytes(),
                         (new["project"] / "app.py").read_bytes())

    def test_d09_01b_integration_failure_blocks_signoff(self):
        for kind in ("factory", "native"):
            run = self._run(kind, initial="raise RuntimeError('boom')", fix="")
            self.assertEqual(self._drive(run), "failed", kind)
            st = run["state"]
            self.assertEqual(st.phase, "INTEGRATE", kind)
            self.assertEqual(st.pause.reason, "integrate_failed", kind)
            self.assertEqual(st.integration.get("verdict"), "FAIL", kind)
            self.assertFalse(st.signoff_approved(), kind)
            self.assertFalse((run["ws"] / "project_memory.json").exists(), kind)
            self.assertNotIn("publish", st.extensions, kind)
            self.assertFalse(st.signoff_blockers() == [], kind)

    def test_d09_02a_fixer_revalidates_single_file(self):
        fix = "### FILE: app.py\n```python\nprint('fixed')\n```\n"
        runs = {}
        for kind in ("factory", "native"):
            run = self._run(kind, initial="raise RuntimeError('boom')", multi_fix=fix)
            self.assertEqual(self._drive(run), "paused", kind)
            st = run["state"]
            self.assertEqual(st.integration.get("verdict"), "PASS", kind)
            fixes = self._canon_fixes(st)
            self.assertEqual(len(fixes), 1, kind)
            self.assertEqual(fixes[0]["task_ids"], ["a"], kind)
            self.assertEqual(fixes[0]["files"], ["app.py"], kind)
            self.assertEqual(fixes[0]["revalidations"],
                             [{"task_id": "a", "verdict": "PASS",
                               "delivered": True}], kind)
            rec = st.verifications["a"]
            self.assertEqual(rec.oracle.get("artifact_sha256"),
                             self._file_hash(run, "app.py"), kind)
            self.assertEqual(st.tasks[0].status, "done", kind)
            runs[kind] = run
        self.assertEqual(runs["factory"]["state"].signoff_evidence(),
                         runs["native"]["state"].signoff_evidence())
        self.assertEqual(runs["factory"]["pool"].calls,
                         runs["native"]["pool"].calls)
        self.assertEqual(runs["factory"]["pool"].transport,
                         runs["native"]["pool"].transport)

    def test_d09_02b_fixer_revalidates_every_changed_file(self):
        fix = ("### FILE: app.py\n```python\nprint('fixed')\n```\n"
               "### FILE: util.py\n```python\nx = 3\n```\n")
        runs = {}
        for kind in ("factory", "native"):
            run = self._run(kind, initial="raise RuntimeError('boom')",
                            multi_fix=fix, tasks=self._tasks())
            self.assertEqual(self._drive(run), "paused", kind)
            st = run["state"]
            self.assertEqual(st.integration.get("verdict"), "PASS", kind)
            fixes = self._canon_fixes(st)
            self.assertEqual(len(fixes), 1, kind)
            self.assertEqual(fixes[0]["task_ids"], ["a", "b"], kind)
            self.assertEqual(fixes[0]["files"], ["app.py", "util.py"], kind)
            self.assertEqual(fixes[0]["revalidations"], [
                {"task_id": "a", "verdict": "PASS", "delivered": True},
                {"task_id": "b", "verdict": "PASS", "delivered": True},
            ], kind)
            for task_id, rel in (("a", "app.py"), ("b", "util.py")):
                rec = st.verifications[task_id]
                self.assertEqual(rec.oracle.get("artifact_sha256"),
                                 self._file_hash(run, rel), (kind, task_id))
                task = next(t for t in st.tasks if t.id == task_id)
                self.assertEqual(task.status, "done", (kind, task_id))
            runs[kind] = run
        self.assertEqual(runs["factory"]["state"].signoff_evidence(),
                         runs["native"]["state"].signoff_evidence())

    def test_d09_02c_revalidation_failure_blocks_integration(self):
        fix = "### FILE: app.py\n```python\nprint('fixed')\n```\n"
        for kind in ("factory", "native"):
            verifier = FakeVerifier(critic_verdicts=["PASS", "FAIL"])
            run = self._run(kind, initial="raise RuntimeError('boom')",
                            multi_fix=fix, verifier=verifier)
            self.assertEqual(self._drive(run), "failed", kind)
            st = run["state"]
            self.assertEqual(st.pause.reason, "integrate_failed", kind)
            self.assertEqual(st.integration.get("verdict"), "FAIL", kind)
            fixes = self._canon_fixes(st)
            self.assertEqual(len(fixes), 1, kind)
            self.assertEqual(fixes[0]["revalidations"],
                             [{"task_id": "a", "verdict": "FAIL",
                               "delivered": False}], kind)
            self.assertEqual(st.tasks[0].status, "failed", kind)
            self.assertFalse(st.signoff_approved(), kind)
            self.assertFalse((run["ws"] / "project_memory.json").exists(), kind)

    def test_d09_02c_oracle_fail_in_revalidation_skips_critic(self):
        fix = "### FILE: app.py\n```python\ndef broken(:\n```\n"
        for kind in ("factory", "native"):
            verifier = FakeVerifier()
            run = self._run(kind, initial="raise RuntimeError('boom')",
                            multi_fix=fix, verifier=verifier)
            self.assertEqual(self._drive(run), "failed", kind)
            st = run["state"]
            self.assertEqual(st.pause.reason, "integrate_failed", kind)
            fixes = self._canon_fixes(st)
            self.assertEqual(fixes[0]["revalidations"],
                             [{"task_id": "a", "verdict": "FAIL",
                               "delivered": False}], kind)
            self.assertEqual(st.tasks[0].status, "failed", kind)
            self.assertEqual(len(verifier.critic_calls), 1, kind)
            self.assertFalse(st.signoff_approved(), kind)

    def test_d09_02d_fixer_exhaustion_never_passes(self):
        bad_fix = "### FILE: app.py\n```python\nraise RuntimeError('boom')\n```\n"
        for kind in ("factory", "native"):
            run = self._run(kind, initial="raise RuntimeError('boom')",
                            multi_fix=bad_fix)
            self.assertEqual(self._drive(run), "failed", kind)
            st = run["state"]
            self.assertEqual(st.pause.reason, "integrate_failed", kind)
            self.assertEqual(st.integration.get("verdict"), "FAIL", kind)
            fixes = self._canon_fixes(st)
            self.assertEqual(len(fixes), 2, kind)
            self.assertTrue(all(r["verdict"] == "PASS"
                                for item in fixes
                                for r in item["revalidations"]), kind)
            self.assertFalse(st.signoff_approved(), kind)
            self.assertNotIn("publish", st.extensions, kind)

    def test_d09_03a_approval_persists_and_reloads(self):
        runs = {}
        for kind in ("factory", "native"):
            run = self._successful_pause(kind)
            approval = run["engine"].approve_signoff("reviewer", "checked")
            self.assertTrue(run["state"].signoff_approved(), kind)
            reloaded = s.ProjectState.from_dict(run["state"].to_dict())
            self.assertTrue(reloaded.signoff_approved(), kind)
            self.assertEqual(reloaded.signoff_hash(), approval.artifact_hash, kind)
            self.assertTrue(any(a.kind == "signoff" and a.decision == "approved"
                                and a.artifact_hash == approval.artifact_hash
                                for a in reloaded.approvals), kind)
            path = run["engine"].store.project_state_path(run["state"].project_root)
            persisted = run["engine"].store.load_project(path)
            self.assertTrue(persisted.signoff_approved(), kind)
            self.assertEqual(persisted.signoff_hash(), approval.artifact_hash, kind)
            runs[kind] = run
        self.assertEqual(
            [a.artifact_hash for a in runs["factory"]["state"].approvals
             if a.kind == "signoff"],
            [a.artifact_hash for a in runs["native"]["state"].approvals
             if a.kind == "signoff"])

    def test_d09_03b_missing_evidence_blocks_approval(self):
        for kind in ("factory", "native"):
            run = self._successful_pause(kind)
            del run["state"].verifications["a"]
            with self.assertRaises(s.StateValidationError):
                run["engine"].approve_signoff()
            self.assertFalse(any(a.kind == "signoff"
                                 for a in run["state"].approvals), kind)
            self.assertFalse(run["state"].signoff_approved(), kind)

    def test_d09_03c_artifact_change_invalidates_approval(self):
        for kind in ("factory", "native"):
            run = self._successful_pause(kind)
            old = run["engine"].approve_signoff()
            target = Path(run["state"].extensions["workspace_root"]) / "app.py"
            target.write_text("print('changed')\n", encoding="utf-8")
            self.assertFalse(run["state"].signoff_approved(), kind)
            with self.assertRaises(s.StateValidationError):
                run["engine"].approve_signoff()
            decisions = [a for a in run["state"].approvals
                         if a.kind == "signoff"]
            self.assertEqual([a.artifact_hash for a in decisions],
                             [old.artifact_hash], kind)
            self.assertEqual(run["state"].phase, "SIGNOFF", kind)

    def test_d09_04a_rejection_is_persisted_and_blocks_done(self):
        for kind in ("factory", "native"):
            run = self._successful_pause(kind)
            rejection = run["engine"].reject_signoff("human", "UI is wrong")
            self.assertEqual(run["state"].pause.reason,
                             "signoff_rejected", kind)
            self.assertEqual(run["engine"].step(), "paused", kind)
            self.assertNotEqual(run["state"].phase, "DONE", kind)
            self.assertFalse(run["state"].signoff_approved(), kind)
            self.assertEqual(rejection.decision, "rejected", kind)
            self.assertEqual(rejection.detail, "UI is wrong", kind)
            reloaded = s.ProjectState.from_dict(run["state"].to_dict())
            self.assertTrue(any(a.kind == "signoff" and a.decision == "rejected"
                                and a.detail == "UI is wrong"
                                for a in reloaded.approvals), kind)

    def test_d09_04b_rejection_then_approval_keeps_both(self):
        for kind in ("factory", "native"):
            run = self._successful_pause(kind)
            run["engine"].reject_signoff("human", "first attempt rejected")
            run["engine"].approve_signoff("reviewer", "fixed")
            self.assertEqual(run["engine"].run(), "done", kind)
            decisions = [a for a in run["state"].approvals
                         if a.kind == "signoff"]
            self.assertEqual([a.decision for a in decisions],
                             ["rejected", "approved"], kind)
            self.assertEqual(decisions[0].detail, "first attempt rejected", kind)
            self.assertEqual(run["state"].phase, "DONE", kind)

    def test_d09_04c_technical_block_is_not_human_rejection(self):
        for kind in ("factory", "native"):
            run = self._successful_pause(kind)
            run["state"].verifications["a"].verdict = "FAIL"
            with self.assertRaises(s.StateValidationError):
                run["engine"].approve_signoff()
            self.assertEqual(run["state"].pause.reason,
                             "signoff_approval", kind)
            decisions = [a for a in run["state"].approvals
                         if a.kind == "signoff"]
            self.assertEqual(decisions, [], kind)
            self.assertNotEqual(run["state"].phase, "DONE", kind)

    def test_d09_05a_oracle_and_critic_fail_block_signoff(self):
        for kind in ("factory", "native"):
            for failure in ("oracle", "critic"):
                run = self._successful_pause(kind)
                rec = run["state"].verifications["a"]
                rec.verdict = "FAIL"
                if failure == "oracle":
                    rec.oracle = {"passed": False}
                    rec.critic = {"verdict": "PASS"}
                else:
                    rec.oracle = {"passed": True}
                    rec.critic = {"verdict": "FAIL",
                                  "critic": {"ok": False}}
                with self.assertRaises(s.StateValidationError):
                    run["engine"].approve_signoff()
                self.assertFalse(run["state"].signoff_approved(), (kind, failure))
                self.assertNotEqual(run["state"].phase, "DONE", (kind, failure))

    def test_d09_05c_payg_unknown_blocks_without_repeat(self):
        script = {"gemini-3.1-pro-preview": "timeout"}
        runs = {}
        for kind in ("factory", "native"):
            run = run_pipeline(kind, script, "high",
                               registry_mode="payg_first")
            self.assertEqual(run["result"], "failed", kind)
            calls_before = list(run["pool"].calls)
            run["state"].pause = s.PauseState()
            self.assertEqual(run["engine"].step(), "paused", kind)
            self.assertEqual(run["state"].pause.reason,
                             "call_unknown_failed", kind)
            self.assertEqual(run["pool"].calls, calls_before, kind)
            self.assertNotEqual(run["state"].phase, "DONE", kind)
            self.assertFalse(run["state"].signoff_approved(), kind)
            runs[kind] = run
        self.assertEqual(len(runs["factory"]["pool"].calls), 1)
        self.assertEqual(len(runs["native"]["pool"].calls), 1)
        recs_f = runs["factory"]["ledger"].unresolved("r")
        recs_n = runs["native"]["ledger"].unresolved("r")
        self.assertEqual([(r.state, r.error_kind) for r in recs_f],
                         [(r.state, r.error_kind) for r in recs_n])

    def test_d09_05d_publisher_recovery_blocks_both_adapters(self):
        for kind in ("factory", "native"):
            run = self._successful_pause(kind)
            run["state"].pause = s.PauseState()
            run["adapter"].recovery_error = "journal corrompido"
            with self.assertRaises((engine.EngineError,
                                    native_adapter.NativeAdapterError)):
                run["engine"].step()
            self.assertEqual(run["state"].phase, "SIGNOFF", kind)
            self.assertNotIn("publish", run["state"].extensions, kind)

    def test_d09_05e_incomplete_integration_blocks_approval(self):
        for kind in ("factory", "native"):
            run = self._successful_pause(kind)
            run["state"].integration = {}
            with self.assertRaises(s.StateValidationError) as ctx:
                run["engine"].approve_signoff()
            self.assertIn("integração incompleta", str(ctx.exception), kind)
            self.assertFalse(any(a.kind == "signoff"
                                 for a in run["state"].approvals), kind)
            self.assertNotEqual(run["state"].phase, "DONE", kind)

    def test_d09_05f_missing_approval_prevents_done_and_publish(self):
        for kind in ("factory", "native"):
            run = self._successful_pause(kind)
            self.assertEqual(run["engine"].step(), "paused", kind)
            self.assertEqual(run["state"].phase, "SIGNOFF", kind)
            self.assertEqual(run["state"].pause.reason,
                             "signoff_approval", kind)
            self.assertFalse(run["state"].signoff_approved(), kind)
            self.assertNotIn("publish", run["state"].extensions, kind)
            self.assertFalse(any(a.kind == "signoff"
                                 for a in run["state"].approvals), kind)


@unittest.skipUnless(DEPS_OK, f"runtime deps missing: {DEPS_ERR}")
class D10ResumePipelineTests(unittest.TestCase):
    """D10: workflow-level resume and recovery across phases."""

    def _base(self, phase="BUILD", *, approved=True, profile="script",
              tasks=None, files=None, architecture=True, requirements=True):
        base = Path(tempfile.mkdtemp(prefix=f"d10-{phase.lower()}-"))
        project = base / "project"; project.mkdir()
        ws = base / "ws"
        st = s.ProjectState(run_id="r", project_root=str(project), goal="demo",
                            engine_kind=s.ENGINE_UNIFIED, phase=phase,
                            profile=profile)
        if requirements:
            st.requirements = s.RequirementContract(
                objective="demo",
                acceptance=[s.AcceptanceCriterion(id="ac1", text="runs")])
        if architecture:
            st.architecture = s.ArchitectureContract(
                approach="script", modules=[{"path": "app.py"}],
                interfaces=[{"name": "main", "file": "app.py"}])
            st.tasks = tasks or [
                s.Task("a", kind="build", target_file="app.py",
                       acceptance=["runs"], requirement_ids=["ac1"])]
        for rel, content in (files or {}).items():
            path = project / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        st.extensions["workspace_root"] = str(ws)
        if approved and architecture:
            st.approve_architecture()
        cfg = settings(base)
        store = StateManager(cfg.workspace_dir, cfg.log_dir)
        store.save_project(st)
        return {"base": base, "project": project, "ws": ws, "settings": cfg,
                "state": st, "store": store}

    def _engine(self, kind, run, *, responder=None, verifier=None,
                visual_check=False):
        cfg = settings(run["base"])
        cfg.max_verify_loops = 1
        cfg.visual_check = visual_check
        st = run["store"].load_project(
            run["store"].project_state_path(run["state"].project_root))
        if responder is None:
            def responder(ref, messages):
                raise AssertionError("unexpected model call")
        pool = PipelinePool({}, responder=responder)
        verifier = verifier or FakeVerifier()
        adapter = _make_adapter(kind, cfg, pool, st, verifier)
        eng = engine.WorkflowEngine(st, run["store"], adapter,
                                    emit=lambda *a: None)
        eng.apply_pool(pool)
        return {"kind": kind, "base": run["base"], "project": run["project"],
                "ws": run["ws"], "settings": cfg, "state": st, "pool": pool,
                "verifier": verifier, "adapter": adapter, "engine": eng}

    @staticmethod
    def _verification(st, rel, content):
        return s.VerificationRecord(
            verdict="PASS",
            oracle={"passed": True,
                    "artifact_sha256": s.artifact_hash(content)})

    def _assert_persisted_blocker(self, st, text):
        self.assertTrue(any(text in b for b in st.verification_blockers()), st)

    def test_d10_01a_spec_persisted_without_pause_does_not_regenerate(self):
        for kind in ("factory", "native"):
            run = self._base("SPEC", architecture=False, tasks=[])
            eng = self._engine(kind, run)["engine"]
            self.assertEqual(eng.step(), "paused", kind)
            st = eng.state
            self.assertEqual(st.phase, "SPEC", kind)
            self.assertEqual(st.pause.reason, "spec_approval", kind)

    def test_d10_01b_incomplete_spec_pauses_without_model(self):
        for kind in ("factory", "native"):
            run = self._base("SPEC", architecture=False, tasks=[])
            run["state"].requirements.open_questions = ["which database?"]
            run["store"].save_project(run["state"])
            eng = self._engine(kind, run)["engine"]
            self.assertEqual(eng.step(), "paused", kind)
            self.assertEqual(eng.state.pause.reason, "spec_questions", kind)

    def test_d10_01d_fresh_spec_recovers_from_scratch(self):
        spec = json.dumps({
            "objective": "demo",
            "scope_in": [], "scope_out": [], "constraints": [],
            "non_negotiables": [],
            "acceptance": [{"id": "ac1", "text": "runs"}],
            "open_questions": [], "assumptions": [], "answers": [],
        })
        for kind in ("factory", "native"):
            run = self._base("SPEC", architecture=False, tasks=[],
                             requirements=False)

            def responder(ref, messages):
                return spec

            eng = self._engine(kind, run, responder=responder)
            self.assertEqual(eng["engine"].step(), "paused", kind)
            self.assertEqual(eng["state"].pause.reason, "spec_approval", kind)
            self.assertEqual(eng["state"].requirements.objective, "demo", kind)

    def test_d10_02b_fresh_architecture_recovers_from_scratch(self):
        arch = json.dumps({
            "approach": "script",
            "modules": [{"path": "app.py", "responsibility": "main"}],
            "interfaces": [{"name": "main", "file": "app.py",
                            "inputs": [], "outputs": [], "errors": [],
                            "used_by": []}],
            "tasks": [{"id": "a", "title": "run", "description": "",
                       "target_file": "app.py", "acceptance": ["runs"],
                       "depends_on": [], "requirement_ids": ["ac1"],
                       "complexity": "low", "role": "junior"}],
            "test_plan": [],
        })
        for kind in ("factory", "native"):
            run = self._base("ARCHITECTURE", approved=False, architecture=False)

            def responder(ref, messages):
                return arch

            eng = self._engine(kind, run, responder=responder)
            self.assertEqual(eng["engine"].step(), "paused", kind)
            self.assertEqual(eng["state"].pause.reason,
                             "architecture_approval", kind)
            self.assertEqual([t.id for t in eng["state"].tasks], ["a"], kind)

    def test_d10_02a_architecture_persisted_without_approval_is_not_redesigned(self):
        for kind in ("factory", "native"):
            run = self._base("ARCHITECTURE", approved=False)
            before = [t.to_dict() for t in run["state"].tasks]
            eng = self._engine(kind, run)["engine"]
            self.assertEqual(eng.step(), "paused", kind)
            st = eng.state
            self.assertEqual(st.pause.reason, "architecture_approval", kind)
            self.assertEqual([t.to_dict() for t in st.tasks], before, kind)

    def test_d10_03a_scaffold_is_idempotent_and_preserves_files(self):
        for kind in ("factory", "native"):
            run = self._base("SCAFFOLD", files={"app.py": "keep\n"})
            eng = self._engine(kind, run)
            self.assertEqual(eng["engine"].step(), "ok", kind)
            first = (run["ws"] / "app.py").read_text(encoding="utf-8")
            st = eng["state"]
            manifest = dict(st.scaffold)
            st.phase = "SCAFFOLD"
            run["store"].save_project(st)
            eng2 = self._engine(kind, run)
            self.assertEqual(eng2["engine"].step(), "ok", kind)
            self.assertEqual((run["ws"] / "app.py").read_text(encoding="utf-8"),
                             first, kind)
            self.assertEqual(first, "keep\n", kind)
            self.assertEqual(eng2["state"].scaffold, manifest, kind)

    def test_d10_03b_scaffold_interruption_is_idempotent(self):
        import engine as engine_mod
        import native_adapter as native_mod
        orig_engine = engine_mod.scaffold
        orig_native = native_mod.scaffold
        try:
            for kind in ("factory", "native"):
                run = self._base("SCAFFOLD")
                eng = self._engine(kind, run)

                def crash_scaffold(root, *a, **k):
                    Path(root, "partial.py").write_text("partial\n",
                                                       encoding="utf-8")
                    raise SimulatedCrash("during scaffold")

                if kind == "factory":
                    engine_mod.scaffold = crash_scaffold
                else:
                    native_mod.scaffold = crash_scaffold
                with self.assertRaises(SimulatedCrash):
                    eng["engine"].step()
                engine_mod.scaffold = orig_engine
                native_mod.scaffold = orig_native
                eng2 = self._engine(kind, run)
                self.assertEqual(eng2["engine"].step(), "ok", kind)
                self.assertEqual((run["ws"] / "partial.py").read_text(
                    encoding="utf-8"), "partial\n", kind)
                self.assertTrue(eng2["state"].scaffold, kind)
        finally:
            engine_mod.scaffold = orig_engine
            native_mod.scaffold = orig_native

    def test_d10_04a_completed_build_task_is_not_repeated(self):
        content = "print('ok')\n"
        for kind in ("factory", "native"):
            run = self._base("BUILD", files={"app.py": content})
            run["state"].tasks[0].status = "done"
            run["state"].verifications["a"] = self._verification(
                run["state"], "app.py", content)
            run["store"].save_project(run["state"])
            eng = self._engine(kind, run)
            self.assertEqual(eng["engine"].step(), "ok", kind)
            st = eng["state"]
            self.assertEqual(st.phase, "VERIFY", kind)
            self.assertEqual(eng["pool"].calls, [], kind)
            self.assertEqual(len(st.tasks[0].attempts), 0, kind)

    def test_d10_05a_verify_resumes_persisted_evidence(self):
        content = "print('ok')\n"
        for kind in ("factory", "native"):
            run = self._base("VERIFY", files={"app.py": content})
            run["state"].tasks[0].status = "done"
            run["state"].verifications["a"] = self._verification(
                run["state"], "app.py", content)
            run["store"].save_project(run["state"])
            eng = self._engine(kind, run)
            self.assertEqual(eng["engine"].step(), "ok", kind)
            self.assertEqual(eng["state"].phase, "INTEGRATE", kind)
            self.assertEqual(eng["pool"].calls, [], kind)

    def test_d10_05c_legacy_pass_without_artifact_hash_is_fail_closed(self):
        content = "print('ok')\n"
        for kind in ("factory", "native"):
            run = self._base("VERIFY", files={"app.py": content})
            run["ws"].mkdir(parents=True, exist_ok=True)
            (run["ws"] / "app.py").write_text(content, encoding="utf-8")
            run["state"].tasks[0].status = "done"
            run["state"].verifications["a"] = s.VerificationRecord(
                verdict="PASS", oracle={"passed": True})
            run["store"].save_project(run["state"])
            reloaded = run["store"].load_project(
                run["store"].project_state_path(run["state"].project_root))
            self.assertIn("evidência sem artifact hash: a",
                          reloaded.verification_blockers(), kind)
            self.assertEqual(reloaded.stale_verification_tasks(), ["a"], kind)
            eng = self._engine(kind, run)
            self.assertEqual(eng["engine"].step(), "failed", kind)
            self.assertEqual(eng["state"].pause.reason, "verify_failed", kind)

            run = self._base("VERIFY", files={"app.py": content})
            run["state"].tasks[0].status = "done"
            run["state"].verifications["a"] = s.VerificationRecord(
                verdict="N/A", oracle={"passed": True})
            run["store"].save_project(run["state"])
            eng = self._engine(kind, run)
            self.assertEqual(eng["engine"].step(), "failed", kind)
            self.assertIn("verificação inválida: a",
                          eng["state"].verification_blockers(), kind)

    def test_d10_05d_legacy_pass_without_hash_is_revalidated_on_integrate(self):
        content = "print('ok')\n"
        for kind in ("factory", "native"):
            run = self._base("INTEGRATE", files={"app.py": content})
            run["ws"].mkdir(parents=True, exist_ok=True)
            (run["ws"] / "app.py").write_text(content, encoding="utf-8")
            run["state"].tasks[0].status = "done"
            run["state"].verifications["a"] = s.VerificationRecord(
                verdict="PASS", oracle={"passed": True})
            run["store"].save_project(run["state"])
            eng = self._engine(kind, run)
            self.assertEqual(eng["engine"].step(), "ok", kind)
            st = eng["state"]
            self.assertEqual(st.verifications["a"].oracle.get(
                "artifact_sha256"), s.artifact_hash(content), kind)
            fixes = st.integration.get("fix_records") or []
            self.assertTrue(any(f.get("outcome") == "recovered" for f in fixes),
                            kind)

    def test_d10_05b_verify_rejects_missing_and_stale_evidence(self):
        for kind in ("factory", "native"):
            run = self._base("VERIFY", files={"app.py": "new\n"})
            run["state"].tasks[0].status = "done"
            run["store"].save_project(run["state"])
            eng = self._engine(kind, run)
            self.assertEqual(eng["engine"].step(), "failed", kind)
            self.assertEqual(eng["state"].pause.reason, "verify_failed", kind)

            run = self._base("VERIFY", files={"app.py": "new\n"})
            run["state"].tasks[0].status = "done"
            run["state"].verifications["a"] = s.VerificationRecord(
                verdict="PASS",
                oracle={"passed": True,
                        "artifact_sha256": s.artifact_hash("old\n")})
            run["store"].save_project(run["state"])
            eng = self._engine(kind, run)
            self.assertEqual(eng["engine"].step(), "failed", kind)
            st = eng["state"]
            self.assertEqual(st.pause.reason, "verify_failed", kind)
            self._assert_persisted_blocker(st, "evidência desatualizada")

    def test_d10_06a_integration_revalidates_stale_file_on_resume(self):
        content = "print('fixed')\n"
        for kind in ("factory", "native"):
            run = self._base("INTEGRATE", files={"app.py": content})
            run["state"].tasks[0].status = "done"
            run["state"].verifications["a"] = s.VerificationRecord(
                verdict="PASS",
                oracle={"passed": True,
                        "artifact_sha256": s.artifact_hash("old\n")})
            run["store"].save_project(run["state"])
            eng = self._engine(kind, run)
            self.assertEqual(eng["engine"].step(), "ok", kind)
            st = eng["state"]
            self.assertEqual(st.integration.get("verdict"), "PASS", kind)
            self.assertEqual(st.phase, "SIGNOFF", kind)
            self.assertEqual(eng["pool"].calls, [], kind)
            rec = st.verifications["a"]
            self.assertEqual(rec.oracle.get("artifact_sha256"),
                             s.artifact_hash(content), kind)
            fixes = st.integration.get("fix_records") or []
            self.assertTrue(any(f.get("outcome") == "recovered" for f in fixes),
                            kind)

    def test_d10_07a_visual_evidence_is_bound_to_artifact_hash(self):
        import visual as visual_mod
        old_native_audit = visual_mod.playwright_audit
        try:
            visual_mod.playwright_audit = lambda root, logs, **k: {
                "verdict": "PASS", "screenshot": ""}
            for kind in ("factory", "native"):
                run = self._base(
                    "VISUAL", profile="web",
                    tasks=[s.Task("a", kind="build", target_file="index.html",
                                  acceptance=["renders"], requirement_ids=["ac1"])],
                    files={"index.html": "<html>ok</html>\n"})
                run["state"].tasks[0].status = "done"
                run["state"].verifications["a"] = self._verification(
                    run["state"], "index.html", "<html>ok</html>\n")
                run["store"].save_project(run["state"])
                eng = self._engine(kind, run, visual_check=True)
                if kind == "factory":
                    eng["adapter"].factory.playwright_audit = lambda root: {
                        "verdict": "PASS", "screenshot": ""}
                self.assertEqual(eng["engine"].step(), "ok", kind)
                st = eng["state"]
                hashes = st.visual.get("artifact_hashes") or {}
                self.assertEqual(hashes.get("index.html"),
                                 s.artifact_hash("<html>ok</html>\n"), kind)
                (run["ws"] / "index.html").write_text("<html>changed</html>\n",
                                                      encoding="utf-8")
                self.assertTrue(any("visual desatualizado" in b
                                    for b in st.signoff_blockers()), kind)
        finally:
            visual_mod.playwright_audit = old_native_audit

    def test_d10_08a_approved_signoff_resume_skips_generation(self):
        content = "print('ok')\n"
        for kind in ("factory", "native"):
            run = self._base("SIGNOFF", files={"app.py": content})
            seed = self._engine(kind, run)
            st = seed["state"]
            st.tasks[0].status = "done"
            st.verifications["a"] = self._verification(st, "app.py", content)
            st.integration = {"verdict": "PASS", "kind": "python", "exit": 0}
            st.visual = {"verdict": "N/A"}
            st.signoff = {"goal": "demo"}
            st.approve_signoff("reviewer", "checked")
            run["store"].save_project(st)
            eng = self._engine(kind, run)
            self.assertEqual(eng["engine"].step(), "ok", kind)
            self.assertEqual(eng["state"].phase, "DONE", kind)
            self.assertEqual(eng["pool"].calls, [], kind)
            self.assertIn("publish", eng["state"].extensions, kind)

    def test_d10_09a_recovered_committed_publication_is_not_repeated(self):
        content = "print('ok')\n"
        for kind in ("factory", "native"):
            run = self._base("SIGNOFF", files={"app.py": content})
            seed = self._engine(kind, run)
            st = seed["state"]
            st.tasks[0].status = "done"
            st.verifications["a"] = self._verification(st, "app.py", content)
            st.integration = {"verdict": "PASS", "kind": "python", "exit": 0}
            st.visual = {"verdict": "N/A"}
            st.signoff = {"goal": "demo"}
            st.approve_signoff("reviewer", "checked")
            run["store"].save_project(st)
            eng = self._engine(kind, run)
            first = eng["adapter"].publish()
            self.assertEqual(first.get("status"), "COMMITTED", kind)
            self.assertEqual(eng["engine"].step(), "ok", kind)
            self.assertEqual(eng["state"].phase, "DONE", kind)
            recovered = eng["state"].extensions.get("publish") or {}
            self.assertEqual(recovered.get("id"), first.get("id"), kind)
            self.assertEqual(eng["pool"].calls, [], kind)

    def _worker(self, mode, kind):
        base = Path(tempfile.mkdtemp(prefix=f"d10-{mode}-{kind}-"))
        worker = Path(__file__).resolve().parent / "d10_crash_worker.py"
        proc = subprocess.run([sys.executable, str(worker), mode, kind,
                               str(base)], cwd=str(Path(__file__).resolve().parents[1]),
                              capture_output=True, text=True, timeout=90)
        self.assertNotEqual(proc.returncode, 0, proc.stderr)
        cfg = settings(base)
        store = StateManager(cfg.workspace_dir, cfg.log_dir)
        project = base / "project"
        st = store.load_project(store.project_state_path(project))
        return {"base": base, "project": project, "ws": base / "ws",
                "settings": cfg, "state": st, "store": store}

    def test_d10_11a_real_process_build_confirm_before_attempt(self):
        for kind in ("factory", "native"):
            run = self._worker("build-confirm", kind)
            self.assertEqual(run["state"].phase, "BUILD", kind)
            eng = self._engine(kind, run)
            self.assertEqual(eng["engine"].step(), "ok", kind)
            self.assertEqual(eng["pool"].calls, [], kind)
            self.assertEqual(len(eng["state"].tasks[0].attempts), 1, kind)
            self.assertTrue(eng["engine"].call_ledger.all()[0].consumed, kind)

    def test_d10_11b_real_process_build_attempt_before_consume(self):
        for kind in ("factory", "native"):
            run = self._worker("build-consume", kind)
            self.assertEqual(run["state"].phase, "BUILD", kind)
            self.assertEqual(len(run["state"].tasks[0].attempts), 1, kind)
            eng = self._engine(kind, run)
            self.assertEqual(eng["engine"].step(), "ok", kind)
            self.assertEqual(eng["pool"].calls, [], kind)
            self.assertEqual(len(eng["state"].tasks[0].attempts), 1, kind)
            call_id = eng["state"].tasks[0].attempts[0].call_id
            self.assertTrue(eng["engine"].call_ledger.get(call_id).consumed, kind)

    def test_d10_11c_real_process_integration_after_fix_before_revalidation(self):
        for kind in ("factory", "native"):
            run = self._worker("integrate-fix", kind)
            self.assertEqual(run["state"].phase, "INTEGRATE", kind)
            self.assertEqual(run["state"].verifications["a"].oracle.get(
                "artifact_sha256"), s.artifact_hash("raise RuntimeError('boom')\n"), kind)
            eng = self._engine(kind, run)
            self.assertEqual(eng["engine"].step(), "ok", kind)
            st = eng["state"]
            self.assertEqual(st.integration.get("verdict"), "PASS", kind)
            self.assertEqual(st.verifications["a"].oracle.get("artifact_sha256"),
                             s.artifact_hash("print('fixed')\n"), kind)
            self.assertEqual(eng["pool"].calls, [], kind)
            self.assertTrue(any(f.get("outcome") == "recovered"
                                for f in st.integration.get("fix_records") or []),
                            kind)

    def test_d10_11d_real_process_signoff_after_approval(self):
        for kind in ("factory", "native"):
            run = self._worker("signoff-approve", kind)
            self.assertEqual(run["state"].phase, "SIGNOFF", kind)
            self.assertTrue(run["state"].signoff_approved(), kind)
            eng = self._engine(kind, run)
            self.assertEqual(eng["engine"].step(), "ok", kind)
            self.assertEqual(eng["state"].phase, "DONE", kind)
            self.assertEqual(eng["pool"].calls, [], kind)
            self.assertIn("publish", eng["state"].extensions, kind)

    def test_d10_11e_real_process_signoff_after_rejection(self):
        for kind in ("factory", "native"):
            run = self._worker("signoff-reject", kind)
            self.assertEqual(run["state"].phase, "SIGNOFF", kind)
            self.assertEqual(run["state"].pause.reason,
                             "signoff_rejected", kind)
            eng = self._engine(kind, run)
            self.assertEqual(eng["engine"].step(), "paused", kind)
            self.assertEqual(eng["state"].phase, "SIGNOFF", kind)
            self.assertEqual(eng["pool"].calls, [], kind)
            self.assertTrue(any(a.decision == "rejected"
                                for a in eng["state"].approvals
                                if a.kind == "signoff"), kind)

    def test_d10_11f_real_process_publication_recovery_pending(self):
        for kind in ("factory", "native"):
            run = self._worker("publication-pending", kind)
            self.assertTrue(run["state"].signoff_approved(), kind)
            eng = self._engine(kind, run)
            try:
                result = eng["engine"].step()
            except (engine.EngineError, native_adapter.NativeAdapterError):
                result = "raised"
            self.assertIn(result, ("failed", "raised"), kind)
            self.assertEqual(eng["state"].phase, "SIGNOFF", kind)
            self.assertNotEqual(eng["state"].phase, "DONE", kind)
            self.assertNotIn("publish", eng["state"].extensions, kind)

    def test_d10_01c_spec_approval_resume_advances_without_regeneration(self):
        for kind in ("factory", "native"):
            run = self._base("SPEC", architecture=False, tasks=[])
            run["state"].approvals.append(s.Approval(
                kind="spec", artifact_hash=run["state"].requirements.content_hash(),
                decision="approved", timestamp=s.utc_now()))
            run["store"].save_project(run["state"])
            eng = self._engine(kind, run)
            self.assertEqual(eng["engine"].step(), "ok", kind)
            self.assertEqual(eng["state"].phase, "ARCHITECTURE", kind)
            self.assertEqual(eng["pool"].calls, [], kind)

    def test_d10_04b_payg_unknown_blocks_without_retry(self):
        script = {"gemini-3.1-pro-preview": "timeout"}
        for kind in ("factory", "native"):
            run = run_pipeline(kind, script, "high", registry_mode="payg_first")
            self.assertEqual(run["result"], "failed", kind)
            calls = list(run["pool"].calls)
            run["state"].pause = s.PauseState()
            self.assertEqual(run["engine"].step(), "paused", kind)
            self.assertEqual(run["state"].pause.reason,
                             "call_unknown_failed", kind)
            self.assertEqual(run["pool"].calls, calls, kind)
            self.assertEqual(len(calls), 1, kind)

    def test_d10_06b_integration_failure_blocks_signoff(self):
        for kind in ("factory", "native"):
            run = self._base("INTEGRATE", files={
                "app.py": "raise RuntimeError('boom')\n"})
            run["state"].tasks[0].status = "done"
            run["state"].verifications["a"] = self._verification(
                run["state"], "app.py", "raise RuntimeError('boom')\n")
            run["store"].save_project(run["state"])

            def responder(ref, messages):
                return ""

            eng = self._engine(kind, run, responder=responder)
            self.assertEqual(eng["engine"].step(), "failed", kind)
            self.assertEqual(eng["state"].pause.reason,
                             "integrate_failed", kind)
            self.assertNotEqual(eng["state"].phase, "SIGNOFF", kind)

    def test_d10_07c_visual_interruption_during_browser_is_retried(self):
        import visual as visual_mod
        orig = visual_mod.playwright_audit
        try:
            for kind in ("factory", "native"):
                run = self._base(
                    "VISUAL", profile="web",
                    tasks=[s.Task("a", kind="build", target_file="index.html",
                                  acceptance=["renders"], requirement_ids=["ac1"])],
                    files={"index.html": "<html>ok</html>\n"})
                run["state"].tasks[0].status = "done"
                run["state"].verifications["a"] = self._verification(
                    run["state"], "index.html", "<html>ok</html>\n")
                run["store"].save_project(run["state"])
                eng = self._engine(kind, run, visual_check=True)

                def crash_audit(*a, **k):
                    raise SimulatedCrash("during browser")

                if kind == "factory":
                    eng["adapter"].factory.playwright_audit = crash_audit
                else:
                    visual_mod.playwright_audit = crash_audit
                with self.assertRaises(SimulatedCrash):
                    eng["engine"].step()
                visual_mod.playwright_audit = orig
                eng2 = self._engine(kind, run, visual_check=True)
                if kind == "factory":
                    eng2["adapter"].factory.playwright_audit = lambda root: {
                        "verdict": "PASS", "screenshot": ""}
                else:
                    visual_mod.playwright_audit = lambda root, logs, **k: {
                        "verdict": "PASS", "screenshot": ""}
                self.assertEqual(eng2["engine"].step(), "ok", kind)
                self.assertEqual(eng2["state"].visual.get("verdict"), "PASS",
                                 kind)
        finally:
            visual_mod.playwright_audit = orig

    def test_d10_07b_visual_unavailable_blocks_required_profile(self):
        import visual as visual_mod
        old = visual_mod.playwright_audit
        try:
            visual_mod.playwright_audit = lambda root, logs, **k: {
                "verdict": "UNAVAILABLE", "note": "playwright unavailable",
                "screenshot": ""}
            for kind in ("factory", "native"):
                run = self._base(
                    "VISUAL", profile="web",
                    tasks=[s.Task("a", kind="build", target_file="index.html",
                                  acceptance=["renders"], requirement_ids=["ac1"])],
                    files={"index.html": "<html>ok</html>\n"})
                run["state"].tasks[0].status = "done"
                run["state"].verifications["a"] = self._verification(
                    run["state"], "index.html", "<html>ok</html>\n")
                run["store"].save_project(run["state"])
                eng = self._engine(kind, run, visual_check=True)
                if kind == "factory":
                    eng["adapter"].factory.playwright_audit = lambda root: {
                        "verdict": "UNAVAILABLE", "note": "unavailable",
                        "screenshot": ""}
                self.assertEqual(eng["engine"].step(), "failed", kind)
                self.assertEqual(eng["state"].pause.reason,
                                 "visual_failed", kind)
        finally:
            visual_mod.playwright_audit = old

    def test_d10_08b_rejection_then_approval_survives_reload(self):
        content = "print('ok')\n"
        for kind in ("factory", "native"):
            run = self._base("SIGNOFF", files={"app.py": content})
            seed = self._engine(kind, run)
            st = seed["state"]
            st.tasks[0].status = "done"
            st.verifications["a"] = self._verification(st, "app.py", content)
            st.integration = {"verdict": "PASS", "kind": "python", "exit": 0}
            st.visual = {"verdict": "N/A"}
            st.signoff = {"goal": "demo"}
            run["store"].save_project(st)
            eng = self._engine(kind, run)
            eng["engine"].reject_signoff("human", "not ready")
            run["store"].save_project(eng["state"])
            eng2 = self._engine(kind, run)
            self.assertEqual(eng2["engine"].step(), "paused", kind)
            eng2["engine"].approve_signoff("reviewer", "ready")
            run["store"].save_project(eng2["state"])
            eng3 = self._engine(kind, run)
            self.assertEqual(eng3["engine"].step(), "ok", kind)
            decisions = [a for a in eng3["state"].approvals
                         if a.kind == "signoff"]
            self.assertEqual([a.decision for a in decisions],
                             ["rejected", "approved"], kind)
            self.assertEqual(eng3["pool"].calls, [], kind)

    def test_d10_09b_rolled_back_journal_is_not_treated_as_success(self):
        content = "print('ok')\n"
        for kind in ("factory", "native"):
            run = self._base("SIGNOFF", files={"app.py": content})
            seed = self._engine(kind, run)
            st = seed["state"]
            st.tasks[0].status = "done"
            st.verifications["a"] = self._verification(st, "app.py", content)
            st.integration = {"verdict": "PASS", "kind": "python", "exit": 0}
            st.visual = {"verdict": "N/A"}
            st.signoff = {"goal": "demo"}
            st.approve_signoff("reviewer", "checked")
            run["store"].save_project(st)
            old_dir = run["project"] / ".crew" / "publish" / "old-rollback"
            old_dir.mkdir(parents=True, exist_ok=True)
            (old_dir / "journal.json").write_text(
                '{"id":"old-rollback","status":"ROLLED_BACK","entries":[]}',
                encoding="utf-8")
            eng = self._engine(kind, run)
            self.assertEqual(eng["engine"].step(), "ok", kind)
            self.assertEqual(eng["state"].phase, "DONE", kind)
            published = eng["state"].extensions.get("publish") or {}
            self.assertEqual(published.get("status"), "COMMITTED", kind)
            self.assertNotEqual(published.get("id"), "old-rollback", kind)
            journal = (old_dir / "journal.json").read_text(encoding="utf-8")
            self.assertIn("ROLLED_BACK", journal, kind)

    def test_d10_10a_dependency_resume_runs_only_pending_task(self):
        content = "print('ok')\n"
        tasks = [
            s.Task("a", kind="build", target_file="app.py",
                   acceptance=["runs"], requirement_ids=["ac1"]),
            s.Task("b", kind="build", target_file="util.py",
                   acceptance=["runs"], requirement_ids=["ac1"]),
        ]
        for kind in ("factory", "native"):
            run = self._base("BUILD", files={"app.py": content,
                                             "util.py": "x = 1\n"},
                             tasks=tasks)
            run["state"].tasks[0].status = "done"
            run["state"].verifications["a"] = self._verification(
                run["state"], "app.py", content)
            run["store"].save_project(run["state"])

            def responder(ref, messages):
                return "```python\nx = 2\n```"

            eng = self._engine(kind, run, responder=responder)
            self.assertEqual(eng["engine"].step(), "ok", kind)
            st = eng["state"]
            self.assertEqual(eng["pool"].task_order, ["b"], kind)
            self.assertEqual(st.tasks[0].status, "done", kind)
            self.assertEqual(st.tasks[1].status, "done", kind)


if __name__ == "__main__":
    unittest.main(verbosity=2)
