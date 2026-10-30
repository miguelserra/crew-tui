#!/usr/bin/env python3
"""P0/P1: engine selection, resume immutability, approvals and dispatch guard."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine as e
import state as s
from registry import ModelRef, unknown_ref
from store import StateManager, atomic_write_json

FIX = Path(__file__).resolve().parent / "fixtures" / "p01"


class FakeAdapter:
    order = e.PHASE_ORDER_UNIFIED

    def __init__(self, architecture=None):
        self.engine = None
        self.architecture = architecture or s.ArchitectureContract(
            approach="a", modules=[{"path": "a.py"}],
            interfaces=[{"name": "f", "file": "a.py"}])
        self.calls = []

    def prepare(self, goal, feedback=""):
        return {"objective": goal, "open_questions": []}

    def next_phase(self, phase):
        idx = self.order.index(phase)
        return self.order[idx + 1]

    def step(self, phase):
        self.calls.append(phase)
        if phase == "ARCHITECTURE":
            self.engine.state.architecture = self.architecture
        return True

    def sync_to_project(self):
        return None

    def on_last_ok(self, task_id, ref):
        return None


class EngineSelectTests(unittest.TestCase):
    def test_resolve_engine(self):
        self.assertEqual(e.resolve_engine("legacy"), s.ENGINE_LEGACY)
        self.assertEqual(e.resolve_engine("unified"), s.ENGINE_UNIFIED)
        self.assertEqual(e.resolve_engine(""), s.ENGINE_LEGACY)
        with self.assertRaises(e.EngineError):
            e.resolve_engine("auto")

    def test_resume_uses_persisted_kind_not_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "p"
            project.mkdir()
            store = StateManager(base / "workspace", base / "logs")
            st = s.ProjectState(run_id="r1", project_root=str(project),
                                engine_kind=s.ENGINE_LEGACY, engine_version="legacy")
            store.save_project(st)
            old = os.environ.get("CREW_ENGINE")
            os.environ["CREW_ENGINE"] = "unified"
            try:
                decision = e.open_execution(store, str(project))
            finally:
                if old is None:
                    os.environ.pop("CREW_ENGINE", None)
                else:
                    os.environ["CREW_ENGINE"] = old
            self.assertEqual(decision.engine_kind, s.ENGINE_LEGACY)
            self.assertEqual(decision.state.engine_kind, s.ENGINE_LEGACY)

    def test_no_engine_migration_without_explicit_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "p"
            project.mkdir()
            store = StateManager(base / "workspace", base / "logs")
            factory = store.legacy_factory_path(project)
            factory.parent.mkdir(parents=True)
            atomic_write_json(factory, json.loads(
                (FIX / "factory_state_architect.json").read_text()))
            st = e.migrate_state(store, str(project))
            self.assertEqual(st.engine_kind, s.ENGINE_LEGACY)
            st2 = e.migrate_engine(store, st)
            self.assertEqual(st2.engine_kind, s.ENGINE_UNIFIED)
            self.assertTrue(any(m.get("engine_changed") for m in st2.migrations))


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.project = base / "p"
        self.project.mkdir()
        self.store = StateManager(base / "workspace", base / "logs")
        self.state = s.ProjectState(run_id="r1", project_root=str(self.project),
                                    goal="g", engine_kind=s.ENGINE_UNIFIED,
                                    engine_version=e.ENGINE_VERSION)
        self.adapter = FakeAdapter()
        self.engine = e.WorkflowEngine(self.state, self.store, self.adapter)
        self.adapter.engine = self.engine

    def tearDown(self):
        self.tmp.cleanup()

    def test_spec_gate_then_architecture_gate(self):
        self.assertEqual(self.engine.step(), "paused")  # spec approval
        self.assertEqual(self.state.pause.reason, "spec_approval")
        self.engine.approve_spec()
        self.engine.resume()
        self.assertEqual(self.state.phase, "ARCHITECTURE")
        self.assertEqual(self.engine.step(), "paused")  # architecture approval
        self.assertEqual(self.state.pause.reason, "architecture_approval")
        self.assertNotEqual(self.state.phase, "SCAFFOLD")
        self.engine.approve_architecture()
        self.assertEqual(self.state.phase, "SCAFFOLD")

    def test_yes_does_not_bypass_architecture(self):
        # --yes is modelled by approve_spec(); it must not touch architecture.
        self.engine.step()
        self.engine.approve_spec()
        self.engine.resume()
        self.engine.step()
        self.assertEqual(self.state.pause.reason, "architecture_approval")
        self.assertFalse(self.state.architecture_approved())

    def test_changed_architecture_recloses_gate(self):
        self.engine.step()
        self.engine.approve_spec()
        self.engine.resume()
        self.engine.step()
        self.engine.approve_architecture()
        self.state.phase = "SCAFFOLD"
        self.state.architecture.modules.append({"path": "b.py"})
        self.assertEqual(self.engine.step(), "paused")
        self.assertEqual(self.state.pause.reason, "architecture_approval")

    def test_unknown_billing_blocked_until_approved(self):
        ref = unknown_ref()
        with self.assertRaises(e.UnknownBilling):
            self.engine.check_refs([ref])
        self.engine.allow_unknown = True
        self.engine.check_refs([ref])  # explicit global opt-in
        self.engine.allow_unknown = False
        self.engine.approve_unknown_billing(ref)
        self.engine.check_refs([ref])

    def test_known_billing_is_not_blocked(self):
        self.engine.check_refs([ModelRef("google", "gemini", "gemini-x",
                                         billing="subscription")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
