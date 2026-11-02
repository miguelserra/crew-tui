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


class DiscoveryCoreGapTests(unittest.TestCase):
    """P7.2c.1 core: structured, evidence-backed inventory gaps."""

    def _mini(self, root):
        (root / "pkg").mkdir()
        (root / "tests").mkdir()
        (root / "pkg" / "__init__.py").write_text("")
        (root / "pkg" / "core.py").write_text(
            "import requests\n"
            "class Api:\n    pass\n"
            "def run():\n    return 1\n"
            "def _private():\n    return 2\n", encoding="utf-8")
        (root / "tests" / "test_core.py").write_text(
            "def test_ok():\n    assert True\n", encoding="utf-8")
        (root / "requirements.txt").write_text(
            "requests==2.0\n# comment\n-e .\n", encoding="utf-8")
        (root / "README.md").write_text(
            "# Demo\n\nThis project demonstrates a static analyser.\n",
            encoding="utf-8")
        (root / "package.json").write_text(json.dumps({
            "name": "demo-app", "description": "A demo",
            "dependencies": {"react": "18.0.0"}}), encoding="utf-8")

    def test_project_name_provenance_and_description(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._mini(root)
            d = discovery.analyze(root)
            self.assertEqual(d["project"]["name"]["value"], "demo-app")
            self.assertEqual(d["project"]["name"]["source"], "package.json")
            desc = d["project"]["description"]
            self.assertEqual(desc["status"], "OBSERVED")
            self.assertIn("static analyser", desc["value"])
            ev = desc["evidence"][0]
            self.assertEqual(ev["file"], "README.md")
            self.assertEqual(ev["line"], 3)
            self.assertTrue((root / ev["file"]).is_file())

    def test_description_is_unknown_without_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "README.md").write_text("# Only a heading\n",
                                            encoding="utf-8")
            d = discovery.analyze(root)
            self.assertEqual(d["project"]["description"]["status"], "UNKNOWN")
            self.assertEqual(d["project"]["description"]["value"], "")

    def test_structure_and_interfaces_exclude_test_functions(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._mini(root)
            d = discovery.analyze(root)
            modules = {m["path"] for m in d["structure"]["modules"]}
            self.assertIn("pkg/core.py", modules)
            self.assertIn("tests/test_core.py", modules)
            classes = {c["name"] for c in d["structure"]["classes"]}
            functions = {f["name"] for f in d["structure"]["functions"]}
            interfaces = {i["name"] for i in d["structure"]["interfaces"]}
            self.assertIn("Api", classes)
            self.assertIn("run", functions)
            self.assertIn("run", interfaces)
            self.assertIn("Api", interfaces)
            self.assertNotIn("test_ok", interfaces)
            self.assertTrue(all(i["status"] == "INFERRED"
                                for i in d["structure"]["interfaces"]))
            self.assertTrue(all(c["extraction_method"] == "python-ast"
                                for c in d["structure"]["classes"]))

    def test_tests_section_and_execution_status(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._mini(root)
            (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            d = discovery.analyze(root)
            tests = d["tests"]
            self.assertIn("tests/test_core.py", tests["files"])
            self.assertIn("tests", tests["directories"])
            self.assertIn("pytest.ini",
                          {c["path"] for c in tests["configuration"]})
            self.assertEqual(tests["execution_status"], "NOT_RUN")

    def test_dependency_manifests_and_unresolved_formats(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._mini(root)
            (root / "package-lock.json").write_text("{}", encoding="utf-8")
            (root / "web").mkdir()
            (root / "web" / "package.json").write_text("{invalid",
                                                      encoding="utf-8")
            d = discovery.analyze(root)
            manifests = {m["path"]: m["status"]
                         for m in d["dependencies"]["manifests"]}
            self.assertEqual(manifests.get("requirements.txt"), "interpreted")
            self.assertEqual(manifests.get("package-lock.json"), "unresolved")
            self.assertEqual(manifests.get("web/package.json"), "unresolved")
            unresolved = {u["path"]: u["reason"]
                          for u in d["dependencies"]["unresolved_formats"]}
            self.assertIn("web/package.json", unresolved)
            self.assertIn("invalid JSON", unresolved["web/package.json"])
            self.assertIn("package-lock.json", unresolved)
            names = {dep["name"]
                     for dep in d["dependencies"]["declared_dependencies"]}
            self.assertIn("requests", names)
            self.assertNotIn("dependencies", names)

    def test_pyproject_dependencies_when_toml_available(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "pyproject.toml").write_text(
                "[project]\nname='x'\ndependencies=['pytest>=7','requests']\n",
                encoding="utf-8")
            d = discovery.analyze(root)
            unresolved = {u["path"] for u in d["dependencies"]["unresolved_formats"]}
            names = {dep["name"]
                     for dep in d["dependencies"]["declared_dependencies"]}
            if discovery._tomllib is None:
                self.assertIn("pyproject.toml", unresolved)
            else:
                self.assertNotIn("pyproject.toml", unresolved)
                self.assertIn("pytest", names)
                self.assertIn("requests", names)

    def test_pipfile_dependencies_when_toml_available(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "Pipfile").write_text(
                "[packages]\nrequests = '*'\n\n"
                "[dev-packages]\npytest = '*'\n", encoding="utf-8")
            d = discovery.analyze(root)
            unresolved = {u["path"] for u in d["dependencies"]["unresolved_formats"]}
            names = {dep["name"]
                     for dep in d["dependencies"]["declared_dependencies"]}
            if discovery._tomllib is None:
                self.assertIn("Pipfile", unresolved)
            else:
                self.assertNotIn("Pipfile", unresolved)
                self.assertIn("requests", names)
                self.assertIn("pytest", names)

    def test_framework_detection_uses_specific_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._mini(root)
            d = discovery.analyze(root)
            frameworks = {f["name"]: f
                          for f in d["technology_stack"]["frameworks"]}
            self.assertIn("React", frameworks)   # declared dependency
            for f in frameworks.values():
                self.assertTrue(f["evidence"], f)

    def test_evidence_coverage_and_items_reference_real_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._mini(root)
            d = discovery.analyze(root)
            cov = d["evidence"]["coverage"]
            self.assertEqual(cov["denominator"], "files_total_regular")
            self.assertGreater(cov["files_observed"], 0)
            self.assertGreaterEqual(cov["analysis_coverage"], 0.0)
            self.assertLessEqual(cov["analysis_coverage"], 1.0)
            items = d["evidence"]["items"]
            self.assertTrue(items)
            for item in items:
                for key in ("id", "classification", "subject", "claim",
                            "source_path", "line_start", "line_end",
                            "extraction_method", "confidence", "limitations"):
                    self.assertIn(key, item)
                self.assertIn(item["classification"],
                              {"OBSERVED", "INFERRED", "UNKNOWN", "CONFLICTING"})
                self.assertTrue(item["source_path"])
                self.assertTrue((root / item["source_path"]).is_file(), item)

    def test_analysis_is_deterministic_modulo_timestamp(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._mini(root)
            first = discovery.analyze(root, max_files=100)
            second = discovery.analyze(root, max_files=100)
            first.pop("generated_at")
            second.pop("generated_at")
            self.assertEqual(first, second)

    def test_parse_error_is_local(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "good.py").write_text("def ok():\n    return 1\n",
                                          encoding="utf-8")
            (root / "bad.py").write_text("def (:\n", encoding="utf-8")
            d = discovery.analyze(root)
            self.assertTrue(any("bad.py" in e
                                for e in d["analysis_scope"]["parse_errors"]))
            functions = {f["name"] for f in d["structure"]["functions"]}
            self.assertIn("ok", functions)

    def test_porcelain_dirty_path_parsing(self):
        self.assertEqual(discovery._porcelain_path(" M pkg/core.py"),
                         "pkg/core.py")
        self.assertEqual(discovery._porcelain_path("?? new.txt"), "new.txt")
        self.assertEqual(discovery._porcelain_path("R  old.py -> new.py"),
                         "new.py")

    def test_non_git_repository_reports_unknown_dirty_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.py").write_text("x = 1\n", encoding="utf-8")
            d = discovery.analyze(root)
            self.assertIsInstance(d["repository"]["git_available"], bool)
            self.assertIn(d["repository"]["dirty"], (None, False, True))
            self.assertIsInstance(d["repository"]["dirty_paths"], list)


class UnsupportedSourceClassificationTests(unittest.TestCase):
    """F-1: source languages without structural extraction are explicit."""

    @staticmethod
    def _write(root, rel, text="x\n"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_inventory_source_languages_without_extractor_are_reported(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for rel in ("a.go", "b.rs", "c.java", "d.rb", "e.php", "f.cs"):
                self._write(root, rel, "main\n")
            # supported extractors present in the same repo
            self._write(root, "app.py",
                        "class Api:\n    pass\n\ndef run():\n    return 1\n")
            self._write(root, "web.js", "import x from 'x';\n")
            d = discovery.analyze(root)
            unsupported = d["structure"]["unsupported_source_suffixes"]
            self.assertEqual(unsupported,
                             [".cs", ".go", ".java", ".php", ".rb", ".rs"])
            self.assertNotIn(".py", unsupported)
            self.assertNotIn(".js", unsupported)
            modules = {m["path"]: m for m in d["structure"]["modules"]}
            self.assertIn("app.py", modules)
            self.assertEqual(modules["app.py"]["extraction_method"], "python-ast")
            self.assertIn("web.js", modules)
            self.assertEqual(modules["web.js"]["extraction_method"],
                             "regex-imports")
            self.assertEqual({c["name"] for c in d["structure"]["classes"]},
                             {"Api"})
            self.assertEqual({f["name"] for f in d["structure"]["functions"]},
                             {"run"})

    def test_other_inventory_source_languages_are_reported(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for rel in ("a.c", "b.h", "c.cpp", "run.sh"):
                self._write(root, rel, "int main(void){return 0;}\n")
            d = discovery.analyze(root)
            self.assertEqual(d["structure"]["unsupported_source_suffixes"],
                             [".c", ".cpp", ".h", ".sh"])

    def test_non_source_text_and_config_are_not_reported(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(root, "README.md", "# title\n")
            self._write(root, "notes.txt", "hello\n")
            self._write(root, "data.json", "{}\n")
            self._write(root, "pyproject.toml", "[project]\nname='x'\n")
            self._write(root, "setup.cfg", "[metadata]\nname=x\n")
            self._write(root, "conf.yaml", "a: 1\n")
            self._write(root, "page.html", "<html></html>\n")
            self._write(root, "style.css", "body{}\n")
            self._write(root, "query.sql", "select 1;\n")
            self._write(root, ".gitignore", "*.pyc\n")
            d = discovery.analyze(root)
            self.assertEqual(d["structure"]["unsupported_source_suffixes"], [])

    def test_uppercase_extensions_are_normalised(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(root, "Main.GO", "package main\n")
            self._write(root, "Lib.RS", "fn main(){}\n")
            d = discovery.analyze(root)
            self.assertEqual(d["structure"]["unsupported_source_suffixes"],
                             [".go", ".rs"])

    def test_multiple_files_same_suffix_are_unique_and_sorted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for rel in ("z.go", "a.go", "m.rs", "b.rs"):
                self._write(root, rel, "x\n")
            d = discovery.analyze(root)
            self.assertEqual(d["structure"]["unsupported_source_suffixes"],
                             [".go", ".rs"])

    def test_classification_is_deterministic_and_json_serialisable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for rel in ("a.go", "b.rs", "app.py"):
                self._write(root, rel, "x = 1\n")
            first = discovery.analyze(root)
            second = discovery.analyze(root)
            self.assertEqual(first["structure"]["unsupported_source_suffixes"],
                             second["structure"]["unsupported_source_suffixes"])
            json.dumps(first)  # must remain JSON-serialisable
            self.assertTrue(first["structure"]["unsupported_source_suffixes"])

    def test_unsupported_list_only_contains_source_without_extraction(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for rel in ("a.go", "app.py", "web.js", "README.md", "data.json"):
                self._write(root, rel, "x\n")
            d = discovery.analyze(root)
            unsupported = set(d["structure"]["unsupported_source_suffixes"])
            self.assertTrue(unsupported.issubset(discovery.SOURCE_SUFFIXES))
            self.assertTrue(unsupported.isdisjoint(
                set(discovery.STRUCTURE_METHODS)))
            self.assertTrue(unsupported.isdisjoint(
                discovery.NON_SOURCE_SUFFIXES))


if __name__ == "__main__":
    unittest.main(verbosity=2)
