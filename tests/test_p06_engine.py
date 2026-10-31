#!/usr/bin/env python3
"""P6: profile phases, resume persistence and publish hook."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine as e
import state as s
from store import StateManager


class FakeAdapter:
    order = e.PHASE_ORDER_UNIFIED

    def __init__(self, project):
        self.project = project
        self.calls = []
        self.published = 0

    def prepare(self, goal, feedback=""):
        self.project.requirements = s.RequirementContract(objective=goal)
        return {"objective": goal, "open_questions": []}

    def step(self, phase):
        self.calls.append(phase)
        if phase == "ARCHITECTURE":
            self.project.architecture = s.ArchitectureContract(
                approach="script", modules=[{"path": "app.py"}],
                interfaces=[{"name": "main", "file": "app.py"}])
            self.project.tasks = [s.Task(id="t1", kind="build", target_file="app.py")]
            self.project.tasks = s.validate_tasks(self.project.tasks)
        if phase == "BUILD":
            self.project.tasks[0].status = "done"
            self.project.verifications["t1"] = s.VerificationRecord(verdict="PASS")
        return True

    def sync_to_project(self):
        return None

    def next_phase(self, phase):
        idx = self.order.index(phase)
        return self.order[idx + 1]

    def on_last_ok(self, task_id, ref):
        return None

    def publish(self):
        self.published += 1
        return {"status": "done", "entries": 1}


class ProfileWorkflowTests(unittest.TestCase):
    def _engine(self, profile="auto"):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        state = s.ProjectState(run_id="r", project_root=str(root), goal="g",
                               engine_kind=s.ENGINE_UNIFIED,
                               engine_version=e.ENGINE_VERSION)
        state.extensions["profile_selection"] = {
            "requested": profile, "resolved": "" if profile == "auto" else profile,
            "reason": "", "profile_version": 1}
        state.profile = "auto" if profile == "auto" else profile
        adapter = FakeAdapter(state)
        eng = e.WorkflowEngine(state, StateManager(root / "ws", root / "logs"), adapter)
        return self.tmp, state, adapter, eng

    def test_auto_resolves_once_and_skips_visual_for_script(self):
        tmp, state, adapter, eng = self._engine("auto")
        try:
            eng.step(); eng.approve_spec(); eng.step(); eng.approve_architecture()
            eng.run()
            self.assertEqual(state.profile, "script")
            self.assertNotIn("VISUAL", adapter.calls)
            self.assertEqual(adapter.published, 1)
        finally:
            tmp.cleanup()

    def test_resume_does_not_recompute_resolved_profile(self):
        tmp, state, adapter, eng = self._engine("auto")
        try:
            eng.step(); eng.approve_spec(); eng.step()
            resolved = state.profile
            self.assertEqual(resolved, "script")
            # force a different auto hint; persisted selection must win
            state.goal = "build a browser UI with visual tests"
            eng.resolve_profile()
            self.assertEqual(state.profile, resolved)
            self.assertEqual(state.extensions["profile_selection"]["resolved"], resolved)
        finally:
            tmp.cleanup()

    def test_web_profile_keeps_visual(self):
        tmp, state, adapter, eng = self._engine("web")
        try:
            eng.step(); eng.approve_spec(); eng.step(); eng.approve_architecture(); eng.run()
            self.assertIn("VISUAL", adapter.calls)
        finally:
            tmp.cleanup()

    def test_profile_change_invalidates_approval(self):
        st = s.ProjectState(run_id="r", profile="web", engine_kind=s.ENGINE_UNIFIED)
        st.architecture = s.ArchitectureContract(approach="a", modules=[{"path": "a.py"}])
        st.approve_architecture()
        self.assertTrue(st.architecture_approved())
        st.profile = "script"
        self.assertFalse(st.architecture_approved())


if __name__ == "__main__":
    unittest.main(verbosity=2)
