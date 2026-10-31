#!/usr/bin/env python3
"""P7.1a: bounded context and atomic architecture validation."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import architecture
import context_reader
import state as s


class ContextTests(unittest.TestCase):
    def test_skips_binary_excluded_and_respects_limits(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.py").write_text("print(1)\n", encoding="utf-8")
            (root / "b.bin").write_bytes(b"\x00\x01\x02\x00" * 100)
            (root / ".env").write_text("SECRET=1", encoding="utf-8")
            (root / "big.py").write_text("x" * 10000, encoding="utf-8")
            ctx = context_reader.read_project_context(
                root, max_files=10, max_file_chars=100, max_total_chars=300)
            names = [n for n, _ in ctx.files]
            self.assertIn("a.py", names)
            self.assertNotIn("b.bin", names)
            self.assertNotIn(".env", names)
            self.assertTrue(any(r == "binary" for _n, r in ctx.skipped))
            self.assertTrue(ctx.truncated)
            self.assertLessEqual(sum(len(t) for _n, t in ctx.files), 300)


class AtomicArchitectureTests(unittest.TestCase):
    def _project(self):
        st = s.ProjectState(run_id="r", goal="demo", engine_kind=s.ENGINE_UNIFIED)
        st.requirements = s.RequirementContract(
            objective="demo", acceptance=[s.AcceptanceCriterion(id="ac1", text="runs")])
        return st

    def _svc(self, payload, capture=None):
        svc = architecture.ArchitectureService(None, None, lambda *a: None)
        def ask(project, system, user, tag):
            if capture is not None:
                capture.append(user)
            return "```json\n" + json.dumps(payload) + "\n```"
        svc._ask = ask
        return svc

    def test_duplicate_or_empty_targets_are_rejected_without_mutation(self):
        payload = {"approach": "x", "modules": [], "interfaces": [],
                   "tasks": [
                       {"id": "t1", "target_file": "a.py", "acceptance": ["runs"]},
                       {"id": "t2", "target_file": "a.py", "acceptance": ["runs"]},
                       {"id": "t3", "target_file": "", "acceptance": ["runs"]},
                   ], "test_plan": []}
        st = self._project()
        before = (st.requirements.objective, st.architecture.approach, list(st.tasks))
        with self.assertRaises(ValueError):
            self._svc(payload).design(st, ".")
        self.assertEqual((st.requirements.objective, st.architecture.approach, st.tasks), before)

    def test_prompt_marks_project_content_untrusted(self):
        payload = {"approach": "x", "modules": [], "interfaces": [],
                   "tasks": [], "test_plan": []}
        captured = []
        with self.assertRaises(ValueError):
            self._svc(payload, captured).design(self._project(), ".")
        self.assertTrue(any("UNTRUSTED" in p for p in captured))


if __name__ == "__main__":
    unittest.main(verbosity=2)
