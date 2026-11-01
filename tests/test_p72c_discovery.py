#!/usr/bin/env python3
"""P7.2c: static project discovery, dossier, freshness and agent context."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import adoption
import discovery
import state as s
from store import StateManager

try:
    import pydantic  # noqa: F401
except Exception:
    MAIN_DEPS_OK = False
else:
    MAIN_DEPS_OK = True


def _fixture(root: Path) -> None:
    (root / "app").mkdir()
    (root / "tests").mkdir()
    (root / "web").mkdir()
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / "main.py").write_text(
        "from app.core import Service\n"
        "def main():\n    return Service().run()\n"
        "if __name__ == '__main__':\n    main()\n", encoding="utf-8")
    (root / "app" / "core.py").write_text(
        "import os\nimport sqlite3\nfrom app.util import helper\n"
        "class Service:\n    def run(self):\n        return helper()\n"
        "# TODO: preserve the legacy protocol\n", encoding="utf-8")
    (root / "app" / "util.py").write_text(
        "def helper():\n    return 'ok'\n", encoding="utf-8")
    (root / "tests" / "test_core.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8")
    (root / "web" / "index.js").write_text(
        "import axios from 'axios';\nexport function boot() { return 1; }\n",
        encoding="utf-8")
    (root / "package.json").write_text(json.dumps({
        "name": "fixture", "main": "web/index.js",
        "dependencies": {"react": "18.0.0", "axios": "1.0.0"},
        "scripts": {"test": "node test.js"},
    }), encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[project]\nname='fixture'\ndependencies=['pytest']\n",
        encoding="utf-8")
    (root / "README.md").write_text("# Fixture\n", encoding="utf-8")
    (root / ".github" / "workflows" / "ci.yml").write_text(
        "name: CI\non: push\n", encoding="utf-8")


class DiscoveryTests(unittest.TestCase):
    def test_repository_understanding_is_evidence_based(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _fixture(root)
            dossier = discovery.analyze(root)
            self.assertTrue(dossier["overview"]["project_name"])
            self.assertIn("Python", dossier["technology_stack"]["languages"])
            self.assertIn("JavaScript", dossier["technology_stack"]["languages"])
            self.assertTrue(any(f["name"] == "React"
                                for f in dossier["technology_stack"]["frameworks"]))
            entries = {e["file"] for e in dossier["entry_points"]}
            self.assertIn("main.py", entries)
            self.assertIn("web/index.js", entries)
            edges = {(e["from"], e["to"]) for e in
                     dossier["dependencies"]["internal"]}
            self.assertIn(("main.py", "app/core.py"), edges)
            self.assertIn(("app/core.py", "app/util.py"), edges)
            self.assertTrue(all(e.get("status") == "OBSERVED"
                                for e in dossier["dependencies"]["internal"]))
            self.assertTrue(dossier["quality_signals"]["tests"])
            self.assertTrue(dossier["quality_signals"]["ci"])
            self.assertTrue(any(m["file"] == "app/core.py"
                                for m in dossier["quality_signals"]["markers"]))
            self.assertTrue(dossier["diagrams"]["component"])
            self.assertTrue(dossier["diagrams"]["dependency"])
            self.assertEqual(dossier["analysis_scope"]["runtime_execution"], "none")
            self.assertEqual(dossier["analysis_scope"]["dependency_installation"], "none")
            self.assertTrue(dossier["epistemic"]["OBSERVED"] > 0)
            for group in ("external_interfaces", "persistence"):
                for item in dossier[group]:
                    for ev in item.get("evidence") or []:
                        self.assertEqual(ev["file"], "package.json"
                                         if group == "persistence" and
                                         ev["file"].endswith("json") else ev["file"])

    def test_every_source_reference_points_to_a_real_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _fixture(root)
            dossier = discovery.analyze(root)
            refs = []
            def walk(value):
                if isinstance(value, dict):
                    if isinstance(value.get("evidence"), list):
                        refs.extend(value["evidence"])
                    for item in value.values():
                        walk(item)
                elif isinstance(value, list):
                    for item in value:
                        walk(item)
            walk(dossier["components"])
            walk(dossier["external_interfaces"])
            walk(dossier["persistence"])
            walk(dossier["risks"])
            walk(dossier["preserve"])
            walk(dossier["conflicts"])
            self.assertTrue(refs)
            for ref in refs:
                self.assertIsInstance(ref, dict)
                self.assertTrue((root / ref["file"]).exists(), ref)
            self.assertIsNone(dossier["provenance"]["model"])
            self.assertEqual(dossier["provenance"]["mode"], "static")

    def test_dossier_persistence_reload_and_source_immutability(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            root.mkdir()
            _fixture(root)
            store = StateManager(Path(td) / "ws", Path(td) / "logs")
            run = adoption.adopt_repository(root, store)
            st = run["state"]
            st.requirements = s.RequirementContract(
                objective="Add a feature",
                acceptance=[s.AcceptanceCriterion(id="ac1", text="tests pass")])
            before = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in root.rglob("*") if p.is_file()
                      and ".crew" not in p.parts and ".git" not in p.parts}
            result = discovery.discover_project(st, store)
            self.assertFalse(result["cached"])
            json_path = Path(result["metadata"]["dossier_path"])
            md_path = Path(result["metadata"]["markdown_path"])
            self.assertTrue(json_path.is_file())
            self.assertTrue(md_path.is_file())
            self.assertEqual(st.extensions["understanding"]["freshness"], "FRESH")
            loaded = s.ProjectState.from_dict(st.to_dict())
            self.assertTrue(discovery.load_dossier(loaded))
            summary = discovery.summary_for_agent(loaded, max_chars=500)
            self.assertLessEqual(len(summary), 500)
            self.assertIn("Freshness", summary)
            self.assertEqual(discovery.section(loaded, "technology_stack")
                             ["languages"]["Python"], 4)
            after = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in root.rglob("*") if p.is_file()
                     and ".crew" not in p.parts and ".git" not in p.parts}
            self.assertEqual(before, after)
            self.assertIn("requested_functionality",
                          json.loads(json_path.read_text(encoding="utf-8")))

    def test_freshness_detects_changed_and_new_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _fixture(root)
            store = StateManager(Path(td) / "ws", Path(td) / "logs")
            st = s.ProjectState(run_id="r", project_root=str(root), goal="g",
                                engine_kind=s.ENGINE_UNIFIED)
            discovery.discover_project(st, store)
            self.assertEqual(discovery.freshness(st)["status"], "FRESH")
            (root / "app" / "core.py").write_text("changed\n", encoding="utf-8")
            fresh = discovery.freshness(st)
            self.assertEqual(fresh["status"], "STALE")
            self.assertIn("app/core.py", fresh["changed"])
            (root / "new.py").write_text("x = 1\n", encoding="utf-8")
            fresh = discovery.freshness(st)
            self.assertIn("new.py", fresh["new"])

    def test_resource_limits_and_symlink_safety_are_explicit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            root.mkdir()
            _fixture(root)
            outside = Path(td) / "outside.py"
            outside.write_text("SECRET = 1\n", encoding="utf-8")
            try:
                (root / "external.py").symlink_to(outside)
            except OSError:
                self.skipTest("symlink not permitted")
            dossier = discovery.analyze(root, max_file_bytes=1)
            self.assertFalse(dossier["analysis_scope"]["hash_complete"])
            self.assertTrue(dossier["analysis_scope"]["limits"])
            self.assertTrue(any(item.get("reason") == "file_size_limit"
                                for item in dossier["inventory"]["hash_skipped"]))
            self.assertEqual(dossier["inventory"]["symlinks"][0]["path"],
                             "external.py")
            self.assertNotIn("external.py",
                             dossier["analysis_scope"]["file_hashes"])

    def test_conflicting_entrypoint_is_classified(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "package.json").write_text(
                json.dumps({"name": "x", "main": "missing.js"}),
                encoding="utf-8")
            dossier = discovery.analyze(root)
            self.assertTrue(any(c["status"] == "CONFLICTING"
                                for c in dossier["conflicts"]))
            self.assertGreater(dossier["epistemic"]["CONFLICTING"], 0)


class CliDiscoveryTests(unittest.TestCase):
    @unittest.skipUnless(MAIN_DEPS_OK, "pydantic/runtime deps missing")
    def test_cli_discover_and_understanding(self):
        repo = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            root.mkdir()
            _fixture(root)
            store = StateManager(Path(td) / "ws", Path(td) / "logs")
            adoption.adopt_repository(root, store)
            cmd = [sys.executable, "main.py", "--discover", "--project",
                   str(root)]
            proc = subprocess.run(cmd, cwd=str(repo), capture_output=True,
                                  text=True, timeout=40, env=env)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("COMPLETE", proc.stdout)
            summary = subprocess.run(
                [sys.executable, "main.py", "--understanding", "--project",
                 str(root)], cwd=str(repo), capture_output=True, text=True,
                timeout=30, env=env)
            self.assertEqual(summary.returncode, 0, summary.stderr)
            data = json.loads(summary.stdout)
            self.assertIn("summary", data)
            self.assertIn("freshness", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
