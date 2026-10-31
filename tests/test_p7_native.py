#!/usr/bin/env python3
"""P7.0/P7.1: baseline and first native service extraction."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import architecture
import engine
import requirements
import state as s


class NativeRequirementsTests(unittest.TestCase):
    def test_prepare_updates_canonical_state(self):
        st = s.ProjectState(run_id="r", goal="demo", engine_kind=s.ENGINE_UNIFIED)
        svc = requirements.RequirementsService(None, None, lambda *a: None)
        payload = {"objective": "demo", "scope_in": ["x"],
                   "acceptance": ["runs"], "open_questions": ["which?"]}
        svc._ask = lambda *a, **k: "```json\n" + json.dumps(payload) + "\n```"
        req = svc.prepare(st)
        self.assertEqual(req["objective"], "demo")
        self.assertEqual(st.requirements.objective, "demo")
        self.assertEqual(st.requirements.acceptance_texts(), ["runs"])
        self.assertEqual(st.requirements.open_questions, ["which?"])


class NativeArchitectureTests(unittest.TestCase):
    def test_design_updates_architecture_and_tasks(self):
        st = s.ProjectState(run_id="r", goal="demo", engine_kind=s.ENGINE_UNIFIED)
        st.requirements = s.RequirementContract(
            objective="demo", acceptance=[s.AcceptanceCriterion(id="ac1", text="runs")])
        svc = architecture.ArchitectureService(None, None, lambda *a: None)
        payload = {"approach": "single file",
                   "modules": [{"path": "app.py"}],
                   "interfaces": [{"name": "main", "file": "app.py"}],
                   "tasks": [{"id": "t1", "title": "run", "description": "",
                              "target_file": "app.py", "acceptance": ["runs"],
                              "requirement_ids": ["ac1"], "complexity": "low",
                              "role": "junior"}],
                   "test_plan": []}
        svc._ask = lambda *a, **k: json.dumps(payload)
        with tempfile.TemporaryDirectory() as td:
            svc.design(st, td)
        self.assertEqual(st.architecture.approach, "single file")
        self.assertEqual(st.tasks[0].target_file, "app.py")
        self.assertEqual(st.tasks[0].requirement_ids, ["ac1"])
        self.assertEqual(st.requirements.acceptance[0].task_ids, ["t1"])


class BaselineTests(unittest.TestCase):
    def test_native_modules_do_not_import_factory(self):
        source = Path(requirements.__file__).read_text() + \
            Path(architecture.__file__).read_text()
        self.assertNotIn("import factory", source)
        self.assertNotIn("from factory", source)

    def test_engine_keeps_both_phase_orders_for_compatibility(self):
        self.assertIn("SCAFFOLD", engine.PHASE_ORDER_LEGACY)
        self.assertIn("ARCHITECTURE", engine.PHASE_ORDER_UNIFIED)
        self.assertLess(engine.PHASE_ORDER_UNIFIED.index("ARCHITECTURE"),
                        engine.PHASE_ORDER_UNIFIED.index("SCAFFOLD"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
