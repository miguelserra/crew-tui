#!/usr/bin/env python3
"""P7.2c.2.1: retrieval reliability — freshness provenance, strict bounds,
validation, compatibility and regressions."""

import json
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

_DUMP = lambda obj: json.dumps(obj, ensure_ascii=False)  # noqa: E731


def _fixture(root: Path) -> None:
    (root / "pkg").mkdir()
    (root / "tests").mkdir()
    (root / "pkg" / "core.py").write_text(
        "class AuthService:\n    pass\n\n"
        "def login(user):\n    return user\n", encoding="utf-8")
    (root / "pkg" / "db.py").write_text("import sqlite3\n", encoding="utf-8")
    (root / "pkg" / "i18n.py").write_text(
        "class Caf\u00e9:\n    pass\n", encoding="utf-8")
    (root / "tests" / "test_core.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8")
    (root / "README.md").write_text(
        "# Demo\n\nAuthentication demo project.\n", encoding="utf-8")
    (root / "requirements.txt").write_text("requests==2.0\n", encoding="utf-8")


class _Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.td = Path(self._td.name)
        root = self.td / "repo"
        root.mkdir()
        _fixture(root)
        self.root = root
        self.store = StateManager(self.td / "ws", self.td / "logs")
        self.state = adoption.adopt_repository(root, self.store)["state"]
        discovery.discover_project(self.state, self.store)

    def tearDown(self):
        self._td.cleanup()

    def dossier_path(self) -> Path:
        return Path(self.state.extensions["understanding"]["dossier_path"])

    def meta(self) -> dict:
        return self.state.extensions["understanding"]


class SummaryTests(_Base):
    def test_summary_contains_last_recorded_freshness_and_is_bounded(self):
        text = discovery.summary_for_agent(self.state, max_chars=400)
        self.assertLessEqual(len(text), 400)
        self.assertTrue(text.startswith("Freshness"))
        self.assertIn("last recorded", text)

    def test_summary_includes_risks_by_default(self):
        text = discovery.summary_for_agent(self.state)
        self.assertTrue(any(line.startswith("Risks:") for line in text.splitlines()),
                        text)

    def test_summary_explicit_false_omits_risks(self):
        text = discovery.summary_for_agent(self.state, include_risks=False)
        self.assertFalse(any(line.startswith("Risks:") for line in text.splitlines()))

    def test_summary_explicit_true_includes_risks(self):
        text = discovery.summary_for_agent(self.state, include_risks=True)
        self.assertTrue(any(line.startswith("Risks:") for line in text.splitlines()))

    def test_structured_summary_full(self):
        data = discovery.get_project_summary(self.state, max_chars=4000)
        for key in ("project_name", "entry_points", "components", "freshness"):
            self.assertIn(key, data)
        self.assertIn("technology_stack", data)
        self.assertEqual(data["freshness"]["source"], "last_recorded")

    def test_structured_summary_strict_bound(self):
        data = discovery.get_project_summary(self.state, max_chars=300)
        self.assertLessEqual(len(_DUMP(data)), 300)
        if data.get("truncated"):
            self.assertIn("summary", data)

    def test_summary_without_dossier(self):
        bare = s.ProjectState(run_id="r", project_root=str(self.root),
                              engine_kind=s.ENGINE_UNIFIED)
        self.assertIn("No project understanding", discovery.summary_for_agent(bare))
        self.assertEqual(discovery.get_project_summary(bare)["error"],
                         "no project understanding dossier")


class SectionTests(_Base):
    def test_section_top_level_and_dotted(self):
        self.assertIn("languages",
                      discovery.section(self.state, "technology_stack"))
        classes = discovery.section(self.state, "structure.classes")
        self.assertTrue(any(c["name"] == "AuthService" for c in classes))
        self.assertTrue(discovery.section(self.state, "dependencies.manifests"))

    def test_section_unknown_returns_none(self):
        self.assertIsNone(discovery.section(self.state, "does_not_exist"))
        self.assertIsNone(discovery.section(self.state, "structure.nope"))
        self.assertIsNone(discovery.section(self.state, "a.b.c.d"))


