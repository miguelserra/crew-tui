#!/usr/bin/env python3
"""P6: workspace isolation and transactional publish."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import publish as pub
import workspace as ws


class WorkspaceTests(unittest.TestCase):
    def test_prepare_copies_existing_and_excludes_internal(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            work = Path(td) / "work"
            project.mkdir()
            (project / "app.py").write_text("old\n", encoding="utf-8")
            (project / ".git").mkdir()
            (project / ".git" / "x").write_text("x", encoding="utf-8")
            (project / ".env").write_text("SECRET=1", encoding="utf-8")
            origin = ws.prepare_workspace(project, work)
            self.assertEqual((work / "app.py").read_text(), "old\n")
            self.assertFalse((work / ".git").exists())
            self.assertFalse((work / ".env").exists())
            self.assertIn("app.py", origin)

    def test_plan_create_replace_skip_and_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            work = Path(td) / "work"
            project.mkdir()
            (project / "keep.py").write_text("keep", encoding="utf-8")
            (project / "change.py").write_text("old", encoding="utf-8")
            work.mkdir()
            (work / "keep.py").write_text("keep", encoding="utf-8")
            (work / "change.py").write_text("new", encoding="utf-8")
            (work / "new.py").write_text("new", encoding="utf-8")
            origin = {"keep.py": ws.sha256_file(project / "keep.py"),
                      "change.py": ws.sha256_file(project / "change.py")}
            plan = ws.plan_publish(project, work, origin)
            actions = {e.rel: e.action for e in plan.entries}
            self.assertEqual(actions["keep.py"], "skip")
            self.assertEqual(actions["change.py"], "replace")
            self.assertEqual(actions["new.py"], "create")
            (project / "change.py").write_text("human", encoding="utf-8")
            plan2 = ws.plan_publish(project, work, origin)
            self.assertTrue(plan2.conflicts)


class PublishTests(unittest.TestCase):
    def _setup(self, td):
        project = Path(td) / "project"
        work = Path(td) / "work"
        state = Path(td) / "state"
        project.mkdir()
        work.mkdir()
        state.mkdir()
        (project / "change.py").write_text("old", encoding="utf-8")
        (work / "change.py").write_text("new", encoding="utf-8")
        (work / "new.py").write_text("brand new", encoding="utf-8")
        origin = {"change.py": ws.sha256_file(project / "change.py")}
        return project, work, state, origin

    def test_publish_updates_files_and_writes_journal(self):
        with tempfile.TemporaryDirectory() as td:
            project, work, state, origin = self._setup(td)
            mgr = pub.PublishManager(project, work, state, origin)
            result = mgr.publish()
            self.assertEqual(result["status"], "COMMITTED")
            self.assertEqual((project / "change.py").read_text(), "new")
            self.assertEqual((project / "new.py").read_text(), "brand new")
            journals = list((state / "publish").glob("*/journal.json"))
            self.assertEqual(len(journals), 1)
            data = json.loads(journals[0].read_text())
            self.assertEqual(data["status"], "COMMITTED")
            self.assertTrue((journals[0].parent / "backup" / "change.py").is_file())

    def test_conflict_blocks_without_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            project, work, state, origin = self._setup(td)
            (project / "change.py").write_text("human", encoding="utf-8")
            mgr = pub.PublishManager(project, work, state, origin)
            with self.assertRaises(pub.PublishConflict):
                mgr.publish()
            self.assertEqual((project / "change.py").read_text(), "human")

    def test_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as td:
            project, work, state, origin = self._setup(td)
            mgr = pub.PublishManager(project, work, state, origin)
            original = pub.PublishManager._apply
            calls = {"n": 0}

            def flaky(self_, item):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise RuntimeError("boom")
                return original(self_, item)

            pub.PublishManager._apply = flaky
            try:
                with self.assertRaises(pub.PublishError):
                    mgr.publish()
            finally:
                pub.PublishManager._apply = original
            self.assertEqual((project / "change.py").read_text(), "old")
            self.assertFalse((project / "new.py").exists())

    def test_recovery_reverts_interrupted_journal(self):
        with tempfile.TemporaryDirectory() as td:
            project, work, state, origin = self._setup(td)
            run = state / "publish" / "run1"
            (run / "backup").mkdir(parents=True)
            old_sha = ws.sha256_file(project / "change.py")
            (run / "backup" / "change.py").write_text("old", encoding="utf-8")
            (project / "change.py").write_text("new", encoding="utf-8")
            (project / "new.py").write_text("brand new", encoding="utf-8")
            journal = {"id": "run1", "status": "PUBLISHING",
                       "entries": [
                           {"rel": "change.py", "action": "replace",
                            "old_sha": old_sha,
                            "new_sha": ws.sha256_file(project / "change.py"),
                            "backup": str(run / "backup" / "change.py"),
                            "status": "done"},
                           {"rel": "new.py", "action": "create", "old_sha": "",
                            "new_sha": ws.sha256_file(project / "new.py"),
                            "backup": None, "status": "done"},
                       ]}
            (run / "journal.json").write_text(json.dumps(journal), encoding="utf-8")
            mgr = pub.PublishManager(project, work, state, origin)
            result = mgr.recover()
            self.assertEqual(result["status"], "ROLLED_BACK")
            self.assertEqual((project / "change.py").read_text(), "old")
            self.assertFalse((project / "new.py").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
