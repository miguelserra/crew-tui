#!/usr/bin/env python3
"""P2: Requirement -> Architecture -> Task traceability and approval hash."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine as e
import state as s


def build_state():
    st = s.ProjectState(run_id="r", goal="demo", phase="ARCHITECTURE",
                        engine_kind=s.ENGINE_UNIFIED, engine_version="1.0")
    st.requirements = s.RequirementContract(
        objective="demo",
        acceptance=[s.AcceptanceCriterion(id="ac1", text="the app runs")],
    )
    st.architecture = s.ArchitectureContract(
        approach="single file", modules=[{"path": "a.py"}],
        interfaces=[{"name": "main", "file": "a.py"}],
        test_plan=[{"task": "t1", "check": "python a.py"}],
    )
    st.tasks = [s.Task(id="t1", title="entry", description="print ok",
                       kind="build", target_file="a.py",
                       acceptance=["the app runs"])]
    s.link_traceability(st.requirements, st.tasks)
    return st


class TraceabilityTests(unittest.TestCase):
    def test_inference_links_criterion_to_task(self):
        st = build_state()
        self.assertEqual(st.requirements.acceptance[0].task_ids, ["t1"])
        self.assertEqual(st.tasks[0].requirement_ids, ["ac1"])

    def test_unmatched_criterion_gets_explicit_justification(self):
        st = build_state()
        st.requirements.acceptance.append(
            s.AcceptanceCriterion(id="ac2", text="special hardware"))
        s.link_traceability(st.requirements, st.tasks)
        ac2 = st.requirements.acceptance[1]
        self.assertEqual(ac2.task_ids, [])
        self.assertTrue(ac2.justification)

    def test_missing_task_reference_is_rejected(self):
        st = build_state()
        st.requirements.acceptance[0].task_ids = ["ghost"]
        with self.assertRaises(s.StateValidationError):
            s.validate_traceability(st.requirements, st.tasks)

    def test_criterion_without_task_or_justification_is_rejected(self):
        st = build_state()
        st.requirements.acceptance.append(
            s.AcceptanceCriterion(id="ac2", text="unmapped"))
        with self.assertRaises(s.StateValidationError):
            s.validate_traceability(st.requirements, st.tasks)

    def test_task_cannot_reference_unknown_criterion(self):
        st = build_state()
        st.tasks[0].requirement_ids = ["ghost"]
        with self.assertRaises(s.StateValidationError):
            s.validate_traceability(st.requirements, st.tasks)

    def test_dag_rejects_missing_dependencies_and_cycles(self):
        missing = [s.Task(id="t1", kind="build", target_file="a.py",
                          depends_on=["ghost"])]
        with self.assertRaises(s.StateValidationError):
            e._validate_dependencies(missing)
        cycle = [
            s.Task(id="t1", kind="build", target_file="a.py", depends_on=["t2"]),
            s.Task(id="t2", kind="build", target_file="b.py", depends_on=["t1"]),
        ]
        with self.assertRaises(s.StateValidationError):
            e._validate_dependencies(cycle)


class ApprovalHashTests(unittest.TestCase):
    def test_approval_is_deterministic(self):
        a = build_state()
        b = build_state()
        self.assertEqual(a.architecture_hash(), b.architecture_hash())

    def test_spec_approval_hashes_requirements(self):
        st = build_state()
        st.approvals.append(s.Approval(
            kind="spec", artifact_hash=st.requirements.content_hash(),
            decision="approved"))
        self.assertTrue(st.spec_approved())
        st.requirements.scope_out.append("new constraint")
        self.assertFalse(st.spec_approved())

    def test_progress_does_not_invalidate_approval(self):
        st = build_state()
        st.approve_architecture()
        st.tasks[0].status = "verified"
        st.tasks[0].attempts.append(s.TaskAttempt(
            attempt_id="a", model=__import__("registry").unknown_ref(),
            outcome="ok"))
        self.assertTrue(st.architecture_approved())

    def test_any_plan_change_invalidates_approval(self):
        mutations = [
            lambda st: st.architecture.modules.append({"path": "b.py"}),
            lambda st: st.architecture.interfaces[0].update({"file": "b.py"}),
            lambda st: st.architecture.test_plan.append({"task": "t1",
                                                         "check": "other"}),
            lambda st: setattr(st.tasks[0], "target_file", "b.py"),
            lambda st: st.tasks[0].acceptance.append("more"),
            lambda st: st.tasks[0].depends_on.append("t2"),
            lambda st: setattr(st.tasks[0], "role", "chief"),
            lambda st: setattr(st.tasks[0], "complexity", "high"),
            lambda st: (st.requirements.acceptance.append(
                s.AcceptanceCriterion(id="ac2", text="other", justification="x")),
                st.tasks[0].requirement_ids.append("ac2")),
            lambda st: setattr(st.requirements.acceptance[0],
                               "justification", "changed"),
            lambda st: setattr(st.requirements.acceptance[0], "text", "changed"),
            lambda st: st.tasks.append(s.Task(id="t2", kind="build",
                                              target_file="b.py")),
        ]
        for i, mutate in enumerate(mutations):
            with self.subTest(i=i):
                st = build_state()
                st.approve_architecture()
                self.assertTrue(st.architecture_approved())
                mutate(st)
                self.assertFalse(st.architecture_approved())

    def test_architectural_hash_covers_dependencies_and_files(self):
        a = build_state()
        b = build_state()
        b.tasks[0].target_file = "other.py"
        self.assertNotEqual(a.architecture_hash(), b.architecture_hash())
        b = build_state()
        b.tasks[0].depends_on = ["t2"]
        self.assertNotEqual(a.architecture_hash(), b.architecture_hash())


if __name__ == "__main__":
    unittest.main(verbosity=2)
