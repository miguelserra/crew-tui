#!/usr/bin/env python3
"""P6: deterministic scaffold."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scaffold as sc
import state as s


def arch():
    return s.ArchitectureContract(
        approach="app",
        modules=[{"path": "src/"}, {"path": "app.py"}],
        interfaces=[{"name": "main", "file": "app.py"}],
    )


def tasks():
    return [s.Task(id="t1", kind="build", target_file="src/util.py")]


class ScaffoldTests(unittest.TestCase):
    def test_creates_empty_files_and_preserves_existing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("keep me", encoding="utf-8")
            manifest = sc.scaffold(root, arch(), tasks(), approved=True)
            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), "keep me")
            self.assertTrue((root / "src" / "util.py").is_file())
            self.assertEqual((root / "src" / "util.py").read_text(), "")
            self.assertIn("app.py", manifest.reused)
            self.assertIn("src/util.py", manifest.created)

    def test_idempotent_and_no_llm(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = sc.scaffold(root, arch(), tasks(), approved=True)
            snap1 = sc.snapshot_files(root)
            second = sc.scaffold(root, arch(), tasks(), approved=True)
            snap2 = sc.snapshot_files(root)
            self.assertEqual(snap1, snap2)
            self.assertIn("app.py", first.created)
            self.assertIn("app.py", second.reused)
            self.assertEqual(second.created, [])

    def test_requires_approval(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(s.StateValidationError):
                sc.scaffold(td, arch(), tasks(), approved=False)

    def test_case_conflict_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            bad = s.ArchitectureContract(
                modules=[{"path": "src/Main.py"}],
                interfaces=[{"name": "x", "file": "src/main.py"}])
            with self.assertRaises(s.StateValidationError):
                sc.scaffold(td, bad, [], approved=True)

    def test_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            bad = s.ArchitectureContract(
                modules=[{"path": "../outside.py"}])
            with self.assertRaises(s.StateValidationError):
                sc.scaffold(td, bad, [], approved=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