class EvidenceAndLookupTests(_Base):
    def test_get_evidence_exact_and_missing(self):
        ids = [e["id"] for e in
               discovery.load_dossier(self.state)["evidence"]["items"]][:2]
        got = discovery.get_evidence(self.state, ids + ["ev-9999"])
        self.assertEqual([g["id"] for g in got], ids)
        for item in got:
            self.assertIn("source_path", item)
            self.assertIn("classification", item)

    def test_get_file_and_symbol_context(self):
        ctx = discovery.get_file_context(self.state, "pkg/core.py")
        self.assertIsNotNone(ctx)
        self.assertTrue(any(m["path"] == "pkg/core.py" for m in ctx["modules"]))
        self.assertTrue(any(sym["name"] == "AuthService" for sym in ctx["symbols"]))
        self.assertIsNone(discovery.get_file_context(self.state, "nope.py"))
        syms = discovery.get_symbol_context(self.state, "AuthService")
        self.assertTrue(all(x["file"] == "pkg/core.py" for x in syms))
        self.assertEqual(discovery.get_symbol_context(
            self.state, "AuthService", file_path="other.py"), [])

    def test_get_component_context(self):
        ctx = discovery.get_component_context(self.state, "pkg")
        self.assertTrue(ctx["found"])
        self.assertIn("content", ctx)
        self.assertFalse(discovery.get_component_context(
            self.state, "missing")["found"])

    def test_component_context_strict_bound(self):
        ctx = discovery.get_component_context(self.state, "pkg", max_chars=200)
        self.assertLessEqual(len(_DUMP(ctx)), 200)


