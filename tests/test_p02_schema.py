#!/usr/bin/env python3
"""P2: explicit schema v2 -> v3 migration (no silent upgrades)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import state as s
import store as store_mod
from store import StateManager, atomic_write_json


def v2_state(project: Path, phase: str = "BUILD") -> dict:
    return {
        "run_id": "v2-run",
        "project_root": str(project),
        "goal": "demo",
        "profile": "auto",
        "phase": phase,
        "schema_version": 2,
        "engine_kind": "legacy",
        "engine_version": "1.0",
        "requirements": {"objective": "demo", "acceptance": ["runs"],
                         "open_questions": [], "custom_req": {"keep": True}},
        "architecture": {"approach": "single file",
                         "modules": [{"path": "a.py"}],
                         "interfaces": [{"name": "main", "file": "a.py"}],
                         "test_plan": [{"task": "t1", "check": "runs"}]},
        "scaffold": {},
        "tasks": [{
            "id": "t1", "title": "run", "description": "main",
            "target_file": "a.py", "acceptance": ["runs"], "depends_on": [],
            "complexity": "low", "role": "junior", "status": "pending",
            "errors": [], "attempts": [], "delivered_by": None,
            "custom_task": {"keep": True},
        }],
        "integration": {}, "visual": {}, "signoff": {},
        "approvals": [], "verifications": {}, "pause": {},
        "history": [], "migrations": [], "created": "", "updated": "",
        "custom_top": {"keep": True},
    }


class SchemaMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.project = self.base / "p"
        self.project.mkdir()
        self.store = StateManager(self.base / "workspace", self.base / "logs")
        self.path = self.store.project_state_path(self.project)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_v2(self, phase="BUILD", project=None):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, v2_state(project or self.project, phase))

    def test_v2_is_not_loaded_silently(self):
        self._write_v2()
        with self.assertRaises(s.StateSchemaError):
            self.store.load_project(self.path)

    def test_v2_migrates_in_every_phase(self):
        for phase in s.PHASES:
            with self.subTest(phase=phase):
                self._write_v2(phase)
                st = self.store.migrate_schema_to_v3(self.project)
                self.assertEqual(st.schema_version, 3)
                self.assertEqual(st.phase, phase)
                self.assertEqual(st.requirements.acceptance_texts(), ["runs"])
                self.assertEqual(st.requirements.acceptance[0].task_ids, ["t1"])
                self.assertEqual(st.tasks[0].requirement_ids, ["ac1"])

    def test_old_architecture_approval_no_longer_matches_v3_hash(self):
        self._write_v2()
        raw = json.loads(self.path.read_text())
        raw["approvals"] = [{"kind": "architecture", "artifact_hash": "old",
                             "actor": "human", "decision": "approved",
                             "timestamp": "x", "detail": ""}]
        atomic_write_json(self.path, raw)
        st = self.store.migrate_schema_to_v3(self.project)
        self.assertFalse(st.architecture_approved())

    def test_unknown_fields_are_preserved(self):
        self._write_v2()
        st = self.store.migrate_schema_to_v3(self.project)
        self.assertEqual(st.extensions["custom_top"], {"keep": True})
        self.assertEqual(st.tasks[0].legacy_extra["custom_task"], {"keep": True})
        self.assertEqual(st.requirements.raw["custom_req"], {"keep": True})

    def test_snapshot_and_idempotence(self):
        self._write_v2()
        first = self.store.migrate_schema_to_v3(self.project)
        snaps = list(self.store.history_dir(self.project).glob("schema-v2-source-*.json"))
        self.assertEqual(len(snaps), 1)
        second = self.store.migrate_schema_to_v3(self.project)
        self.assertEqual(second.schema_version, 3)
        snaps2 = list(self.store.history_dir(self.project).glob("schema-v2-source-*.json"))
        self.assertEqual(len(snaps2), 1)
        self.assertEqual(first.run_id, second.run_id)

    def test_corrupt_v2_is_preserved(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{broken", encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaises(s.StateCorruptError):
            self.store.migrate_schema_to_v3(self.project)
        self.assertEqual(self.path.read_bytes(), before)

    def test_failed_save_leaves_v2_intact(self):
        self._write_v2()
        before = self.path.read_bytes()
        original = store_mod.atomic_write_json

        def boom(path, data, validate=None):
            if Path(path) == self.path:
                raise RuntimeError("disk full")
            return original(path, data, validate=validate)

        store_mod.atomic_write_json = boom
        try:
            with self.assertRaises(RuntimeError):
                self.store.migrate_schema_to_v3(self.project)
        finally:
            store_mod.atomic_write_json = original
        self.assertEqual(self.path.read_bytes(), before)
        snaps = list(self.store.history_dir(self.project).glob("schema-v2-source-*.json"))
        self.assertEqual(len(snaps), 1)

    def test_no_silent_v2_write_on_load(self):
        self._write_v2()
        before = self.path.read_bytes()
        with self.assertRaises(s.StateSchemaError):
            self.store.load_project(self.path)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
