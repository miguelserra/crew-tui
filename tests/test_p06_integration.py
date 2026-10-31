#!/usr/bin/env python3
"""P6 integration: FactoryAdapter isolated workspace and publish."""

import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import main
    import engine
    import state as s
    import publish as pub
except Exception as exc:  # pragma: no cover
    DEPS_OK = False
    DEPS_ERR = str(exc)
else:
    DEPS_OK = True
    DEPS_ERR = ""


@unittest.skipUnless(DEPS_OK, f"runtime deps missing: {DEPS_ERR}")
class IsolatedFactoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.project = base / "project"
        self.project.mkdir()
        (self.project / "app.py").write_text("old\n", encoding="utf-8")
        self.settings = main.Settings()
        self.settings.keep_awake = False
        self.settings.sleep_offpeak = False
        self.settings.workspace_dir = str(base / "workspace")
        self.settings.log_dir = str(base / "logs")
        self.pool = types.SimpleNamespace(
            is_peak=lambda: False, local_up=False, attempt_sink=None,
            telemetry=None, dispatch_guard=None,
            complete=lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("scaffold must not call the LLM")))
        self.state = s.ProjectState(
            run_id="r1", project_root=str(self.project), goal="demo",
            engine_kind=s.ENGINE_UNIFIED, engine_version=engine.ENGINE_VERSION,
            phase="SCAFFOLD", profile="script")
        self.state.requirements = s.RequirementContract(objective="demo")
        self.state.architecture = s.ArchitectureContract(
            approach="script", modules=[{"path": "app.py"}, {"path": "new.py"}],
            interfaces=[{"name": "main", "file": "app.py"}])
        self.state.tasks = [s.Task(id="t1", kind="build", target_file="new.py",
                                   acceptance=["runs"])]
        self.state.approve_architecture()
        self.adapter = engine.FactoryAdapter(
            self.settings, self.pool, lambda *a: None, self.state,
            isolate_workspace=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_workspace_is_seeded_and_project_untouched_by_scaffold(self):
        ws = Path(self.state.extensions["workspace_root"])
        self.assertTrue((ws / "app.py").is_file())
        self.assertEqual((ws / "app.py").read_text(), "old\n")
        self.assertTrue(self.adapter.step("SCAFFOLD"))
        self.adapter.sync_to_project()
        self.assertTrue((ws / "new.py").is_file())
        self.assertFalse((self.project / "new.py").exists())
        self.assertEqual((self.project / "app.py").read_text(), "old\n")

    def test_scaffold_is_idempotent(self):
        self.assertTrue(self.adapter.step("SCAFFOLD"))
        self.adapter.sync_to_project()
        snap = {p.name for p in Path(self.state.extensions["workspace_root"]).rglob("*")}
        self.assertTrue(self.adapter.step("SCAFFOLD"))
        self.adapter.sync_to_project()
        snap2 = {p.name for p in Path(self.state.extensions["workspace_root"]).rglob("*")}
        self.assertEqual(snap, snap2)

    def test_publish_updates_project_only_after_call(self):
        self.assertTrue(self.adapter.step("SCAFFOLD"))
        self.adapter.sync_to_project()
        ws = Path(self.state.extensions["workspace_root"])
        (ws / "new.py").write_text("generated\n", encoding="utf-8")
        self.assertFalse((self.project / "new.py").exists())
        result = self.adapter.publish()
        self.assertEqual(result["status"], "done")
        self.assertEqual((self.project / "new.py").read_text(), "generated\n")
        self.assertEqual((self.project / "app.py").read_text(), "old\n")

    def test_human_change_blocks_publish(self):
        self.assertTrue(self.adapter.step("SCAFFOLD"))
        self.adapter.sync_to_project()
        ws = Path(self.state.extensions["workspace_root"])
        (ws / "app.py").write_text("generated\n", encoding="utf-8")
        (self.project / "app.py").write_text("human\n", encoding="utf-8")
        with self.assertRaises(pub.PublishConflict):
            self.adapter.publish()
        self.assertEqual((self.project / "app.py").read_text(), "human\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