class RetrievalBoundsTests(_Base):
    def test_complete_response_fits_budget_and_returns_results(self):
        res = discovery.retrieve_context(self.state, "AuthService",
                                         max_chars=4000)
        self.assertLessEqual(len(_DUMP(res)), 4000)
        self.assertTrue(res["results"])

    def test_first_oversized_result_does_not_break_budget(self):
        res = discovery.retrieve_context(self.state, "AuthService",
                                         max_chars=400,
                                         max_chars_per_result=8000)
        self.assertLessEqual(len(_DUMP(res)), 400)
        self.assertTrue(res["truncated"] or res["results"])

    def test_several_results_respect_budget(self):
        res = discovery.retrieve_context(self.state, "pkg",
                                         max_chars=1500,
                                         max_chars_per_result=8000,
                                         max_results=50)
        self.assertLessEqual(len(_DUMP(res)), 1500)
        self.assertTrue(res["truncated"])

    def test_per_result_bound_includes_metadata(self):
        res = discovery.retrieve_context(self.state, "AuthService",
                                         max_chars=50000,
                                         max_chars_per_result=400)
        self.assertTrue(res["results"])
        for entry in res["results"]:
            self.assertLessEqual(len(_DUMP(entry)), 400, entry)
            self.assertIn("evidence_ids", entry)
            self.assertIn("epistemic_status", entry)
            self.assertTrue(entry["match_reason"])
            self.assertIn("relevance_score", entry)
        self.assertTrue(any(e["truncated"] for e in res["results"]))

    def test_unicode_and_escaping_are_measured(self):
        res = discovery.retrieve_context(self.state, "Caf\u00e9",
                                         max_chars=1500,
                                         max_chars_per_result=8000)
        self.assertLessEqual(len(_DUMP(res)), 1500)
        json.loads(_DUMP(res))  # valid JSON after escaping
        self.assertTrue(res["results"])

    def test_tiny_per_result_omits_entries_but_respects_flag(self):
        res = discovery.retrieve_context(self.state, "AuthService",
                                         max_chars=50000,
                                         max_chars_per_result=120)
        self.assertLessEqual(len(_DUMP(res)), 50000)
        for entry in res["results"]:
            self.assertLessEqual(len(_DUMP(entry)), 120)
        self.assertTrue(res["truncated"])

    def test_min_chars_clamped_consistently_across_apis(self):
        comp = discovery.get_component_context(self.state, "pkg", max_chars=50)
        summ = discovery.get_project_summary(self.state, max_chars=50)
        self.assertLessEqual(len(_DUMP(comp)), 200)
        self.assertLessEqual(len(_DUMP(summ)), 200)
        comp2 = discovery.get_component_context(self.state, "pkg", max_chars=200)
        self.assertLessEqual(len(_DUMP(comp2)), 200)

    def test_tiny_budget_preserves_each_api_minimum_contract(self):
        comp = discovery.get_component_context(self.state, "pkg", max_chars=200)
        summary = discovery.get_project_summary(self.state, max_chars=200)
        self.assertLessEqual(len(_DUMP(comp)), 200)
        self.assertLessEqual(len(_DUMP(summary)), 200)
        # Component lookup prioritizes its identity and a preview; project
        # summary prioritizes a compact freshness assessment and summary preview.
        self.assertEqual(comp["component"], "pkg")
        self.assertTrue(comp["found"])
        self.assertTrue(comp["truncated"])
        self.assertIn("preview", comp)
        self.assertTrue(summary["truncated"])
        self.assertIn("freshness", summary)
        self.assertIn("summary", summary)

    def test_empty_query_with_symbols_returns_no_matches(self):
        res = discovery.retrieve_context(self.state, "", symbols=["AuthService"])
        self.assertEqual(res["results"], [])
        self.assertEqual(res["total_count"], 0)
        # Exact lookup is the documented alternative.
        self.assertTrue(discovery.get_symbol_context(self.state, "AuthService"))

    def test_symbols_are_optional_case_insensitive_name_filter(self):
        all_matches = discovery.retrieve_context(self.state, "AuthService")
        no_filter = discovery.retrieve_context(self.state, "AuthService",
                                               symbols=[])
        selected = discovery.retrieve_context(self.state, "AuthService",
                                              symbols=["authservice"])
        excluded = discovery.retrieve_context(self.state, "AuthService",
                                              symbols=["login"])
        self.assertEqual(no_filter["results"], all_matches["results"])
        self.assertTrue(selected["results"])
        self.assertTrue(all("AuthService" in _DUMP(row)
                            for row in selected["results"]))
        self.assertEqual(excluded["results"], [])

    def test_task_context_is_echoed_but_does_not_change_lexical_ranking(self):
        plain = discovery.retrieve_context(self.state, "AuthService")
        with_context = discovery.retrieve_context(
            self.state, "AuthService", task_context="database migration")
        self.assertEqual(with_context["task_context"], "database migration")
        self.assertEqual(with_context["results"], plain["results"])
        self.assertEqual(with_context["total_count"], plain["total_count"])

    def test_max_chars_200_is_never_exceeded(self):
        res = discovery.retrieve_context(self.state, "AuthService",
                                         max_chars=200)
        self.assertLessEqual(len(_DUMP(res)), 200)

    def test_total_count_is_pre_cap(self):
        res = discovery.retrieve_context(self.state, "pkg", max_results=1,
                                         max_chars=50000)
        self.assertGreaterEqual(res["total_count"], len(res["results"]))
        self.assertGreater(res["total_count"], 1 if res["results"] else 0)

    def test_evidence_and_status_preserved_under_truncation(self):
        res = discovery.retrieve_context(self.state, "pkg",
                                         max_chars=1500,
                                         max_chars_per_result=400,
                                         max_results=50)
        self.assertTrue(res["results"])
        self.assertTrue(res["truncated"])
        for entry in res["results"]:
            self.assertIsInstance(entry["evidence_ids"], list)
            self.assertIn(entry["epistemic_status"],
                          {"OBSERVED", "INFERRED", "UNKNOWN", "CONFLICTING"})
        union = []
        for entry in res["results"]:
            for eid in entry["evidence_ids"]:
                if eid not in union:
                    union.append(eid)
        self.assertEqual(res["evidence_ids"], union)

    def test_invalid_limits_raise_type_error(self):
        bad = [{"max_results": 1.5}, {"max_results": "10"},
               {"max_results": True}, {"max_results": None},
               {"max_chars": 1.5}, {"max_chars": "200"},
               {"max_chars": False}, {"max_chars": None},
               {"max_chars_per_result": 1.5},
               {"max_chars_per_result": "100"},
               {"max_chars_per_result": True},
               {"max_chars_per_result": None}]
        for kwargs in bad:
            with self.assertRaises(TypeError, msg=kwargs):
                discovery.retrieve_context(self.state, "AuthService", **kwargs)

    def test_valid_limits_and_clamping(self):
        res = discovery.retrieve_context(self.state, "AuthService",
                                         max_results=999, max_chars=999999,
                                         max_chars_per_result=999999)
        self.assertEqual(res["max_results"], 50)
        self.assertEqual(res["max_chars"], 50000)
        self.assertEqual(res["max_chars_per_result"], 8000)
        res0 = discovery.retrieve_context(self.state, "AuthService",
                                          max_results=0, max_chars=1,
                                          max_chars_per_result=1)
        self.assertEqual(res0["max_results"], 1)
        self.assertEqual(res0["max_chars"], 200)
        self.assertEqual(res0["max_chars_per_result"], 100)

    def test_limits_consistent_across_apis(self):
        with self.assertRaises(TypeError):
            discovery.get_project_summary(self.state, max_chars="4000")
        with self.assertRaises(TypeError):
            discovery.get_component_context(self.state, "pkg", max_chars=1.5)
        with self.assertRaises(TypeError):
            discovery.get_component_context(self.state, "pkg", max_chars=True)


