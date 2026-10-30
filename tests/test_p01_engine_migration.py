#!/usr/bin/env python3
"""P1: --migrate-engine validation, idempotence and transactionality."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine as e
import state as s
from store import StateManager


class MigrateEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.project = self.base / "p"
        self.project.mkdir()
        self.store = StateManager(self.base / "workspace", self.base / "logs")

    def tearDown(self):
        self.tmp.cleanup()

    def _state(self, *, phase="BUILD", requirements=True, architecture=True,
               tasks=None, engine_kind=s.ENGINE_LEGACY):
        st = s.ProjectState(
            run_id="r1", project_root=str(self.project), goal="demo",
            engine_kind=engine_kind, engine_version="legacy", phase=phase,
            requirements=s.RequirementContract(objective="demo") if requirements
            else s.RequirementContract(),
            architecture=s.ArchitectureContract(approach="a") if architecture
            else s.ArchitectureContract(),
            tasks=tasks if tasks is not None else [
                s.Task(id="t1", kind="build", target_file="a.py")],
        )
        return st

    def _save(self, st):
        return self.store.save_project(st)

    def test_idempotent(self):
        st = self._state()
        self._save(st)
        first = e.migrate_engine(self.store, st)
        count = len(first.migrations)
        second = e.migrate_engine(self.store, first)
        self.assertEqual(second.engine_kind, s.ENGINE_UNIFIED)
        self.assertEqual(len(second.migrations), count)

    def test_missing_architecture_rewinds_to_architecture(self):
        st = self._state(phase="BUILD", architecture=False)
        self._save(st)
        out = e.migrate_engine(self.store, st)
        self.assertEqual(out.phase, "ARCHITECTURE")

    def test_missing_requirements_rewinds_to_spec(self):
        st = self._state(phase="BUILD", requirements=False)
        self._save(st)
        out = e.migrate_engine(self.store, st)
        self.assertEqual(out.phase, "SPEC")

    def test_stale_approval_reopens_gate(self):
        st = self._state(phase="BUILD")
        st.approvals.append(s.Approval(kind="architecture",
                                       artifact_hash="old-hash"))
        self._save(st)
        out = e.migrate_engine(self.store, st)
        self.assertEqual(out.pause.reason, "architecture_approval")
        self.assertFalse(out.architecture_approved())

    def test_missing_dependency_is_rejected_at_state_boundary(self):
        with self.assertRaises(s.StateValidationError):
            self._state(tasks=[s.Task(id="t1", kind="build", target_file="a.py",
                                      depends_on=["ghost"])])

    def test_cycle_is_rejected_at_state_boundary(self):
        with self.assertRaises(s.StateValidationError):
            self._state(tasks=[
                s.Task(id="t1", kind="build", target_file="a.py", depends_on=["t2"]),
                s.Task(id="t2", kind="build", target_file="b.py", depends_on=["t1"]),
            ])

    def test_snapshot_created_and_factory_untouched(self):
        factory = self.store.legacy_factory_path(self.project)
        factory.parent.mkdir(parents=True)
        factory.write_text('{"legacy": true}', encoding="utf-8")
        before = factory.read_bytes()
        st = self._state()
        self._save(st)
        e.migrate_engine(self.store, st)
        self.assertEqual(factory.read_bytes(), before)
        snaps = list(self.store.history_dir(self.project).glob("engine-source-*.json"))
        self.assertEqual(len(snaps), 1)

    def test_failed_save_leaves_source_intact(self):
        st = self._state()
        path = self._save(st)
        before = path.read_bytes()
        original = self.store.save_project

        def boom(_state):
            raise RuntimeError("disk full")

        self.store.save_project = boom
        with self.assertRaises(RuntimeError):
            e.migrate_engine(self.store, st)
        self.store.save_project = original
        self.assertEqual(path.read_bytes(), before)
        snaps = list(self.store.history_dir(self.project).glob("engine-source-*.json"))
        self.assertEqual(len(snaps), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
