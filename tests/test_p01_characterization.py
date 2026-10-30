#!/usr/bin/env python3
"""P0: historical contracts and formats that must survive migration.

Only format/contract invariants live here. Known defects are asserted in the
regression suite against the target semantics, never frozen as expected.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import state as s

FIX = Path(__file__).resolve().parent / "fixtures" / "p01"


class HistoricalFormatTests(unittest.TestCase):
    def test_factory_state_without_engine_metadata_is_accepted(self):
        raw = json.loads((FIX / "factory_state_architect.json").read_text())
        self.assertNotIn("engine_kind", raw)
        st = s.from_legacy_factory(raw)
        self.assertEqual(st.engine_kind, s.ENGINE_LEGACY)
        self.assertEqual(st.schema_version, s.SCHEMA_VERSION)

    def test_old_requirement_fields_survive(self):
        raw = json.loads((FIX / "factory_state_architect.json").read_text())
        st = s.from_legacy_factory(raw)
        self.assertEqual(st.requirements.scope_in, ["one file"])
        self.assertEqual(st.requirements.scope_out, ["database"])
        self.assertEqual(st.requirements.constraints, ["stdlib"])
        self.assertEqual(st.requirements.acceptance_texts(), ["runs"])

    def test_old_architecture_interfaces_survive(self):
        raw = json.loads((FIX / "factory_state_architect.json").read_text())
        st = s.from_legacy_factory(raw)
        self.assertEqual(st.architecture.interfaces[0]["name"], "main")
        self.assertEqual(st.architecture.modules[0]["path"], "app.py")

    def test_old_task_fields_survive(self):
        raw = json.loads((FIX / "factory_state_architect.json").read_text())
        st = s.from_legacy_factory(raw)
        task = st.tasks[0]
        self.assertEqual(task.id, "t1")
        self.assertEqual(task.role, "junior")
        self.assertEqual(task.complexity, "low")
        self.assertEqual(task.status, "done")

    def test_old_step_names_map_to_phases(self):
        # migrated legacy state continues in the legacy order
        expected = {
            "REQUIREMENTS": "SCAFFOLD",
            "SCAFFOLD": "ARCHITECTURE",
            "ARCHITECT": "BUILD",
            "BUILD": "BUILD",
            "INTEGRATE": "INTEGRATE",
            "VISUAL": "VISUAL",
            "SIGN_OFF": "SIGNOFF",
            "DONE": "DONE",
        }
        for step, phase in expected.items():
            raw = json.loads((FIX / "factory_state_architect.json").read_text())
            raw["step"] = step
            self.assertEqual(s.from_legacy_factory(raw).phase, phase, step)

    def test_old_session_with_spec(self):
        session = json.loads((FIX / "session.json").read_text())
        spec = json.loads((FIX / "spec.json").read_text())
        st = s.from_legacy_session(session, spec)
        self.assertEqual(st.goal, "demo app")
        self.assertEqual(st.project_root, "/tmp/crew-demo")
        self.assertEqual(st.tasks[0].target_file, "app.py")

    def test_project_memory_is_not_a_resume_source(self):
        # Historical fact: project_memory.json is write-only. The canonical
        # state is .factory/state.json / .crew/state.json.
        raw = json.loads((FIX / "factory_state_architect.json").read_text())
        st = s.from_legacy_factory(raw)
        self.assertNotIn("handover", st.to_dict())
        self.assertNotIn("project_memory", st.to_dict())


if __name__ == "__main__":
    unittest.main(verbosity=2)
