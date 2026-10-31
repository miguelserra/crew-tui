#!/usr/bin/env python3
"""P6.1 publisher crash-recovery matrix (fault injection at each boundary)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import publish as pub
import workspace as ws


class Crash(BaseException):
    """Simulates process death: bypasses the normal Exception rollback path."""


def _setup(base: Path, n=3):
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


def _journal(mgr):
    files = sorted(mgr.root.glob("*/journal.json"))
    if not files:
        return None
    return json.loads(files[-1].read_text(encoding="utf-8"))


def _texts(project: Path, n=3):
    return [(project / f"f{i}.py").read_text() for i in range(n)]


class RecoveryTests(unittest.TestCase):
    def test_crash_after_journal_rolls_back(self):
        with tempfile.TemporaryDirectory() as td:
            project, work, state, origin = _setup(Path(td))

            def fault(name, info):
                if name == "after_journal":
                    raise Crash()

            mgr = pub.PublishManager(project, work, state, origin, fault=fault)
            with self.assertRaises(Crash):
                mgr.publish()
            self.assertEqual(_texts(project), ["old0", "old1", "old2"])
            self.assertEqual(_journal(mgr)["status"], "PREPARED")

            fresh = pub.PublishManager(project, work, state, origin)
            self.assertEqual(fresh.recover()["status"], "ROLLED_BACK")
            self.assertEqual(_texts(project), ["old0", "old1", "old2"])
            self.assertIsNone(fresh.recovery_required())

    def test_crash_after_publishing_rolls_back(self):
        with tempfile.TemporaryDirectory() as td:
            project, work, state, origin = _setup(Path(td))

            def fault(name, info):
                if name == "after_publishing":
                    raise Crash()

            mgr = pub.PublishManager(project, work, state, origin, fault=fault)
            with self.assertRaises(Crash):
                mgr.publish()
            self.assertEqual(_texts(project), ["old0", "old1", "old2"])
            self.assertEqual(_journal(mgr)["status"], "PUBLISHING")
            self.assertEqual(pub.PublishManager(project, work, state, origin)
                             .recover()["status"], "ROLLED_BACK")
            self.assertEqual(_texts(project), ["old0", "old1", "old2"])

    def test_crash_mid_apply_never_reports_committed(self):
        with tempfile.TemporaryDirectory() as td:
            project, work, state, origin = _setup(Path(td))

            def fault(name, info):
                if name == "after_apply" and info.get("rel") == "f0.py":
                    raise Crash()

            mgr = pub.PublishManager(project, work, state, origin, fault=fault)
            with self.assertRaises(Crash):
                mgr.publish()
            journal = _journal(mgr)
            self.assertNotEqual(journal["status"], "COMMITTED")
            self.assertEqual(journal["status"], "PUBLISHING")
            self.assertEqual(_texts(project), ["new0", "old1", "old2"])

            fresh = pub.PublishManager(project, work, state, origin)
            self.assertEqual(fresh.recover()["status"], "ROLLED_BACK")
            self.assertEqual(_texts(project), ["old0", "old1", "old2"])
            self.assertIsNone(fresh.recovery_required())

    def test_crash_after_all_applied_before_commit_rolls_back(self):
        with tempfile.TemporaryDirectory() as td:
            project, work, state, origin = _setup(Path(td))

            def fault(name, info):
                if name == "before_commit":
                    raise Crash()

            mgr = pub.PublishManager(project, work, state, origin, fault=fault)
            with self.assertRaises(Crash):
                mgr.publish()
            self.assertEqual(_texts(project), ["new0", "new1", "new2"])
            self.assertEqual(_journal(mgr)["status"], "PUBLISHING")
            self.assertEqual(pub.PublishManager(project, work, state, origin)
                             .recover()["status"], "ROLLED_BACK")
            self.assertEqual(_texts(project), ["old0", "old1", "old2"])

    def test_crash_after_commit_reports_published(self):
        with tempfile.TemporaryDirectory() as td:
            project, work, state, origin = _setup(Path(td))

            def fault(name, info):
                if name == "after_commit":
                    raise Crash()

            mgr = pub.PublishManager(project, work, state, origin, fault=fault)
            with self.assertRaises(Crash):
                mgr.publish()
            self.assertEqual(_journal(mgr)["status"], "COMMITTED")
            self.assertEqual(_texts(project), ["new0", "new1", "new2"])
            fresh = pub.PublishManager(project, work, state, origin)
            self.assertEqual(fresh.recover()["status"], "COMMITTED")
            self.assertEqual(_texts(project), ["new0", "new1", "new2"])

    def test_crash_during_rollback_is_retried(self):
        with tempfile.TemporaryDirectory() as td:
            project, work, state, origin = _setup(Path(td))
            crashed = {"n": 0}

            def fault(name, info):
                if name == "before_apply" and info.get("rel") == "f1.py":
                    raise RuntimeError("boom")
                if name == "during_rollback" and info.get("rel") == "f0.py" \
                        and not crashed["n"]:
                    crashed["n"] = 1
                    raise Crash()

            mgr = pub.PublishManager(project, work, state, origin, fault=fault)
            with self.assertRaises(Crash):
                mgr.publish()
            self.assertEqual(_journal(mgr)["status"], "ROLLING_BACK")
            self.assertEqual(_texts(project), ["new0", "old1", "old2"])

            fresh = pub.PublishManager(project, work, state, origin)
            self.assertEqual(fresh.recover()["status"], "ROLLED_BACK")
            self.assertEqual(_texts(project), ["old0", "old1", "old2"])
            self.assertIsNone(fresh.recovery_required())

    def test_rollback_failure_requires_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            project, work, state, origin = _setup(Path(td))
            crashed = {"n": 0}

            def fault(name, info):
                if name == "before_apply" and info.get("rel") == "f1.py":
                    raise RuntimeError("boom")
                if name == "during_rollback" and info.get("rel") == "f0.py" \
                        and not crashed["n"]:
                    crashed["n"] = 1
                    raise Crash()

            mgr = pub.PublishManager(project, work, state, origin, fault=fault)
            with self.assertRaises(Crash):
                mgr.publish()
            backup = next(mgr.root.glob("*/backup/f0.py"))
            backup.unlink()

            fresh = pub.PublishManager(project, work, state, origin)
            with self.assertRaises(pub.RecoveryRequired):
                fresh.recover()
            self.assertIsNotNone(fresh.recovery_required())
            self.assertEqual(_journal(fresh)["status"], "RECOVERY_REQUIRED")

    def test_crash_during_backup_leaves_project_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            project, work, state, origin = _setup(Path(td))

            def fault(name, info):
                if name == "after_backup" and info.get("rel") == "f0.py":
                    raise Crash()

            mgr = pub.PublishManager(project, work, state, origin, fault=fault)
            with self.assertRaises(Crash):
                mgr.publish()
            self.assertIsNone(_journal(mgr))
            self.assertEqual(_texts(project), ["old0", "old1", "old2"])
            self.assertIsNone(pub.PublishManager(project, work, state, origin)
                              .recover())
            self.assertEqual(_texts(project), ["old0", "old1", "old2"])

    def test_crash_before_journal_leaves_project_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            project, work, state, origin = _setup(Path(td))

            def fault(name, info):
                if name == "before_journal":
                    raise Crash()

            mgr = pub.PublishManager(project, work, state, origin, fault=fault)
            with self.assertRaises(Crash):
                mgr.publish()
            self.assertIsNone(_journal(mgr))
            self.assertEqual(_texts(project), ["old0", "old1", "old2"])
            fresh = pub.PublishManager(project, work, state, origin)
            self.assertIsNone(fresh.recover())
            self.assertEqual(_texts(project), ["old0", "old1", "old2"])

    def test_orphan_run_dir_is_detected_and_cleaned(self):
        with tempfile.TemporaryDirectory() as td:
            project, work, state, origin = _setup(Path(td))
            mgr = pub.PublishManager(project, work, state, origin)
            orphan = mgr.root / "lonely"
            orphan.mkdir(parents=True)
            (orphan / "backup.bin").write_bytes(b"diagnostic")
            self.assertEqual([o["id"] for o in mgr.orphans()], ["lonely"])
            preview = mgr.clean_orphans(dry_run=True)
            self.assertEqual(preview["removed"], [])
            self.assertTrue(orphan.is_dir())
            result = mgr.clean_orphans(["lonely"])
            self.assertEqual(result["removed"], ["lonely"])
            self.assertFalse(orphan.exists())

    def test_orphan_cleanup_does_not_touch_journals(self):
        with tempfile.TemporaryDirectory() as td:
            project, work, state, origin = _setup(Path(td))
            mgr = pub.PublishManager(project, work, state, origin)
            mgr.publish()
            orphan = mgr.root / "lonely"
            orphan.mkdir(parents=True)
            (orphan / "x").write_text("x", encoding="utf-8")
            mgr.clean_orphans(["lonely"])
            self.assertTrue((mgr.root / _journal(mgr)["id"] / "journal.json").is_file())

    def test_real_process_death_is_recovered(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            project, work, state, origin = _setup(base, 3)
            child = r"""
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
import publish, workspace as ws
base = Path(sys.argv[1])
project, work, state = base / "project", base / "work", base / "state"
origin = {f"f{i}.py": ws.sha256_file(project / f"f{i}.py") for i in range(3)}
def fault(name, info):
    if name == "after_apply" and info.get("rel") == "f0.py":
        os._exit(99)
publish.PublishManager(project, work, state, origin, fault=fault).publish()
"""
            completed = subprocess.run(
                [sys.executable, "-c", child, str(base), str(ROOT)],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(completed.returncode, 99, completed.stderr)
            self.assertEqual(_texts(project), ["new0", "old1", "old2"])
            self.assertEqual(_journal(pub.PublishManager(project, work, state,
                                                         origin))["status"],
                             "PUBLISHING")
            fresh = pub.PublishManager(project, work, state, origin)
            self.assertEqual(fresh.recover()["status"], "ROLLED_BACK")
            self.assertEqual(_texts(project), ["old0", "old1", "old2"])
            self.assertIsNone(fresh.recovery_required())

    def test_normal_publish_is_committed(self):
        with tempfile.TemporaryDirectory() as td:
            project, work, state, origin = _setup(Path(td))
            mgr = pub.PublishManager(project, work, state, origin)
            result = mgr.publish()
            self.assertEqual(result["status"], "COMMITTED")
            self.assertEqual(result["entries"], 3)
            self.assertEqual(_texts(project), ["new0", "new1", "new2"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