class FreshnessProvenanceTests(_Base):
    def test_generation_records_initial_assessment(self):
        last = self.meta()["last_freshness"]
        self.assertEqual(last["status"], "FRESH")
        self.assertTrue(last["checked_at"])
        self.assertEqual(last["source"], "generation")

    def test_generation_origin_assessment_source_preserved(self):
        res = discovery.retrieve_context(self.state, "AuthService")
        fresh = res["freshness"]
        self.assertEqual(fresh["status"], "FRESH")
        self.assertEqual(fresh["assessment_source"], "generation")

    def test_live_assessment_origin_stays_fresh_but_not_live(self):
        discovery.freshness(self.state)  # real live assessment
        res = discovery.retrieve_context(self.state, "AuthService")
        fresh = res["freshness"]
        self.assertEqual(fresh["status"], "FRESH")          # not downgraded
        self.assertEqual(fresh["assessment_source"], "live_assessment")
        self.assertEqual(fresh["source"], "last_recorded")  # retrieval provenance
        self.assertIs(fresh["live"], False)                 # not a live check now

    def test_persistence_boundary_retrieval(self):
        discovery.freshness(self.state)
        self.store.save_project(self.state)
        path = self.store.project_state_path(self.root)
        dossier_before = self.dossier_path().read_bytes()
        # Discard in-memory state; use only the reloaded representation.
        reloaded = self.store.load_project(path)
        last = reloaded.extensions["understanding"]["last_freshness"]
        generated = reloaded.extensions["understanding"]["generated_at"]
        self.assertTrue(last["checked_at"])
        res = discovery.retrieve_context(reloaded, "AuthService")
        fresh = res["freshness"]
        self.assertEqual(fresh["status"], last["status"])
        self.assertEqual(fresh["checked_at"], last["checked_at"])
        self.assertEqual(fresh["generated_at"], generated)
        self.assertEqual(fresh["assessment_source"], "live_assessment")
        self.assertEqual(fresh["source"], "last_recorded")
        self.assertIs(fresh["live"], False)
        self.assertEqual(self.dossier_path().read_bytes(), dossier_before)

    def test_freshness_provenance_survives_fresh_process_reload(self):
        (self.root / "pkg" / "core.py").write_text(
            "class AuthService:\n    changed = True\n", encoding="utf-8")
        result = discovery.freshness(self.state, self.root)
        self.assertEqual(result["status"], "STALE")
        state_path = self.store.save_project(self.state)
        script = r'''import json, sys
from pathlib import Path
from store import StateManager
import discovery
state = StateManager().load_project(Path(sys.argv[1]))
result = discovery.retrieve_context(state, "AuthService")
print(json.dumps({
    "stored": state.extensions["understanding"]["last_freshness"],
    "generated_at": state.extensions["understanding"]["generated_at"],
    "retrieved": result["freshness"],
}, ensure_ascii=False))'''
        completed = subprocess.run(
            [sys.executable, "-c", script, str(state_path)],
            cwd=Path(__file__).resolve().parents[1],
            check=True, capture_output=True, text=True, timeout=30)
        reloaded = json.loads(completed.stdout)
        stored = reloaded["stored"]
        fresh = reloaded["retrieved"]
        self.assertEqual(stored["status"], "STALE")
        self.assertIn("pkg/core.py", stored["changed"])
        self.assertEqual(stored["source"], "live_assessment")
        self.assertEqual(fresh["status"], "STALE")
        self.assertEqual(fresh["checked_at"], stored["checked_at"])
        self.assertEqual(fresh["assessment_source"], "live_assessment")
        self.assertEqual(fresh["generated_at"], reloaded["generated_at"])

    def test_retrieval_reports_last_recorded_not_live(self):
        res = discovery.retrieve_context(self.state, "AuthService")
        fresh = res["freshness"]
        self.assertEqual(fresh["source"], "last_recorded")
        self.assertIs(fresh["live"], False)
        self.assertTrue(fresh["checked_at"])
        self.assertTrue(fresh["generated_at"])
        self.assertEqual(fresh["status"], "FRESH")

    def test_retrieval_does_not_change_checked_at(self):
        before = self.meta()["last_freshness"]["checked_at"]
        discovery.retrieve_context(self.state, "AuthService")
        discovery.retrieve_context(self.state, "pkg")
        discovery.summary_for_agent(self.state)
        self.assertEqual(self.meta()["last_freshness"]["checked_at"], before)

    def test_explicit_freshness_records_checked_at_and_persists(self):
        result = discovery.freshness(self.state)
        self.assertTrue(result.get("checked_at"))
        self.assertEqual(self.meta()["last_freshness"]["checked_at"],
                         result["checked_at"])
        self.store.save_project(self.state)
        reloaded = self.store.load_project(
            self.store.project_state_path(self.root))
        last = reloaded.extensions["understanding"]["last_freshness"]
        self.assertEqual(last["checked_at"], result["checked_at"])

    def test_repo_change_does_not_become_live_fresh(self):
        before = self.meta()["last_freshness"]["checked_at"]
        (self.root / "pkg" / "newfile.py").write_text("x = 1\n", encoding="utf-8")
        res = discovery.retrieve_context(self.state, "AuthService")
        self.assertIs(res["freshness"]["live"], False)
        self.assertEqual(res["freshness"]["source"], "last_recorded")
        self.assertEqual(self.meta()["last_freshness"]["checked_at"], before)

    def test_last_recorded_stale_is_preserved(self):
        self.meta()["last_freshness"] = {
            "status": "STALE", "reason": "changed",
            "checked_at": "2026-11-02T10:00:00Z"}
        res = discovery.retrieve_context(self.state, "AuthService")
        self.assertEqual(res["freshness"]["status"], "STALE")
        self.assertTrue(res["freshness"]["stale"])

    def test_missing_metadata_is_unknown(self):
        self.meta().pop("last_freshness", None)
        res = discovery.retrieve_context(self.state, "AuthService")
        self.assertEqual(res["freshness"]["status"], "UNKNOWN")
        self.assertIs(res["freshness"]["live"], False)

    def test_malformed_metadata_is_unknown(self):
        for bad in (None, "boom", ["x"], {}, {"status": "WAT",
                                              "checked_at": "t"}):
            self.meta()["last_freshness"] = bad
            res = discovery.retrieve_context(self.state, "AuthService")
            self.assertEqual(res["freshness"]["status"], "UNKNOWN", bad)

    def test_fresh_without_checked_at_is_unknown(self):
        self.meta()["last_freshness"] = {"status": "FRESH"}
        res = discovery.retrieve_context(self.state, "AuthService")
        self.assertEqual(res["freshness"]["status"], "UNKNOWN")

    def test_incomplete_generation_coverage_is_unknown(self):
        td2 = tempfile.TemporaryDirectory()
        try:
            root = Path(td2.name) / "repo"
            root.mkdir()
            _fixture(root)
            store = StateManager(Path(td2.name) / "ws", Path(td2.name) / "logs")
            state = adoption.adopt_repository(root, store)["state"]
            # Force incomplete hash coverage during generation.
            discovery.discover_project(state, store, max_file_bytes=1)
            last = state.extensions["understanding"]["last_freshness"]
            self.assertEqual(last["status"], "UNKNOWN")
            res = discovery.retrieve_context(state, "AuthService")
            self.assertEqual(res["freshness"]["status"], "UNKNOWN")
        finally:
            td2.cleanup()

    def test_missing_dossier_is_unknown_even_if_metadata_fresh(self):
        self.dossier_path().unlink()
        res = discovery.retrieve_context(self.state, "AuthService")
        self.assertEqual(res["freshness"]["status"], "UNKNOWN")
        self.assertIs(res["freshness"]["live"], False)

    def test_invalid_dossier_is_unknown(self):
        self.dossier_path().write_text("{not json", encoding="utf-8")
        res = discovery.retrieve_context(self.state, "AuthService")
        self.assertEqual(res["freshness"]["status"], "UNKNOWN")


