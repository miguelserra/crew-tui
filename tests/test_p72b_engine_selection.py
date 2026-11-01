#!/usr/bin/env python3
"""P7.2b: adapter identity selection and non-destructive external adoption."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import adoption
import engine
import engine_selection as sel
try:
    import pydantic  # noqa: F401
except Exception:
    MAIN_DEPS_OK = False
else:
    MAIN_DEPS_OK = True
import state as s
from store import StateManager


class SelectionTests(unittest.TestCase):
    def _state(self, kind=s.ENGINE_UNIFIED, identity=None):
        st = s.ProjectState(run_id="r", goal="g", engine_kind=kind)
        if identity:
            sel.set_identity(st, identity)
        return st

    def test_new_default_is_native(self):
        identity, reason = sel.new_identity(env="")
        self.assertEqual(identity, "native")
        self.assertIn("default", reason)

    def test_new_explicit_factory_and_native(self):
        self.assertEqual(sel.new_identity("factory")[0], "factory")
        self.assertEqual(sel.new_identity("native")[0], "native")
        self.assertEqual(sel.new_identity("legacy")[0], "factory")
        self.assertEqual(sel.new_identity("unified")[0], "native")

    def test_legacy_state_infers_factory(self):
        identity, reason = sel.resume_identity(self._state(s.ENGINE_LEGACY))
        self.assertEqual(identity, "factory")
        self.assertIn("legacy", reason)

    def test_unified_missing_identity_is_ambiguous(self):
        with self.assertRaises(sel.EngineIdentityError):
            sel.resume_identity(self._state())

    def test_explicit_resolution_for_missing_identity(self):
        st = self._state()
        self.assertEqual(sel.resume_identity(st, "native")[0], "native")
        self.assertEqual(sel.resume_identity(st, "factory")[0], "factory")

    def test_conflicting_resume_override_is_rejected(self):
        st = self._state(identity="native")
        with self.assertRaises(sel.EngineIdentityError):
            sel.resume_identity(st, "factory")

    def test_identity_survives_reload_and_repeated_resume(self):
        st = self._state(identity="native")
        for _ in range(3):
            st = s.ProjectState.from_dict(st.to_dict())
            self.assertEqual(sel.resume_identity(st)[0], "native")

    def test_explicit_resolution_is_persisted(self):
        st = self._state()
        identity, _reason = sel.resolve_and_persist(st, "native")
        self.assertEqual(identity, "native")
        self.assertEqual(sel.identity_of(st), "native")
        reloaded = s.ProjectState.from_dict(st.to_dict())
        self.assertEqual(sel.resume_identity(reloaded)[0], "native")

    def test_status_describes_origin_identity_adapter(self):
        st = self._state(identity="factory")
        data = sel.describe(st)
        self.assertEqual(data["project_origin"], "CREATED_BY_CREW")
        self.assertEqual(data["persisted_engine_identity"], "factory")
        self.assertEqual(data["effective_adapter"], "FactoryAdapter")
        self.assertFalse(data["error"])

    def test_new_unified_state_persists_native_identity(self):
        with tempfile.TemporaryDirectory() as td:
            st = engine.new_unified_state("g", td)
            self.assertEqual(sel.identity_of(st), "native")
            self.assertEqual(sel.origin_of(st), "CREATED_BY_CREW")


class CliSelectionTests(unittest.TestCase):
    @unittest.skipUnless(MAIN_DEPS_OK, "pydantic/runtime deps missing")
    def test_cli_new_project_engine_selection_and_status(self):
        root = Path(__file__).resolve().parents[1]
        import os
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        for engine_value, expected in ((None, "native"),
                                       ("factory", "factory")):
            with tempfile.TemporaryDirectory() as td:
                brief = Path(td) / "brief.json"
                brief.write_text(json.dumps({
                    "project_name": "Engine Demo",
                    "project_description": "Build a page",
                    "main_objective": "Show a page",
                }), encoding="utf-8")
                project = Path(td) / "project"
                cmd = [sys.executable, "main.py", "--project", str(project),
                       "--brief", str(brief)]
                if engine_value:
                    cmd += ["--engine", engine_value]
                proc = subprocess.run(cmd, cwd=str(root), input="",
                                      capture_output=True, text=True,
                                      timeout=40, env=env)
                self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
                data = json.loads((project / ".crew" / "state.json").read_text())
                self.assertEqual(data["extensions"]["engine_identity"], expected)
                self.assertEqual(data["extensions"]["project_origin"],
                                 "CREATED_BY_CREW")
                status = subprocess.run(
                    [sys.executable, "main.py", "--status", "--project",
                     str(project)], cwd=str(root), capture_output=True,
                    text=True, timeout=30, env=env)
                self.assertEqual(status.returncode, 0, status.stderr)
                info = json.loads(status.stdout)
                self.assertEqual(info["persisted_engine_identity"], expected)
                self.assertEqual(info["effective_adapter"],
                                 "FactoryAdapter" if expected == "factory"
                                 else "NativeAdapter")

    @unittest.skipUnless(MAIN_DEPS_OK, "pydantic/runtime deps missing")
    def test_cli_adoption_registers_native_external_project(self):
        root = Path(__file__).resolve().parents[1]
        import os
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "external"
            repo.mkdir()
            (repo / "app.py").write_text("print('x')\n", encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "main.py", "--adopt", str(repo)],
                cwd=str(root), capture_output=True, text=True, timeout=40,
                env=env)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            status = subprocess.run(
                [sys.executable, "main.py", "--status", "--project", str(repo)],
                cwd=str(root), capture_output=True, text=True, timeout=30,
                env=env)
            info = json.loads(status.stdout)
            self.assertEqual(info["project_origin"],
                             "EXTERNAL_EXISTING_REPOSITORY")
            self.assertEqual(info["persisted_engine_identity"], "native")
            self.assertEqual(info["effective_adapter"], "NativeAdapter")


class AdoptionTests(unittest.TestCase):
    def _repo(self, td):
        root = Path(td) / "repo"
        root.mkdir()
        (root / "main.py").write_text("print('hello')\n", encoding="utf-8")
        (root / "package.json").write_text('{"name":"demo"}\n', encoding="utf-8")
        (root / "tests").mkdir()
        (root / "tests" / "test_main.py").write_text("def test_ok(): pass\n",
                                                     encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"],
                       check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "T"],
                       check=True)
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "initial"],
                       check=True)
        (root / "main.py").write_text("print('human change')\n", encoding="utf-8")
        return root

    def test_adoption_preserves_external_repository_and_baseline(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._repo(td)
            before_files = {
                str(p.relative_to(root)): p.read_bytes()
                for p in root.rglob("*") if p.is_file() and ".git" not in p.parts
            }
            head_before = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"], check=True,
                capture_output=True, text=True).stdout.strip()
            store = StateManager(Path(td) / "ws", Path(td) / "logs")
            result = adoption.adopt_repository(root, store)
            st = result["state"]
            self.assertEqual(sel.identity_of(st), "native")
            self.assertEqual(sel.origin_of(st),
                             sel.EXTERNAL_EXISTING_REPOSITORY)
            baseline = st.extensions["adoption"]["baseline"]
            self.assertIn("main.py", baseline["file_sha256"])
            self.assertTrue(baseline["git_dirty"])
            self.assertIn("Python", baseline["languages"])
            self.assertIn("package.json", baseline["configurations"])
            self.assertIn("tests/test_main.py", baseline["tests"])
            self.assertEqual(result["baseline"]["git_head"], head_before)
            self.assertTrue(st.extensions["adoption"]["verified_unchanged"])
            for rel, data in before_files.items():
                self.assertEqual((root / rel).read_bytes(), data, rel)
            policy = st.extensions["adoption"]["policy"]
            self.assertFalse(policy["automatic_restructure"])
            self.assertFalse(policy["automatic_dependency_upgrade"])
            self.assertFalse(policy["automatic_scaffold_over_existing"])
            self.assertFalse(policy["silent_delete"])
            self.assertTrue(policy["chestertons_fence"])
            self.assertTrue((root / ".crew" / "state.json").is_file())

    def test_adoption_rejects_managed_or_factory_state(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateManager(Path(td) / "ws", Path(td) / "logs")
            root = Path(td) / "crew"
            (root / ".crew").mkdir(parents=True)
            (root / ".crew" / "state.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(adoption.AdoptionError):
                adoption.adopt_repository(root, store)
            root2 = Path(td) / "factory"
            (root2 / ".factory").mkdir(parents=True)
            (root2 / ".factory" / "state.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(adoption.AdoptionError):
                adoption.adopt_repository(root2, store)

    def test_baseline_reports_coverage_and_limits(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.py").write_text("a" * 10, encoding="utf-8")
            (root / "b.py").write_text("b" * 10, encoding="utf-8")
            bounded = adoption.inspect_repository(root, max_files=1)
            self.assertFalse(bounded["inventory_complete"])
            self.assertEqual(bounded["files_total_regular"], 2)
            self.assertEqual(bounded["hashed_count"], 1)
            self.assertEqual(bounded["hash_coverage"], 0.5)
            self.assertFalse(bounded["hash_complete"])
            limited = adoption.inspect_repository(root, max_file_bytes=1)
            self.assertTrue(limited["hash_skipped"])
            self.assertFalse(limited["hash_complete"])

    def test_adoption_incomplete_baseline_does_not_claim_full_integrity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            root.mkdir()
            (root / "a.py").write_text("content", encoding="utf-8")
            store = StateManager(Path(td) / "ws", Path(td) / "logs")
            result = adoption.adopt_repository(root, store, max_file_bytes=1)
            st = result["state"]
            self.assertFalse(st.extensions["adoption"]["verified_unchanged"])
            integrity = st.extensions["adoption"]["integrity"]
            self.assertFalse(integrity["baseline_hash_complete"])
            self.assertGreater(integrity["hash_skipped_count"], 0)

    def test_symlinks_and_lfs_metadata_are_recorded_without_following(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            outside = Path(td) / "outside.txt"
            root.mkdir()
            outside.write_text("outside\n", encoding="utf-8")
            (root / "lfs.bin").write_text(
                "version https://git-lfs.github.com/spec/v1\noid sha256:abc\n",
                encoding="utf-8")
            try:
                (root / "link.txt").symlink_to(outside)
            except OSError:
                self.skipTest("symlink not permitted")
            info = adoption.inspect_repository(root)
            self.assertEqual(len(info["symlinks"]), 1)
            self.assertFalse(info["symlinks"][0]["target_inside_root"])
            self.assertNotIn("link.txt", info["file_sha256"])
            self.assertNotIn("outside.txt", info["file_sha256"])
            self.assertEqual(info["lfs_pointers"], ["lfs.bin"])

    def test_baseline_inspection_is_deterministic(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.py").write_text("a", encoding="utf-8")
            (root / "b.txt").write_text("b", encoding="utf-8")
            first = adoption.inspect_repository(root)
            second = adoption.inspect_repository(root)
            for key in ("files", "file_sha256", "languages", "inventory_total",
                        "hashed_count", "hash_coverage", "hash_complete"):
                self.assertEqual(first[key], second[key], key)

    def test_adoption_does_not_overwrite_configuration(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateManager(Path(td) / "ws", Path(td) / "logs")
            root = Path(td) / "repo"
            root.mkdir()
            (root / "pyproject.toml").write_text("[project]\nname='x'\n",
                                                 encoding="utf-8")
            (root / "README.md").write_text("keep\n", encoding="utf-8")
            adoption.adopt_repository(root, store)
            self.assertEqual((root / "pyproject.toml").read_text(),
                             "[project]\nname='x'\n")
            self.assertEqual((root / "README.md").read_text(), "keep\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
