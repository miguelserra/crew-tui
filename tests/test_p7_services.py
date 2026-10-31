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
    def __init__(self, reply=""):
        self.reply = reply
        self.last_attempt_model = {}
        self.router = types.SimpleNamespace(route=lambda req: types.SimpleNamespace(
            candidates=[object()] if reply else [], chain=None))

    def complete(self, *a, **k):
        if not self.reply:
            raise AssertionError("unexpected model call")
        from registry import ModelRef
        self.last_attempt_model["__integration_fix__"] = ModelRef(
            provider="fake", endpoint="local", model_id="fake-1",
            family="fake", billing="local")
        return self.reply


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_native_services_do_not_import_factory(self):
        base = Path(__file__).resolve().parents[1]
        for name in ("integration.py", "visual.py", "signoff.py",
                     "native_util.py", "native_adapter.py"):
            src = (base / name).read_text(encoding="utf-8")
            self.assertNotIn("import factory", src, name)
            self.assertNotIn("from factory", src, name)


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

    def test_fixer_attributes_shared_call_to_tasks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
            (root / "util.py").write_text("x = 1\n", encoding="utf-8")
            st = s.ProjectState(run_id="r", goal="x", engine_kind=s.ENGINE_UNIFIED)
            st.tasks = [s.Task("a", target_file="app.py"),
                        s.Task("b", target_file="util.py")]
            st.integration = {}
            reply = ("### FILE: app.py\n```python\nprint('ok')\n```\n"
                     "### FILE: util.py\n```python\nx = 2\n```\n")
            pool = FakePool(reply)
            settings = types.SimpleNamespace(max_verify_loops=1)
            svc = integration.IntegrationService(settings, pool, lambda *a: None, "r")
            svc._msg = lambda *a: object()
            orig_blocks = integration.native_util.multi_file_blocks
            integration.native_util.multi_file_blocks = lambda raw: [
                ("app.py", "print('ok')"), ("util.py", "x = 2")]
            self.addCleanup(setattr, integration.native_util,
                            "multi_file_blocks", orig_blocks)
            calls = {"n": 0}

            def run_entry(kind, path, root):
                calls["n"] += 1
                if calls["n"] == 1:
                    return {"verdict": "FAIL", "kind": kind,
                            "output": "app.py util.py boom"}
                return {"verdict": "PASS", "kind": kind, "output": "ok"}

            svc._run_entry = run_entry
            self.assertTrue(svc.integrate(st, root))
            recs = st.integration.get("fix_records", [])
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["task_ids"], ["a", "b"])
            self.assertEqual(recs[0]["files"], ["app.py", "util.py"])
            for task in st.tasks:
                self.assertEqual(task.status, "done")
                self.assertEqual(task.delivered_by.model_id, "fake-1")
                self.assertEqual(len(task.attempts), 1)
                self.assertEqual(task.attempts[0].attempt_id, recs[0]["call_id"])
            self.assertFalse((root / ".factory").exists())

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

    def test_visual_fix_is_reviewed_again(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "index.html").write_text("<html></html>", encoding="utf-8")
            st = s.ProjectState(run_id="r", goal="x", engine_kind=s.ENGINE_UNIFIED)
            settings = types.SimpleNamespace(visual_check=True, allow_unverified=False)
            svc = visual.VisualService(settings, FakePool(), lambda *a: None, "r")
            audits = [{"verdict": "FAIL", "screenshot": str(root / "first.png")},
                      {"verdict": "PASS", "screenshot": str(root / "second.png")}]
            reviews = [{"verdict": "FAIL", "objections": [{"claim": "bad"}]},
                       {"verdict": "PASS"}]
            orig_audit, orig_review, orig_fix = (visual.playwright_audit,
                                                  svc._review, svc._fix)
            visual.playwright_audit = lambda r, d, **k: audits.pop(0)
            svc._review = lambda st, png: reviews.pop(0)
            svc._fix = lambda st, r, v: None
            try:
                self.assertTrue(svc.step_visual(st, root))
            finally:
                visual.playwright_audit = orig_audit
                svc._review, svc._fix = orig_review, orig_fix
            self.assertEqual(st.visual["vision_retry"]["verdict"], "FAIL")
            self.assertEqual(st.visual["vision"]["verdict"], "PASS")
            self.assertFalse((root / ".factory").exists())

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
