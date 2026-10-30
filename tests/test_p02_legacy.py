#!/usr/bin/env python3
"""P2: legacy formats are compatibility views of canonical ProjectState."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine as e
import state as s
from store import StateManager, atomic_write_json

FIX = Path(__file__).resolve().parent / "fixtures" / "p01"


class LegacyConversionTests(unittest.TestCase):
    def test_factory_state_migrates_to_v3_for_every_phase(self):
        raw = json.loads((FIX / "factory_state_architect.json").read_text())
        for step in ("REQUIREMENTS", "SCAFFOLD", "ARCHITECT", "BUILD",
                     "INTEGRATE", "VISUAL", "SIGN_OFF", "DONE"):
            with self.subTest(step=step):
                data = dict(raw)
                data["step"] = step
                st = s.from_legacy_factory(data)
                self.assertEqual(st.schema_version, 3)
                self.assertEqual(st.engine_kind, s.ENGINE_LEGACY)
                self.assertEqual(st.requirements.acceptance_texts(), ["runs"])
                self.assertEqual(st.tasks[0].requirement_ids, ["ac1"])

    def test_unknown_factory_fields_are_preserved(self):
        raw = json.loads((FIX / "factory_state_architect.json").read_text())
        raw["custom_top"] = {"keep": 1}
        raw["tasks"][0]["custom_task"] = {"keep": 2}
        st = s.from_legacy_factory(raw)
        self.assertEqual(st.extensions["legacy_factory"]["custom_top"], {"keep": 1})
        self.assertEqual(st.tasks[0].legacy_extra["custom_task"], {"keep": 2})

    def test_session_migrates_to_v3(self):
        session = json.loads((FIX / "session.json").read_text())
        spec = json.loads((FIX / "spec.json").read_text())
        st = s.from_legacy_session(session, spec)
        self.assertEqual(st.schema_version, 3)
        self.assertEqual(st.tasks[0].requirement_ids, ["ac1"])

    def test_factory_compat_round_trip(self):
        st = s.from_legacy_factory(
            json.loads((FIX / "factory_state_architect.json").read_text()))
        compat = s.factory_compat_dict(st)
        self.assertEqual(compat["schema_version"], 1)
        self.assertEqual(compat["engine_kind"], "legacy")
        again = s.from_legacy_factory(compat)
        self.assertEqual(again.goal, st.goal)
        self.assertEqual(again.requirements.acceptance_texts(), ["runs"])
        self.assertEqual(again.tasks[0].target_file, "app.py")


class CanonicalPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.project = self.base / "p"
        self.project.mkdir()
        self.store = StateManager(self.base / "workspace", self.base / "logs")

    def tearDown(self):
        self.tmp.cleanup()

    def _state(self):
        st = s.ProjectState(run_id="r1", project_root=str(self.project),
                            goal="g", engine_kind=s.ENGINE_LEGACY,
                            engine_version="legacy", phase="BUILD")
        st.requirements = s.RequirementContract(
            objective="g", acceptance=[s.AcceptanceCriterion(id="ac1", text="runs")])
        st.tasks = [s.Task(id="t1", kind="build", target_file="a.py",
                           acceptance=["runs"])]
        return st

    def test_factory_persistence_is_a_compatibility_view(self):
        st = self._state()
        eng = e.WorkflowEngine(st, self.store, adapter=None, persistence="factory")
        eng.save()
        factory = self.store.legacy_factory_path(self.project)
        crew = self.store.project_state_path(self.project)
        self.assertTrue(factory.is_file())
        self.assertFalse(crew.exists())
        raw = json.loads(factory.read_text())
        self.assertEqual(raw["schema_version"], 1)
        self.assertEqual(raw["engine_kind"], "legacy")
        self.assertEqual(raw["requirements"]["acceptance"], ["runs"])
        # canonical state remains v3 in memory
        self.assertEqual(st.schema_version, 3)

    def test_crew_persistence_does_not_write_factory(self):
        st = self._state()
        st.engine_kind = s.ENGINE_UNIFIED
        eng = e.WorkflowEngine(st, self.store, adapter=None, persistence="crew")
        eng.save()
        self.assertTrue(self.store.project_state_path(self.project).is_file())
        self.assertFalse(self.store.legacy_factory_path(self.project).exists())

    def test_legacy_factory_can_be_resumed_into_canonical(self):
        st = self._state()
        e.WorkflowEngine(st, self.store, adapter=None, persistence="factory").save()
        target = self.store.resolve_resume(self.project)
        self.assertIsNone(target.project_state)
        canonical = s.from_legacy_factory(target.legacy_raw)
        self.assertEqual(canonical.schema_version, 3)
        self.assertEqual(canonical.tasks[0].target_file, "a.py")


if __name__ == "__main__":
    unittest.main(verbosity=2)
