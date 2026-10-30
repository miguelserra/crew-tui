#!/usr/bin/env python3
"""P0/P1: canonical store, corruption handling, promotion and migration."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import state as s
from store import StateManager, atomic_write_json, read_json

FIX = Path(__file__).resolve().parent / "fixtures" / "p01"


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        self.store = StateManager(workspace_dir=self.base / "workspace",
                                  log_dir=self.base / "logs")

    def tearDown(self):
        self.tmp.cleanup()

    def _state(self, run_id="r1", root=True, engine=s.ENGINE_UNIFIED):
        return s.ProjectState(run_id=run_id, goal="g",
                              project_root=str(self.project) if root else "",
                              engine_kind=engine, engine_version="1.0")

    def test_atomic_round_trip(self):
        st = self._state()
        path = self.store.save_project(st)
        again = self.store.load_project(path)
        self.assertEqual(again.run_id, "r1")
        self.assertEqual(again.engine_kind, s.ENGINE_UNIFIED)

    def test_corrupt_state_raises_and_is_preserved(self):
        path = self.store.project_state_path(self.project)
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaises(s.StateCorruptError):
            self.store.load_project(path)
        self.assertEqual(path.read_bytes(), before)

    def test_atomic_write_leaves_no_tmp_on_validation_failure(self):
        path = self.project / "x.json"

        def no(_data):
            raise ValueError("bad")

        with self.assertRaises(ValueError):
            atomic_write_json(path, {"a": 1}, validate=no)
        self.assertFalse(path.exists())
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_staging_promotion_consumes_staging(self):
        st = self._state(root=False)
        staging = self.store.save_staging(st)
        st.project_root = str(self.project)
        final = self.store.promote_staging(staging, self.project, st)
        self.assertTrue(final.is_file())
        self.assertFalse(staging.exists())
        self.assertTrue(list(staging.parent.glob("*consumed*")))

    def test_promotion_is_idempotent_for_same_run(self):
        st = self._state(root=False)
        staging = self.store.save_staging(st)
        st.project_root = str(self.project)
        self.store.promote_staging(staging, self.project, st)
        # simulate a crash: staging still present, final already valid
        self.store.save_staging(st)
        self.store.promote_staging(staging, self.project, st)
        self.assertFalse(staging.exists())
        self.assertEqual(self.store.load_project(
            self.store.project_state_path(self.project)).run_id, "r1")

    def test_promotion_archives_previous_run(self):
        first = self._state("r1")
        self.store.save_project(first)
        second = self._state("r2")
        staging = self.store.save_staging(second)
        second.project_root = str(self.project)
        self.store.promote_staging(staging, self.project, second)
        hist = self.store.history_dir(self.project) / "r1.json"
        self.assertTrue(hist.is_file())
        self.assertEqual(self.store.load_project(
            self.store.project_state_path(self.project)).run_id, "r2")

    def test_corrupt_final_is_not_overwritten_by_promotion(self):
        final = self.store.project_state_path(self.project)
        final.parent.mkdir(parents=True)
        final.write_text("{broken", encoding="utf-8")
        st = self._state(root=False)
        staging = self.store.save_staging(st)
        st.project_root = str(self.project)
        with self.assertRaises(s.StateCorruptError):
            self.store.promote_staging(staging, self.project, st)
        self.assertTrue(staging.is_file())
        self.assertEqual(final.read_text(encoding="utf-8"), "{broken")

    def test_resume_prefers_crew_and_never_falls_back_on_corruption(self):
        factory = self.store.legacy_factory_path(self.project)
        factory.parent.mkdir(parents=True)
        factory.write_text(json.dumps({"project": str(self.project), "step": "BUILD",
                                       "engine_kind": "legacy", "schema_version": 2}),
                           encoding="utf-8")
        target = self.store.resolve_resume(self.project)
        self.assertEqual(target.engine_kind, "legacy")
        self.assertEqual(target.path, factory)

        crew = self.store.project_state_path(self.project)
        crew.parent.mkdir(parents=True, exist_ok=True)
        crew.write_text("{broken", encoding="utf-8")
        with self.assertRaises(s.StateCorruptError):
            self.store.resolve_resume(self.project)
        self.assertTrue(factory.is_file())

    def test_migrate_format_keeps_engine_legacy_and_preserves_source(self):
        factory = self.store.legacy_factory_path(self.project)
        factory.parent.mkdir(parents=True)
        raw = json.loads((FIX / "factory_state_architect.json").read_text())
        raw["project"] = str(self.project)
        atomic_write_json(factory, raw)
        before = factory.read_bytes()

        st = self.store.migrate_factory_to_crew(self.project)
        self.assertEqual(st.engine_kind, s.ENGINE_LEGACY)
        self.assertEqual(st.schema_version, s.SCHEMA_VERSION)
        self.assertEqual(factory.read_bytes(), before)
        crew = self.store.load_project(self.store.project_state_path(self.project))
        self.assertEqual(crew.engine_kind, s.ENGINE_LEGACY)
        snapshots = list(self.store.history_dir(self.project).glob("factory-source-*.json"))
        self.assertEqual(len(snapshots), 1)

    def test_legacy_metadata_defaults(self):
        factory = self.store.legacy_factory_path(self.project)
        factory.parent.mkdir(parents=True)
        factory.write_text(json.dumps({"project": str(self.project), "step": "BUILD"}),
                           encoding="utf-8")
        target = self.store.resolve_resume(self.project)
        self.assertEqual(target.engine_kind, "legacy")
        self.assertEqual(target.schema_version, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
