"""P7.2c.3-B: enabled ARCHITECTURE/BUILD through real adapters/services."""
import dataclasses
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import discovery
import engine
import state as s
from store import StateManager
from router import identity_key
from workflow_context import ContextActivation, ContextRecoveryRequired
from test_p7_pipeline_adapters import (FakeVerifier, PipelinePool, _make_adapter,
                                      make_state, settings)

ARCH_JSON = json.dumps({
    "approach": "managed architecture",
    "modules": [{"path": "app.py", "responsibility": "entry"}],
    "interfaces": [{"name": "main", "file": "app.py", "inputs": [],
                    "outputs": [], "errors": [], "used_by": []}],
    "tasks": [{"id": "a", "title": "app.py", "description": "entry point",
               "target_file": "app.py", "acceptance": ["runs"],
               "depends_on": [], "requirement_ids": [], "complexity": "high",
               "role": "senior"}],
    "test_plan": [{"task": "a", "check": "runs"}],
})


class Harness:
    def __init__(self, kind, *, enabled=True, first_only=False,
                 verifier=None, mode="full"):
        self.td = tempfile.TemporaryDirectory()
        self.base = Path(self.td.name)
        project = self.base / "project"; project.mkdir()
        ws = self.base / "ws"
        (project / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
        self.cfg = settings(self.base)
        self.cfg.test_oracle = False
        st = make_state(project, ws, "high")
        st.requirements = s.RequirementContract(
            objective="app main function",
            acceptance=[s.AcceptanceCriterion(id="ac1", text="runs")])
        store = StateManager(self.cfg.workspace_dir, self.cfg.log_dir)
        store.save_project(st)
        discovery.discover_project(st, store)
        self.state = store.load_project(store.project_state_path(project))
        self.store = store
        self.captured = []

        def responder(ref, messages):
            blob = "\n".join(str(getattr(m, "content", m)) for m in messages)
            self.captured.append(blob)
            if "Design the solution" in blob:
                return ARCH_JSON
            return "```python\nprint('ok')\n```"

        self.pool = PipelinePool({}, registry_mode=mode, responder=responder)
        self.pool.registry.models = {
            k: dataclasses.replace(v, context_window=200000)
            for k, v in self.pool.registry.models.items()}
        self.adapter = _make_adapter(kind, self.cfg, self.pool, self.state,
                                     verifier or FakeVerifier())
        self.engine = engine.WorkflowEngine(self.state, self.store, self.adapter)
        self.engine.apply_pool(self.pool)
        if enabled:
            providers = tuple(identity_key(i)
                              for i in self.pool.registry.models.values())
            if first_only:
                providers = providers[:1]
            self.engine.context_core().activate(ContextActivation(
                run_id=self.state.run_id,
                phases=("ARCHITECTURE", "BUILD"),
                providers=providers, actor="human:test"))
        self.core = self.engine.context_core()

    def close(self):
        self.td.cleanup()

    def snapshot(self, logical_id):
        return self.core.store.load(logical_id)

    def record(self, logical_id):
        return self.state.extensions["workflow_context"]["records"][logical_id]

    def ledger_for(self, logical_id):
        return [r for r in self.engine.call_ledger.all()
                if r.context_snapshot_id == logical_id]

    def _route(self):
        from router import RouteRequest
        req = RouteRequest("architecture", complexity="high", tag="ARCHITECT",
                           run_id=self.state.run_id)
        return self.pool.router.route(req)

    @staticmethod
    def _messages():
        from langchain_core.messages import HumanMessage, SystemMessage
        return [SystemMessage("s"), HumanMessage("h")]


class ArchitectureIntegrationTests(unittest.TestCase):
    def test_native_enabled_architecture_injects_and_commits(self):
        h = Harness("native")
        try:
            self.assertTrue(h.adapter.step("ARCHITECTURE"))
            self.assertIn("UNTRUSTED DOSSIER DATA", h.captured[-1])
            snap = h.snapshot("architecture-r")
            self.assertTrue(snap.payload["selection"]["block"])
            self.assertEqual(h.record("architecture-r")["state"], "APPLIED")
            recs = h.ledger_for("architecture-r")
            self.assertEqual(len(recs), 1)
            self.assertTrue(recs[0].consumed)
            self.assertEqual(h.state.architecture.approach, "managed architecture")
            self.assertEqual(len(h.captured), 1)
        finally:
            h.close()

    def test_factory_enabled_architecture_injects_and_commits(self):
        h = Harness("factory")
        try:
            self.assertTrue(h.adapter.step("ARCHITECTURE"))
            self.assertIn("UNTRUSTED DOSSIER DATA", h.captured[-1])
            self.assertEqual(h.record("architecture-r")["state"], "APPLIED")
            self.assertTrue(h.ledger_for("architecture-r")[0].consumed)
            self.assertEqual(h.state.architecture.approach, "managed architecture")
            self.assertEqual(len(h.captured), 1)
        finally:
            h.close()

    def test_architecture_parity_selected_items(self):
        a, b = Harness("native"), Harness("factory")
        try:
            a.adapter.step("ARCHITECTURE"); b.adapter.step("ARCHITECTURE")
            sa = a.snapshot("architecture-r").payload["selection"]
            sb = b.snapshot("architecture-r").payload["selection"]
            self.assertTrue(sa["block"] and sb["block"])
            self.assertEqual(sa["items"], sb["items"])
        finally:
            a.close(); b.close()

    def test_disabled_architecture_unchanged_no_artifacts(self):
        h = Harness("native", enabled=False)
        try:
            self.assertTrue(h.adapter.step("ARCHITECTURE"))
            self.assertNotIn("UNTRUSTED DOSSIER DATA", h.captured[-1])
            self.assertFalse(h.core.store.root.exists())
            self.assertEqual(h.engine.call_ledger.all(), [])
        finally:
            h.close()

    def test_disclosure_denied_uses_exact_base_prompt(self):
        h = Harness("native", first_only=True)
        try:
            self.assertTrue(h.adapter.step("ARCHITECTURE"))
            snap = h.snapshot("architecture-r")
            self.assertEqual(snap.payload["selection"]["block"], "")
            self.assertIn("disclosure_denied",
                          snap.payload["selection"]["omissions"])
            self.assertEqual(snap.payload["messages_sha256"],
                             snap.payload["base_sha256"])
            self.assertNotIn("UNTRUSTED DOSSIER DATA", h.captured[-1])
        finally:
            h.close()


class BuildIntegrationTests(unittest.TestCase):
    def test_native_enabled_build_injects_installs_and_commits(self):
        h = Harness("native")
        try:
            self.assertTrue(h.adapter.step("BUILD"))
            self.assertIn("UNTRUSTED DOSSIER DATA", h.captured[-1])
            snap = h.snapshot("build-r-a")
            self.assertTrue(snap.payload["selection"]["block"])
            self.assertEqual(h.record("build-r-a")["state"], "APPLIED")
            recs = h.ledger_for("build-r-a")
            self.assertEqual(len(recs), 1)
            self.assertTrue(recs[0].consumed)
            self.assertEqual((h.base / "ws" / "app.py").read_text(encoding="utf-8"),
                             "print('ok')\n")
            self.assertEqual(h.state.tasks[0].status, "done")
            self.assertEqual(h.state.tasks[0].delivered_by.model_id,
                             recs[0].served["model_id"])
        finally:
            h.close()

    def test_factory_enabled_build_injects_installs_and_commits(self):
        h = Harness("factory")
        try:
            self.assertTrue(h.adapter.step("BUILD"))
            self.assertIn("UNTRUSTED DOSSIER DATA", h.captured[-1])
            self.assertEqual(h.record("build-r-a")["state"], "APPLIED")
            self.assertTrue(h.ledger_for("build-r-a")[0].consumed)
            self.assertEqual((h.base / "ws" / "app.py").read_text(encoding="utf-8"),
                             "print('ok')\n")
            self.assertEqual(h.state.tasks[0].status, "done")
        finally:
            h.close()

    def test_build_parity_selected_items(self):
        a, b = Harness("native"), Harness("factory")
        try:
            a.adapter.step("BUILD"); b.adapter.step("BUILD")
            sa = a.snapshot("build-r-a").payload["selection"]
            sb = b.snapshot("build-r-a").payload["selection"]
            self.assertTrue(sa["block"] and sb["block"])
            self.assertEqual(sa["items"], sb["items"])
        finally:
            a.close(); b.close()

    def test_disabled_build_preserves_prompt_and_installs_without_ledger(self):
        h = Harness("native", enabled=False)
        try:
            self.assertTrue(h.adapter.step("BUILD"))
            self.assertNotIn("UNTRUSTED DOSSIER DATA", h.captured[-1])
            self.assertFalse(h.core.store.root.exists())
            self.assertEqual(h.engine.call_ledger.all(), [])
            self.assertEqual((h.base / "ws" / "app.py").read_text(encoding="utf-8"),
                             "print('ok')\n")
        finally:
            h.close()

    def test_conflicting_human_edit_is_not_overwritten(self):
        h = Harness("native")
        try:
            target = h.base / "ws" / "app.py"
            target.write_text("human edit\n", encoding="utf-8")
            with self.assertRaises(ContextRecoveryRequired):
                h.adapter.step("BUILD")
            self.assertEqual(target.read_text(encoding="utf-8"), "human edit\n")
            self.assertEqual(h.record("build-r-a")["state"], "PREPARED")
            self.assertFalse(h.ledger_for("build-r-a")[0].consumed)
        finally:
            h.close()

    def test_managed_verification_failure_runs_existing_fix(self):
        for kind in ("native", "factory"):
            h = Harness(kind, verifier=FakeVerifier(mode="fail_first"))
            try:
                self.assertTrue(h.adapter.step("BUILD"), kind)
                self.assertTrue(any("VERIFICATION ERROR" in b
                                    or "ERROR OUTPUT" in b for b in h.captured), kind)
                self.assertEqual(h.record("build-r-a")["state"], "APPLIED", kind)
                self.assertTrue(h.ledger_for("build-r-a")[0].consumed, kind)
                self.assertEqual((h.base / "ws" / "app.py").read_text(
                    encoding="utf-8"), "print('ok')\n", kind)
                delivered = h.state.tasks[0].delivered_by
                self.assertIsNotNone(delivered, kind)
                self.assertEqual(delivered.model_id,
                                 h.pool.last_attempt_model["a"].model_id, kind)
            finally:
                h.close()

    def test_managed_fix_exhaustion_leaves_build_recoverable(self):
        for kind in ("native", "factory"):
            h = Harness(kind, verifier=FakeVerifier(mode="always_fail"))
            try:
                self.assertFalse(h.adapter.step("BUILD"), kind)
                self.assertEqual(h.record("build-r-a")["state"], "PREPARED", kind)
                recs = h.ledger_for("build-r-a")
                self.assertFalse(recs[0].consumed, kind)
                self.assertTrue(any("VERIFICATION ERROR" in b
                                    or "ERROR OUTPUT" in b for b in h.captured), kind)
            finally:
                h.close()

    def test_unknown_payg_never_resubmits(self):
        from calls import CallOutcomeUnknown
        h = Harness("native", mode="payg")
        try:
            h.pool.responder = lambda ref, messages: TimeoutError("timed out")
            with self.assertRaises(CallOutcomeUnknown):
                h.adapter.step("BUILD")
            self.assertEqual(h.ledger_for("build-r-a")[0].state, "UNKNOWN")
            self.assertEqual(len(h.pool.transport), 1)
            with self.assertRaises(CallOutcomeUnknown):
                h.core.recover("build-r-a")
            self.assertEqual(len(h.pool.transport), 1)
        finally:
            h.close()


if __name__ == "__main__":
    unittest.main()


class ConsentCliTests(unittest.TestCase):
    def test_cli_consent_is_explicit_scoped_and_persisted(self):
        import main
        h = Harness("native", enabled=False)
        try:
            self.assertIsNone(h.state.extensions.get("workflow_context", {}).get("activation"))
            args = SimpleNamespace(context_consent=["deepseek-v4-pro"],
                                   context_phases="BUILD",
                                   yes=True, allow_paid=True)
            main._activate_context_from_args(args, h.engine, h.pool, h.state)
            raw = h.state.extensions["workflow_context"]["activation"]
            self.assertEqual(raw["phases"], ["BUILD"])
            self.assertEqual(len(raw["providers"]), 1)
            self.assertEqual(raw["actor"], "human:cli")
            # Architecture remains outside the consented phase set.
            snap = h.core.prepare("architecture-r", "ARCHITECTURE", h._route(), h._messages())
            self.assertIsNone(snap)
        finally:
            h.close()

    def test_no_cli_flag_means_disabled(self):
        import main
        h = Harness("native", enabled=False)
        try:
            main._activate_context_from_args(
                SimpleNamespace(context_consent=None, context_phases="BUILD",
                                yes=True, allow_paid=True),
                h.engine, h.pool, h.state)
            self.assertIsNone(h.state.extensions.get("workflow_context", {}).get("activation"))
        finally:
            h.close()


class ServiceResumeTests(unittest.TestCase):
    def test_build_resume_after_confirmed_before_application(self):
        h = Harness("native")
        try:
            original = h.core.commit
            def crash(logical_id, apply):
                raise ContextRecoveryRequired("simulated crash before application")
            h.core.commit = crash
            with self.assertRaises(ContextRecoveryRequired):
                h.adapter.step("BUILD")
            self.assertEqual(len(h.pool.transport), 1)
            record = h.record("build-r-a")
            self.assertEqual(record["state"], "PREPARED")
            self.assertEqual(h.ledger_for("build-r-a")[0].state, "CONFIRMED")
            self.assertFalse(h.ledger_for("build-r-a")[0].consumed)

            # Fresh process-equivalent: reload canonical state and rebuild adapter.
            state = h.store.load_project(h.store.project_state_path(h.base / "project"))
            adapter = _make_adapter("native", h.cfg, h.pool, state)
            eng = engine.WorkflowEngine(state, h.store, adapter)
            eng.apply_pool(h.pool)
            self.assertTrue(adapter.step("BUILD"))
            self.assertEqual(len(h.pool.transport), 1)  # recovered locally
            self.assertEqual((h.base / "ws" / "app.py").read_text(encoding="utf-8"),
                             "print('ok')\n")
            self.assertEqual(state.extensions["workflow_context"]["records"]
                             ["build-r-a"]["state"], "APPLIED")
            self.assertTrue(eng.call_ledger.all()[0].consumed)
        finally:
            h.close()


class InstallInterruptionTests(unittest.TestCase):
    def test_interrupted_atomic_install_leaves_original_and_unconsumed(self):
        import os
        from unittest.mock import patch
        h = Harness("native")
        try:
            target = h.base / "ws" / "app.py"
            original = target.read_text(encoding="utf-8")
            real_replace = os.replace
            def fail(src, dst):
                if str(dst).endswith("app.py"):
                    raise OSError("simulated interruption during install")
                return real_replace(src, dst)
            with patch("os.replace", side_effect=fail):
                with self.assertRaises(OSError):
                    h.adapter.step("BUILD")
            self.assertEqual(target.read_text(encoding="utf-8"), original)
            self.assertEqual(h.record("build-r-a")["state"], "PREPARED")
            self.assertFalse(h.ledger_for("build-r-a")[0].consumed)
        finally:
            h.close()


class Crash(BaseException):
    pass


def _fix_verifier():
    class V(FakeVerifier):
        def run_critic(self, producer, task, code, run_id=None, review=False):
            bad = "BAD" in code
            verdict = "FAIL" if bad else "PASS"
            return {"verdict": verdict, "critic": {"ok": not bad},
                    "producer_family": "fake", "critic_family": "critic",
                    "producer_known": True,
                    "objections": ([{"claim": "bad", "evidence": "x", "fix": "y"}]
                                   if bad else [])}
    return V()


def _fix_responder(captured, *, fix_ambiguous=False):
    def responder(ref, messages):
        blob = "\n".join(str(getattr(m, "content", m)) for m in messages)
        captured.append(blob)
        if ("VERIFICATION ERROR" in blob or "ERROR OUTPUT" in blob):
            if fix_ambiguous:
                return TimeoutError("timed out")
            return "```python\nGOOD = 1\n```"
        return "```python\nBAD = 1\n```"
    return responder


class FixRecoveryTests(unittest.TestCase):
    def test_resume_after_recorded_verification_failure_does_not_resubmit_build(self):
        h = Harness("native", verifier=_fix_verifier())
        try:
            h.pool.responder = _fix_responder(h.captured)
            h.adapter.executor._fix = lambda *a, **k: (_ for _ in ()).throw(Crash())
            with self.assertRaises(Crash):
                h.adapter.step("BUILD")
            self.assertEqual(len(h.pool.transport), 1)  # BUILD only
            self.assertEqual(h.record("build-r-a")["state"], "PREPARED")
            self.assertEqual(h.ledger_for("build-r-a")[0].state, "CONFIRMED")
            self.assertFalse(h.ledger_for("build-r-a")[0].consumed)

            state = h.store.load_project(h.store.project_state_path(h.base / "project"))
            adapter = _make_adapter("native", h.cfg, h.pool, state,
                                    verifier=_fix_verifier())
            eng = engine.WorkflowEngine(state, h.store, adapter)
            eng.apply_pool(h.pool)
            self.assertTrue(adapter.step("BUILD"))
            self.assertEqual(len(h.pool.transport), 2)  # BUILD never repeated
            builds = sum("Deliverable file" in b for b in h.captured)
            fixes = sum("VERIFICATION ERROR" in b or "ERROR OUTPUT" in b
                        for b in h.captured)
            self.assertEqual(builds, 1)
            self.assertGreaterEqual(fixes, 1)
            self.assertEqual((h.base / "ws" / "app.py").read_text(
                encoding="utf-8"), "GOOD = 1\n")
            self.assertEqual(state.extensions["workflow_context"]["records"]
                             ["build-r-a"]["state"], "APPLIED")
            self.assertTrue(eng.call_ledger.all()[0].consumed)
        finally:
            h.close()

    def test_ambiguous_fix_is_fail_closed(self):
        from calls import CallOutcomeUnknown
        h = Harness("native", verifier=_fix_verifier())
        try:
            # Keep a free BUILD producer and a distinct PAYG FIX candidate.
            h.pool.registry.models = {
                k: v for k, v in h.pool.registry.models.items()
                if v.model_id in ("gemini-3.1-pro-preview", "deepseek-v4-pro")}
            h.pool.responder = _fix_responder(h.captured, fix_ambiguous=True)
            with self.assertRaises(CallOutcomeUnknown):
                h.adapter.step("BUILD")
            states = [r.state for r in h.engine.call_ledger.all()]
            self.assertIn("UNKNOWN", states)
            self.assertEqual(h.record("build-r-a")["state"], "PREPARED")
            self.assertFalse(h.ledger_for("build-r-a")[0].consumed)
            self.assertEqual(len(h.pool.transport), 2)
        finally:
            h.close()

    def test_fix_producer_identity_is_preserved_native_and_factory(self):
        for kind in ("native", "factory"):
            h = Harness(kind, verifier=_fix_verifier())
            try:
                h.pool.responder = _fix_responder(h.captured)
                self.assertTrue(h.adapter.step("BUILD"), kind)
                self.assertEqual(h.state.tasks[0].delivered_by.model_id,
                                 h.pool.last_attempt_model["a"].model_id, kind)
                self.assertEqual((h.base / "ws" / "app.py").read_text(
                    encoding="utf-8"), "GOOD = 1\n", kind)
            finally:
                h.close()
