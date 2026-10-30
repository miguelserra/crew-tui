#!/usr/bin/env python3
"""P2: canonical contracts serialize losslessly and keep compatibility data."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import state as s


def sample_state():
    st = s.ProjectState(
        run_id="r1", project_root="/tmp/p", goal="demo",
        engine_kind=s.ENGINE_UNIFIED, engine_version="1.0", phase="BUILD",
        extensions={"custom_top": {"x": 1}},
    )
    st.requirements = s.RequirementContract(
        objective="demo",
        acceptance=[
            s.AcceptanceCriterion(id="ac1", text="runs"),
            s.AcceptanceCriterion(id="ac2", text="prints ok",
                                  justification="não aplicável neste perfil"),
        ],
        assumptions=["stdlib"],
    )
    st.architecture = s.ArchitectureContract(
        approach="single file", modules=[{"path": "a.py"}],
        interfaces=[{"name": "main", "file": "a.py"}],
        test_plan=[{"task": "t1", "check": "python a.py"}],
        raw={"approach": "single file", "tasks": [{"id": "t1"}]},
    )
    st.tasks = [
        s.Task(id="t1", title="run", description="main", kind="build",
               target_file="a.py", acceptance=["runs"], requirement_ids=["ac1"],
               legacy_extra={"old_field": "keep"}),
    ]
    st.tasks[0].attempts.append(s.TaskAttempt(
        attempt_id="a1", model=__import__("registry").ref_for(
            "opencode-go", "kimi", billing="subscription"), outcome="ok"))
    st.verifications["t1"] = s.VerificationRecord(
        verdict="PASS", criterion_ids=["ac1"], reason="ok")
    st.pause = s.PauseState(reason="test", phase="BUILD", since="now")
    return st


class CanonicalContractTests(unittest.TestCase):
    def test_round_trip_is_lossless(self):
        st = sample_state()
        st.approve_architecture()
        st.pause = s.PauseState(reason="test", phase="BUILD", since="now")
        data = st.to_dict()
        again = s.ProjectState.from_dict(data)
        self.assertEqual(again.schema_version, s.SCHEMA_VERSION)
        self.assertEqual(again.requirements.acceptance_texts(), ["runs", "prints ok"])
        self.assertEqual(again.tasks[0].requirement_ids, ["ac1"])
        self.assertEqual(again.tasks[0].legacy_extra, {"old_field": "keep"})
        self.assertEqual(again.extensions, {"custom_top": {"x": 1}})
        self.assertEqual(again.architecture.test_plan,
                         [{"task": "t1", "check": "python a.py"}])
        self.assertTrue(again.architecture_approved())
        self.assertEqual(again.verifications["t1"].criterion_ids, ["ac1"])
        self.assertEqual(again.tasks[0].attempts[0].outcome, "ok")
        self.assertEqual(again.pause.reason, "test")

    def test_verification_cannot_reference_unknown_criterion(self):
        st = sample_state()
        st.verifications["t1"].criterion_ids = ["ghost"]
        with self.assertRaises(s.StateValidationError):
            s.ProjectState.from_dict(st.to_dict())

    def test_legacy_requirement_shape_is_exported_explicitly(self):
        req = s.RequirementContract.from_dict(
            {"objective": "x", "acceptance": ["a", "b"]})
        legacy = req.to_legacy_dict()
        self.assertEqual(legacy["acceptance"], ["a", "b"])
        self.assertEqual(req.acceptance[0].id, "ac1")

    def test_architecture_approval_payload_excludes_canonical_tasks(self):
        arch = s.ArchitectureContract(approach="a", raw={
            "approach": "a", "tasks": [{"id": "t1"}]})
        self.assertNotIn("tasks", arch.approval_payload())

    def test_v3_schema_is_required(self):
        data = sample_state().to_dict()
        data["schema_version"] = 2
        with self.assertRaises(s.StateSchemaError):
            s.ProjectState.from_dict(data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