class SafetyTests(_Base):
    def test_retrieval_does_not_rescan_repository(self):
        original = adoption.inspect_repository
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            raise AssertionError("retrieval must not inspect the repository")

        adoption.inspect_repository = boom
        try:
            res = discovery.retrieve_context(self.state, "AuthService")
        finally:
            adoption.inspect_repository = original
        self.assertEqual(calls["n"], 0)
        self.assertTrue(res["results"])

    def test_retrieval_does_not_mutate_dossier_or_state(self):
        before_bytes = self.dossier_path().read_bytes()
        before_ext = json.loads(_DUMP(self.state.extensions))
        discovery.retrieve_context(self.state, "AuthService")
        discovery.retrieve_context(self.state, "pkg")
        discovery.summary_for_agent(self.state)
        discovery.get_project_summary(self.state)
        self.assertEqual(self.dossier_path().read_bytes(), before_bytes)
        self.assertEqual(json.loads(_DUMP(self.state.extensions)), before_ext)

    def test_no_model_or_network(self):
        res = discovery.retrieve_context(self.state, "AuthService")
        self.assertEqual(res["retrieval"], "lexical")
        self.assertIsNone(res["model"])

    def test_legacy_minimal_dossier_is_tolerated(self):
        legacy = {
            "schema": "p7-2c/1",
            "overview": {"project_name": "legacy"},
            "technology_stack": {"languages": {"Python": 1}, "frameworks": []},
            "entry_points": [], "components": [],
            "analysis_scope": {"files_analyzed": 1, "files_total": 1},
            "repository": {"head": "abc", "branch": "main"},
        }
        path = self.td / "legacy_dossier.json"
        path.write_text(_DUMP(legacy), encoding="utf-8")
        state = s.ProjectState(run_id="r", project_root=str(self.root),
                               engine_kind=s.ENGINE_UNIFIED)
        state.extensions["understanding"] = {"dossier_path": str(path)}
        res = discovery.retrieve_context(state, "legacy")
        self.assertIn("results", res)
        self.assertIn("Freshness", discovery.summary_for_agent(state))
        self.assertIsNone(discovery.section(state, "structure"))
        self.assertEqual(discovery.get_evidence(state, ["ev-0001"]), [])
        self.assertEqual(res["freshness"]["status"], "UNKNOWN")


