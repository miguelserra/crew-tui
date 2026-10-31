#!/usr/bin/env python3
"""P7.2a: Project Brief, adaptive interview, provenance and approval."""

import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import brief
import engine
import state as s
from store import StateManager

try:
    import pydantic  # noqa: F401
except Exception:
    MAIN_DEPS_OK = False
else:
    MAIN_DEPS_OK = True


MINIMAL = {
    "project_name": "Brief Demo",
    "project_description": "Build a small visual page",
    "main_objective": "Show a deterministic hello page",
}


class BriefTests(unittest.TestCase):
    def _engine(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        st = s.ProjectState(run_id="r", project_root=str(root), goal="",
                            engine_kind=s.ENGINE_UNIFIED, phase="SPEC",
                            profile="auto")
        store = StateManager(root / "ws", root / "logs")
        adapter = types.SimpleNamespace()
        eng = engine.WorkflowEngine(st, store, adapter, emit=lambda *a: None)
        return root, st, eng

    def _minimal(self):
        root, st, eng = self._engine()
        eng.start_brief(MINIMAL)
        return root, st, eng

    def _resolve_blocking(self, eng):
        for q in list(eng.brief_questions()):
            if q["status"] != "OPEN" or q["priority"] != "BLOCKING":
                continue
            if q["question_id"] == "q_core_workflows":
                eng.answer_brief_question(q["question_id"],
                                          "Create and view a page", revise=True)
            elif q["question_id"] == "q_acceptance":
                eng.answer_brief_question(q["question_id"],
                                          "The page renders without errors",
                                          revise=True)
            else:
                eng.answer_brief_question(q["question_id"], "Resolved",
                                          revise=True)

    def test_p72a_01_minimal_brief_and_optional_defer(self):
        root, st, eng = self._minimal()
        self.assertEqual(brief.brief_of(st).get("project_name"), "Brief Demo")
        open_ids = {q["question_id"] for q in eng.brief_questions()
                    if q["status"] == "OPEN"}
        self.assertIn("q_application_type", open_ids)
        eng.answer_brief_question("q_application_type", "Not decided",
                                  defer=True)
        q = next(x for x in eng.brief_questions()
                 if x["question_id"] == "q_application_type")
        self.assertEqual(q["status"], "DEFERRED")
        self.assertIn("application_type", brief.brief_of(st).undecided)
        self.assertTrue(eng.brief_blockers())
        self.tmp.cleanup()

    def test_p72a_02_partial_brief_survives_reload(self):
        root, st, eng = self._minimal()
        eng.update_brief({"intended_users": "Operators"})
        loaded = st.__class__.from_dict(st.to_dict())
        self.assertEqual(brief.brief_of(loaded).get("intended_users"),
                         "Operators")
        self.assertTrue(brief.has_brief(loaded))
        self.tmp.cleanup()

    def test_p72a_03_questions_are_adaptive(self):
        root, st, eng = self._minimal()
        ids = {q["question_id"] for q in eng.brief_questions()}
        self.assertIn("q_core_workflows", ids)
        self.assertIn("q_acceptance", ids)
        self.assertIn("q_application_type", ids)
        self.assertIn("q_preferred_language", ids)
        self.assertNotIn("q_data_storage", ids)
        self.tmp.cleanup()

    def test_p72a_04_answered_questions_are_not_repeated(self):
        root, st, eng = self._minimal()
        eng.answer_brief_question("q_core_workflows", "Create a page",
                                  revise=True)
        q = next(x for x in eng.brief_questions()
                 if x["question_id"] == "q_core_workflows")
        self.assertEqual(q["status"], "ANSWERED")
        self.assertNotIn("q_core_workflows",
                         [x.question_id for x in
                          brief.interview_of(st).open_questions()])
        self.tmp.cleanup()

    def test_p72a_05_blocking_ambiguity_blocks_architecture(self):
        root, st, eng = self._minimal()
        self.assertEqual(eng.step(), "paused")
        self.assertEqual(st.pause.reason, "spec_questions")
        self.assertFalse(eng.approve_spec())
        self.assertEqual(st.phase, "SPEC")
        self.tmp.cleanup()

    def test_p72a_06_optional_ambiguity_can_be_deferred(self):
        root, st, eng = self._minimal()
        self._resolve_blocking(eng)
        eng.answer_brief_question("q_application_type", "Not decided",
                                  defer=True)
        self.assertFalse(any("q_application_type" in b
                             for b in eng.brief_blockers()))
        self.assertTrue(eng.approve_spec())
        self.assertEqual(st.phase, "ARCHITECTURE")
        self.tmp.cleanup()

    def test_p72a_07_requirement_provenance_is_traceable(self):
        root, st, eng = self._minimal()
        self._resolve_blocking(eng)
        items = st.extensions["p7_2a"]["requirements"]
        by_id = {x["requirement_id"]: x for x in items}
        self.assertIn("req_project_name", by_id)
        self.assertTrue(by_id["req_project_name"]["source_brief_field"]
                        == "project_name")
        self.assertTrue(by_id["req_q_core_workflows"]["source_question_id"]
                        == "q_core_workflows")
        self.assertTrue(by_id["req_q_core_workflows"]["user_confirmed"])
        self.assertEqual(st.requirements.acceptance[0].text,
                         "The page renders without errors")
        self.assertIn("p7_2a", st.requirements.raw)
        self.tmp.cleanup()

    def test_p72a_08_revision_invalidates_approval(self):
        root, st, eng = self._minimal()
        self._resolve_blocking(eng)
        self.assertTrue(eng.approve_spec())
        self.assertTrue(st.spec_approved())
        old_hash = st.requirements.content_hash()
        eng.answer_brief_question("q_core_workflows", "Create and edit a page",
                                  revise=True)
        self.assertNotEqual(st.requirements.content_hash(), old_hash)
        self.assertFalse(st.spec_approved())
        self.assertTrue(any(a.kind == "spec" for a in st.approvals))
        self.tmp.cleanup()

    def test_p72a_09_human_approval_is_required(self):
        root, st, eng = self._minimal()
        self._resolve_blocking(eng)
        self.assertEqual(eng.step(), "paused")
        self.assertEqual(st.pause.reason, "spec_approval")
        self.assertFalse(st.spec_approved())
        self.assertTrue(eng.approve_spec())
        self.assertTrue(st.spec_approved())
        self.tmp.cleanup()

    def test_p72a_10_crash_resume_preserves_interview(self):
        root, st, eng = self._minimal()
        eng.answer_brief_question("q_core_workflows", "Create a page",
                                  revise=True)
        eng.save()
        loaded = st.__class__.from_dict(st.to_dict())
        q = next(x for x in brief.interview_of(loaded).questions
                 if x.question_id == "q_core_workflows")
        self.assertEqual(q.status, "ANSWERED")
        self.assertEqual(q.answer, "Create a page")
        self.assertIn("q_core_workflows",
                      [x.question_id for x in
                       brief.interview_of(loaded).questions])
        self.tmp.cleanup()

    def test_p72a_11_legacy_state_has_no_brief(self):
        st = s.ProjectState(run_id="r", goal="legacy",
                            engine_kind=s.ENGINE_UNIFIED)
        self.assertFalse(brief.has_brief(st))
        self.assertEqual(brief.blockers(st), [])
        raw = st.to_dict()
        raw.pop("extensions", None)
        loaded = s.ProjectState.from_dict(raw)
        self.assertFalse(brief.has_brief(loaded))
        self.assertEqual(loaded.requirements.objective, "")

    @unittest.skipUnless(MAIN_DEPS_OK, "pydantic/runtime deps missing")
    def test_p72a_12_cli_exposes_brief_and_interview_flags(self):
        root = Path(__file__).resolve().parents[1]
        proc = subprocess.run([sys.executable, "main.py", "--help"],
                              cwd=str(root), capture_output=True, text=True,
                              timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for flag in ("--brief", "--answer", "--defer", "--approve-spec",
                     "--show-brief"):
            self.assertIn(flag, proc.stdout)

    @unittest.skipUnless(MAIN_DEPS_OK, "pydantic/runtime deps missing")
    def test_p72a_12b_cli_brief_reports_blockers_without_model(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as td:
            brief_path = Path(td) / "brief.json"
            brief_path.write_text(__import__("json").dumps(MINIMAL),
                                  encoding="utf-8")
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            proc = subprocess.run(
                [sys.executable, "main.py", "--project",
                 str(Path(td) / "project"), "--brief", str(brief_path)],
                cwd=str(root), input="", capture_output=True, text=True,
                timeout=40, env=env)
            self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
            self.assertIn("BLOCKING", proc.stdout)
            self.assertIn("q_core_workflows", proc.stdout)

    @unittest.skipUnless(MAIN_DEPS_OK, "pydantic/runtime deps missing")
    def test_p72a_12c_cli_answers_and_defer_clear_blockers(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as td:
            brief_path = Path(td) / "brief.json"
            brief_path.write_text(__import__("json").dumps(MINIMAL),
                                  encoding="utf-8")
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            proc = subprocess.run(
                [sys.executable, "main.py", "--project",
                 str(Path(td) / "project"), "--brief", str(brief_path),
                 "--answer", "q_core_workflows=Create a page",
                 "--answer", "q_acceptance=The page renders",
                 "--defer", "q_application_type",
                 "--defer", "q_preferred_language"],
                cwd=str(root), input="", capture_output=True, text=True,
                timeout=40, env=env)
            self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
            self.assertNotIn("BLOCKED: decisão bloqueante", proc.stdout)
            self.assertIn("--approve-spec", proc.stdout)

    def test_p72a_13_suggestions_are_not_requirements(self):
        root, st, eng = self._minimal()
        before = {x["requirement_id"] for x in
                  st.extensions["p7_2a"].get("requirements", [])}
        self.assertNotIn("req_application_type", before)
        self.assertNotIn("req_preferred_language", before)
        self.assertFalse(st.requirements.acceptance)
        self.tmp.cleanup()

    def test_p72a_14_conflicting_answer_requires_clarification(self):
        root, st, eng = self._minimal()
        eng.answer_brief_question("q_application_type", "CLI", revise=True)
        result = eng.answer_brief_question("q_application_type", "Web application")
        self.assertIn("conflict", result)
        self.assertEqual(brief.brief_of(st).get("application_type"), "CLI")
        self.assertTrue(any(q["question_id"].startswith("q_conflict_")
                            and q["status"] == "OPEN"
                            for q in eng.brief_questions()))
        eng.answer_brief_question("q_application_type", "Web application",
                                  revise=True)
        self.assertEqual(brief.brief_of(st).get("application_type"),
                         "Web application")
        self.tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
