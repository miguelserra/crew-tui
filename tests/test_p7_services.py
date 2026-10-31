#!/usr/bin/env python3
"""P7.1c: native integration/visual/signoff services."""

import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import integration
import signoff
import visual
import state as s


class FakePool:
    def __init__(self):
        self.last_attempt_model = {}
        self.router = types.SimpleNamespace(route=lambda req: types.SimpleNamespace(
            candidates=[], chain=None))

    def complete(self, *a, **k):
        raise AssertionError("unexpected model call")


class IntegrationTests(unittest.TestCase):
    def test_runs_python_entrypoint(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("print('ok')\n", encoding="utf-8")
            st = s.ProjectState(run_id="r", goal="x", engine_kind=s.ENGINE_UNIFIED)
            settings = types.SimpleNamespace(max_verify_loops=1)
            svc = integration.IntegrationService(settings, FakePool(),
                                                 lambda *a: None, "r")
            self.assertTrue(svc.integrate(st, root))
            self.assertEqual(st.integration["verdict"], "PASS")

    def test_no_entrypoint_is_skip(self):
        with tempfile.TemporaryDirectory() as td:
            st = s.ProjectState(run_id="r", goal="x", engine_kind=s.ENGINE_UNIFIED)
            settings = types.SimpleNamespace(max_verify_loops=1)
            svc = integration.IntegrationService(settings, FakePool(),
                                                 lambda *a: None, "r")
            self.assertTrue(svc.integrate(st, td))
            self.assertEqual(st.integration["verdict"], "SKIP")


class VisualTests(unittest.TestCase):
    def test_no_html_is_na(self):
        with tempfile.TemporaryDirectory() as td:
            st = s.ProjectState(run_id="r", goal="x", engine_kind=s.ENGINE_UNIFIED)
            settings = types.SimpleNamespace(visual_check=True,
                                             allow_unverified=False)
            svc = visual.VisualService(settings, FakePool(), lambda *a: None, "r")
            self.assertTrue(svc.step_visual(st, td))
            self.assertEqual(st.visual["verdict"], visual.verify_policy.NA)

    def test_html_without_visual_model_is_unverified(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "index.html").write_text("<html></html>", encoding="utf-8")
            st = s.ProjectState(run_id="r", goal="x", engine_kind=s.ENGINE_UNIFIED)
            settings = types.SimpleNamespace(visual_check=True,
                                             allow_unverified=False)
            orig = visual.playwright_audit
            visual.playwright_audit = lambda root, logs, **k: {
                "verdict": "PASS", "screenshot": str(root / "shot.png")}
            (root / "shot.png").write_bytes(b"png")
            try:
                svc = visual.VisualService(settings, FakePool(), lambda *a: None, "r")
                self.assertFalse(svc.step_visual(st, root))
            finally:
                visual.playwright_audit = orig


class SignoffTests(unittest.TestCase):
    def test_writes_project_memory_without_model(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            st = s.ProjectState(run_id="r", goal="x", engine_kind=s.ENGINE_UNIFIED)
            st.requirements = s.RequirementContract(objective="x")
            settings = types.SimpleNamespace()
            svc = signoff.SignoffService(settings, FakePool(), lambda *a: None, "r")
            self.assertTrue(svc.signoff(st, root, final_root=root))
            self.assertTrue((root / "project_memory.json").is_file())
            self.assertEqual(st.signoff["goal"], "x")


if __name__ == "__main__":
    unittest.main(verbosity=2)