class RankingTests(_Base):
    def test_exact_symbol_ranks_first(self):
        res = discovery.retrieve_context(self.state, "AuthService")
        top = res["results"][0]
        self.assertEqual(top["content"].get("name"), "AuthService")
        self.assertIn("exact_symbol", top["match_reason"])
        self.assertGreaterEqual(top["relevance_score"], 1.0)

    def test_result_ordering_is_deterministic(self):
        a = discovery.retrieve_context(self.state, "pkg", max_chars=50000,
                                       max_results=50)
        b = discovery.retrieve_context(self.state, "pkg", max_chars=50000,
                                       max_results=50)
        key = lambda r: (r["section"], r["path"], r["relevance_score"])  # noqa: E731
        self.assertEqual([key(r) for r in a["results"]],
                         [key(r) for r in b["results"]])

    def test_sorted_by_score_then_section_then_path(self):
        res = discovery.retrieve_context(self.state, "pkg", max_chars=50000,
                                         max_results=50)
        rows = [(-r["relevance_score"], r["section"], r["path"])
                for r in res["results"]]
        self.assertEqual(rows, sorted(rows))

    def test_unknown_query_does_not_broaden(self):
        res = discovery.retrieve_context(self.state, "zzzz-not-present-zzzz")
        self.assertEqual(res["results"], [])
        self.assertEqual(res["total_count"], 0)


class EngineIntegrationTests(_Base):
    def test_workflow_engine_delegates_equivalently(self):
        from engine import WorkflowEngine
        eng = WorkflowEngine(self.state, self.store, None)
        a = eng.retrieve_context("AuthService", max_chars=4000)
        b = discovery.retrieve_context(self.state, "AuthService", max_chars=4000)
        self.assertEqual(_DUMP(a), _DUMP(b))
        self.assertEqual(eng.project_summary(max_chars=300),
                         discovery.summary_for_agent(self.state, max_chars=300))
        self.assertIn("project_name", eng.project_summary_structured())
        self.assertTrue(eng.component_context("pkg")["found"])
        self.assertIsInstance(eng.evidence(a["evidence_ids"]), list)


if __name__ == "__main__":
    unittest.main(verbosity=2)
