#!/usr/bin/env python3
"""P6.1: ignore safety, workspace integrity, rollback/recovery and resolution."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import publish as pub
import workspace as ws


class IgnoreTests(unittest.TestCase):
    def test_security_and_technical_exclusions(self):
        r = ws.IgnoreRules()
        for name in (".env", ".env.local", ".env.production", "id_rsa",
                     "server.pem", "server.key", ".ssh/config", ".aws/credentials"):
            self.assertTrue(r.is_excluded(name), name)
        self.assertFalse(r.is_excluded(".env.example"))
        for name in (".vs/x", "bin/app.dll", "obj/x", ".vscode/settings.json",
                     "node_modules/x", "dist/app.js", "__pycache__/x.pyc"):
            self.assertTrue(r.is_excluded(name), name)

    def test_crewignore_gitignore_and_include(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".crewignore").write_text("secret.txt\n*.tmp\n!.keep.tmp\n",
                                              encoding="utf-8")
            (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
            r = ws.IgnoreRules.from_project(root)
            self.assertEqual(r.reason("secret.txt"), ".crewignore")
            self.assertEqual(r.reason("x.log"), ".gitignore")
            self.assertFalse(r.is_excluded(".keep.tmp"))
            self.assertFalse(r.is_excluded("src/main.py"))


class IntegrityTests(unittest.TestCase):
    def test_missing_original_blocks_publish(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            project = base / "project"
            work = base / "work"
            project.mkdir()
            (project / "config.py").write_text("old", encoding="utf-8")
            origin = ws.prepare_workspace(project, work)
            (work / "config.py").unlink()
            manifest = ws.validate_workspace_integrity(project, work, origin)
            self.assertFalse(manifest.ok)
            plan = ws.plan_publish(project, work, origin, manifest=manifest)
            self.assertTrue(plan.conflicts)

    def test_approved_deletion_allows_missing_original(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            project = base / "project"
            work = base / "work"
            project.mkdir()
            (project / "old.py").write_text("old", encoding="utf-8")
            old = ws.sha256_file(project / "old.py")
            origin = ws.prepare_workspace(project, work)
            (work / "old.py").unlink()
            deletion = SimpleNamespace(target_file="old.py", expected_sha256=old,
                                       requirement_id="ac1")
            manifest = ws.validate_workspace_integrity(
                project, work, origin, approved_deletions=[deletion])
            self.assertTrue(manifest.ok)
            plan = ws.plan_publish(project, work, origin,
                                   approved_deletions=[deletion], manifest=manifest)
            self.assertEqual(plan.conflicts, [])
            self.assertTrue(any(e.action == "delete" for e in plan.entries))

    def test_approved_deletion_can_be_reverted_from_journal(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            project = base / "project"
            work = base / "work"
            project.mkdir()
            work.mkdir()
            (project / "old.py").write_text("old", encoding="utf-8")
            old = ws.sha256_file(project / "old.py")
            origin = {"old.py": old}
            deletion = SimpleNamespace(target_file="old.py",
                                       expected_sha256=old,
                                       requirement_id="ac1")
            manifest = ws.validate_workspace_integrity(
                project, work, origin, approved_deletions=[deletion])
            mgr = pub.PublishManager(project, work, base / "state", origin,
                                     deletions=[deletion], manifest=manifest)
            result = mgr.publish()
            self.assertFalse((project / "old.py").exists())
            resolved = mgr.resolve(result["id"], "old.py", "original")
            self.assertEqual(resolved["status"], "REVALIDATION_REQUIRED")
            self.assertEqual((project / "old.py").read_text(), "old")

    def test_human_only_change_is_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            project = base / "project"
            work = base / "work"
            project.mkdir()
            (project / "main.py").write_text("old", encoding="utf-8")
            origin = ws.prepare_workspace(project, work)
            (project / "main.py").write_text("human", encoding="utf-8")
            manifest = ws.validate_workspace_integrity(project, work, origin)
            plan = ws.plan_publish(project, work, origin, manifest=manifest)
            self.assertEqual(plan.entries[0].action, "skip")
            self.assertEqual(plan.conflicts, [])
            mgr = pub.PublishManager(project, work, base / "state", origin,
                                     manifest=manifest)
            mgr.publish()
            self.assertEqual((project / "main.py").read_text(), "human")


class PublishSafetyTests(unittest.TestCase):
    def _setup(self, td, n=3):
        base = Path(td)
        project = base / "project"
        work = base / "work"
        state = base / "state"
        project.mkdir()
        work.mkdir()
        state.mkdir()
        origin = {}
        for i in range(n):
            rel = f"f{i}.py"
            (project / rel).write_text(f"old{i}", encoding="utf-8")
            (work / rel).write_text(f"new{i}", encoding="utf-8")
            origin[rel] = ws.sha256_file(project / rel)
        return project, work, state, origin

    def test_failure_after_first_rolls_back_all(self):
        with tempfile.TemporaryDirectory() as td:
            project, work, state, origin = self._setup(td, 3)
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
            for i in range(3):
                self.assertEqual((project / f"f{i}.py").read_text(), f"old{i}")

    def test_human_third_version_blocks_rollback(self):
        with tempfile.TemporaryDirectory() as td:
            project, work, state, origin = self._setup(td, 3)
            mgr = pub.PublishManager(project, work, state, origin)
            original = pub.PublishManager._apply
            calls = {"n": 0}

            def flaky(self_, item):
                calls["n"] += 1
                if calls["n"] == 2:
                    # human edits the already-published first file
                    (project / "f0.py").write_text("human", encoding="utf-8")
                    raise RuntimeError("boom")
                return original(self_, item)

            pub.PublishManager._apply = flaky
            try:
                with self.assertRaises(pub.PublishError):
                    mgr.publish()
            finally:
                pub.PublishManager._apply = original
            self.assertEqual((project / "f0.py").read_text(), "human")
            self.assertIsNotNone(mgr.recovery_required())

    def test_interruption_after_replace_recovers_original(self):
        with tempfile.TemporaryDirectory() as td:
            project, work, state, origin = self._setup(td, 1)
            run = state / "publish" / "run1"
            (run / "backup").mkdir(parents=True)
            (run / "backup" / "f0.py").write_text("old0", encoding="utf-8")
            (project / "f0.py").write_text("new0", encoding="utf-8")
            journal = {"id": "run1", "status": "PUBLISHING",
                       "entries": [{"rel": "f0.py", "action": "replace",
                                    "old_sha": origin["f0.py"],
                                    "new_sha": ws.sha256_file(project / "f0.py"),
                                    "backup": str(run / "backup" / "f0.py"),
                                    "generated": None, "status": "applying"}]}
            (run / "journal.json").write_text(json.dumps(journal), encoding="utf-8")
            mgr = pub.PublishManager(project, work, state, origin)
            result = mgr.recover()
            self.assertEqual(result["status"], "ROLLED_BACK")
            self.assertEqual((project / "f0.py").read_text(), "old0")

    def test_corrupt_backup_requires_recovery_without_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            project, work, state, origin = self._setup(td, 1)
            run = state / "publish" / "run1"
            (run / "backup").mkdir(parents=True)
            (run / "backup" / "f0.py").write_text("corrupt", encoding="utf-8")
            (project / "f0.py").write_text("new0", encoding="utf-8")
            journal = {"id": "run1", "status": "PUBLISHING",
                       "entries": [{"rel": "f0.py", "action": "replace",
                                    "old_sha": origin["f0.py"],
                                    "new_sha": ws.sha256_file(project / "f0.py"),
                                    "backup": str(run / "backup" / "f0.py"),
                                    "generated": None, "status": "done"}]}
            (run / "journal.json").write_text(json.dumps(journal), encoding="utf-8")
            mgr = pub.PublishManager(project, work, state, origin)
            with self.assertRaises(pub.RecoveryRequired):
                mgr.recover()
            self.assertEqual((project / "f0.py").read_text(), "new0")
            self.assertIsNotNone(mgr.recovery_required())

    def test_corrupt_or_malicious_journal_blocks_without_writes(self):
        with tempfile.TemporaryDirectory() as td:
            project, work, state, origin = self._setup(td, 1)
            run = state / "publish" / "bad"
            run.mkdir(parents=True)
            (run / "journal.json").write_text("{broken", encoding="utf-8")
            mgr = pub.PublishManager(project, work, state, origin)
            with self.assertRaises(pub.RecoveryRequired):
                mgr.recover()
            self.assertIsNotNone(mgr.recovery_required())
            (run / "journal.json").unlink()
            (run / "journal.json").write_text(json.dumps({
                "id": "bad", "status": "PUBLISHING",
                "entries": [{"rel": "../../outside.py", "action": "create",
                             "new_sha": "x", "status": "done"}]}), encoding="utf-8")
            with self.assertRaises(pub.RecoveryRequired):
                mgr.recover()

    def test_symlink_destination_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            project, work, state, origin = self._setup(td, 1)
            outside = Path(td) / "outside.py"
            outside.write_text("outside", encoding="utf-8")
            (project / "f0.py").unlink()
            try:
                (project / "f0.py").symlink_to(outside)
            except OSError:
                self.skipTest("symlink not permitted")
            mgr = pub.PublishManager(project, work, state, origin)
            with self.assertRaises(pub.PublishError):
                mgr.publish()

    def test_lock_blocks_concurrent_publish(self):
        with tempfile.TemporaryDirectory() as td:
            project, work, state, origin = self._setup(td, 1)
            mgr = pub.PublishManager(project, work, state, origin)
            with pub.ProjectLock(mgr.lock_path):
                with self.assertRaises(pub.PublishBusy):
                    mgr.publish()

    def test_resolution_manual_requires_revalidation(self):
        with tempfile.TemporaryDirectory() as td:
            project, work, state, origin = self._setup(td, 1)
            run = state / "publish" / "run1"
            (run / "backup").mkdir(parents=True)
            (run / "generated").mkdir()
            (run / "backup" / "f0.py").write_text("old0", encoding="utf-8")
            (run / "generated" / "f0.py").write_text("new0", encoding="utf-8")
            (project / "f0.py").write_text("human", encoding="utf-8")
            journal = {"id": "run1", "status": "RECOVERY_REQUIRED",
                       "entries": [{"rel": "f0.py", "action": "replace",
                                    "old_sha": origin["f0.py"],
                                    "new_sha": ws.sha256_file(run / "generated" / "f0.py"),
                                    "backup": str(run / "backup" / "f0.py"),
                                    "generated": str(run / "generated" / "f0.py"),
                                    "status": "recovery_required"}]}
            (run / "journal.json").write_text(json.dumps(journal), encoding="utf-8")
            manual = Path(td) / "manual.py"
            manual.write_text("manual", encoding="utf-8")
            mgr = pub.PublishManager(project, work, state, origin)
            result = mgr.resolve("run1", "f0.py", "manual", str(manual))
            self.assertEqual(result["status"], "REVALIDATION_REQUIRED")
            self.assertEqual((project / "f0.py").read_text(), "manual")


if __name__ == "__main__":
    unittest.main(verbosity=2)
