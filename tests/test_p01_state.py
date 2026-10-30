#!/usr/bin/env python3
"""P0/P1: canonical state, task validation and legacy format migration."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import state as s
from registry import ModelRef, ref_for

FIX = Path(__file__).resolve().parent / "fixtures" / "p01"


class TaskValidationTests(unittest.TestCase):
    def test_build_requires_relative_target(self):
        with self.assertRaises(s.StateValidationError):
            s.validate_tasks([s.Task(id="t1", kind="build", target_file="")])
        with self.assertRaises(s.StateValidationError):
            s.validate_tasks([s.Task(id="t1", kind="build", target_file="/etc/x")])
        with self.assertRaises(s.StateValidationError):
            s.validate_tasks([s.Task(id="t1", kind="build", target_file="../x")])
        with self.assertRaises(s.StateValidationError):
            s.validate_tasks([s.Task(id="t1", kind="build", target_file="C:/x")])

    def test_build_targets_unique_case_insensitive(self):
        with self.assertRaises(s.StateValidationError):
            s.validate_tasks([
                s.Task(id="t1", kind="build", target_file="src/Main.py"),
                s.Task(id="t2", kind="build", target_file="src/main.py"),
            ])

    def test_review_and_prose_may_omit_file(self):
        tasks = s.validate_tasks([
            s.Task(id="r1", kind="review", target_file=""),
            s.Task(id="p1", kind="prose", target_file=""),
        ])
        self.assertEqual([t.target_file for t in tasks], ["", ""])

    def test_separators_are_normalized(self):
        task = s.validate_tasks([s.Task(id="t1", kind="build", target_file="src\\a\\b.py")])[0]
        self.assertEqual(task.target_file, "src/a/b.py")

    def test_symlink_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "proj"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            try:
                (root / "link").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("symlink not permitted on this platform")
            with self.assertRaises(s.StateValidationError):
                s.validate_tasks([s.Task(id="t1", kind="build",
                                         target_file="link/x.py")],
                                 project_root=root)
            # a normal relative path still passes
            s.validate_tasks([s.Task(id="t2", kind="build",
                                     target_file="src/x.py")], project_root=root)


class AttemptTests(unittest.TestCase):
    def test_attempt_round_trip_keeps_effective_model(self):
        ref = ref_for("opencode-go", "kimi-k3.1-pro", billing="subscription")
        attempt = s.TaskAttempt(attempt_id="a1", model=ref, started_at="t0",
                                completed_at="t1", outcome="ok")
        again = s.TaskAttempt.from_dict(attempt.to_dict())
        self.assertEqual(again.model, ref)
        self.assertEqual(again.outcome, "ok")

    def test_with_attempt_sets_delivered_by_on_success(self):
        task = s.Task(id="t1", kind="build", target_file="a.py")
        failed = s.TaskAttempt(attempt_id="a1", model=ref_for("gemini-plus", "gemini-flash"),
                               outcome="429/quota")
        ok = s.TaskAttempt(attempt_id="a2", model=ref_for("opencode-go", "kimi"),
                           outcome="ok")
        s.with_attempt(task, failed)
        self.assertIsNone(task.delivered_by)
        s.with_attempt(task, ok)
        self.assertEqual(task.delivered_by.model_id, "kimi")


class ApprovalTests(unittest.TestCase):
    def _state(self):
        return s.ProjectState(run_id="r1", goal="g", engine_kind=s.ENGINE_UNIFIED,
                              engine_version="1.0")

    def test_architecture_hash_invalidates_approval(self):
        st = self._state()
        st.architecture = s.ArchitectureContract(
            approach="a", modules=[{"path": "a.py"}],
            interfaces=[{"name": "f", "file": "a.py"}])
        st.approve_architecture()
        self.assertTrue(st.architecture_approved())
        st.architecture.modules.append({"path": "b.py"})
        self.assertFalse(st.architecture_approved())
        self.assertTrue(st.tighten_architecture_gate())

    def test_unknown_billing_approval_is_per_ref(self):
        st = self._state()
        ref = ModelRef("x", "y", "z", billing="unknown")
        self.assertFalse(st.unknown_billing_approved(ref))
        st.approve_unknown_billing(ref)
        self.assertTrue(st.unknown_billing_approved(ref))
        self.assertFalse(st.unknown_billing_approved(
            ModelRef("x", "y", "other", billing="unknown")))


class ProjectStateTests(unittest.TestCase):
    def test_round_trip_keeps_engine_and_schema(self):
        st = s.ProjectState(run_id="r1", goal="g", engine_kind=s.ENGINE_LEGACY,
                            engine_version="legacy-1")
        data = st.to_dict()
        self.assertEqual(data["engine_kind"], "legacy")
        self.assertEqual(data["schema_version"], s.SCHEMA_VERSION)
        again = s.ProjectState.from_dict(data)
        self.assertEqual(again.engine_kind, "legacy")
        self.assertEqual(again.engine_version, "legacy-1")
        self.assertEqual(again.schema_version, s.SCHEMA_VERSION)

    def test_future_schema_is_rejected(self):
        data = s.ProjectState(run_id="r1", goal="g").to_dict()
        data["schema_version"] = s.SCHEMA_VERSION + 1
        with self.assertRaises(s.StateSchemaError):
            s.ProjectState.from_dict(data)

    def test_missing_engine_kind_is_rejected(self):
        with self.assertRaises(s.StateSchemaError):
            s.ProjectState.from_dict({"run_id": "r1", "schema_version": 2})


class LegacyMigrationTests(unittest.TestCase):
    def test_factory_state_migration_keeps_engine_legacy(self):
        raw = json.loads((FIX / "factory_state_architect.json").read_text())
        st = s.from_legacy_factory(raw)
        self.assertEqual(st.engine_kind, s.ENGINE_LEGACY)
        self.assertEqual(st.schema_version, s.SCHEMA_VERSION)
        self.assertEqual(st.requirements.objective, "demo app")
        self.assertEqual(st.project_root, "/tmp/crew-demo")
        self.assertEqual(len(st.tasks), 1)
        self.assertEqual(st.tasks[0].target_file, "app.py")
        # migrated legacy runs keep the old order: ARCHITECT -> BUILD
        self.assertEqual(st.phase, "BUILD")
        self.assertTrue(st.migrations)

    def test_factory_scaffold_step_maps_to_architecture(self):
        raw = json.loads((FIX / "factory_state_architect.json").read_text())
        raw["step"] = "SCAFFOLD"
        st = s.from_legacy_factory(raw)
        self.assertEqual(st.phase, "ARCHITECTURE")

    def test_unified_migration_uses_corrected_order(self):
        raw = json.loads((FIX / "factory_state_architect.json").read_text())
        st = s.from_legacy_factory(raw, engine_kind=s.ENGINE_UNIFIED)
        self.assertEqual(st.phase, "SCAFFOLD")

    def test_missing_requirements_forces_spec(self):
        raw = json.loads((FIX / "factory_state_architect.json").read_text())
        raw["step"] = "REQUIREMENTS"
        raw["requirements"] = {}
        self.assertEqual(s.from_legacy_factory(raw).phase, "SPEC")

    def test_missing_architecture_reopens_architecture(self):
        raw = json.loads((FIX / "factory_state_architect.json").read_text())
        raw["step"] = "ARCHITECT"
        raw["architecture"] = {}
        self.assertEqual(s.from_legacy_factory(raw).phase, "ARCHITECTURE")
        raw["step"] = "SCAFFOLD"
        self.assertEqual(s.from_legacy_factory(raw).phase, "ARCHITECTURE")

    def test_session_migration(self):
        session = json.loads((FIX / "session.json").read_text())
        spec = json.loads((FIX / "spec.json").read_text())
        st = s.from_legacy_session(session, spec)
        self.assertEqual(st.engine_kind, s.ENGINE_LEGACY)
        self.assertEqual(st.phase, "DONE")
        self.assertEqual(st.tasks[0].status, "verified")
        self.assertEqual(st.tasks[0].kind, "build")

    def test_migration_does_not_mutate_input(self):
        raw = json.loads((FIX / "factory_state_architect.json").read_text())
        before = json.dumps(raw, sort_keys=True)
        s.from_legacy_factory(raw)
        self.assertEqual(json.dumps(raw, sort_keys=True), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
