#!/usr/bin/env python3
"""P7.2b.3 V06: visual artifact hash versioning and migration tests.

Locks down the versioned visual-hash contract introduced to fix the silent
backward-compatibility break:

* new records store ``{"v": 2, "sha": <raw-byte sha256>}``;
* legacy records (plain hex string) are interpreted with the old
  normalized-text ``artifact_hash`` semantics, never as raw bytes;
* unchanged legacy evidence stays valid; changed or missing evidence is
  fail-closed (re-audit); mixed records coexist; re-audit is additive and
  idempotent; sign-off is never weakened.
"""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import state as s
import visual


def _state(root, visual_payload=None):
    st = s.ProjectState(run_id="r", goal="g", engine_kind=s.ENGINE_UNIFIED,
                        project_root=str(root))
    st.tasks = [s.Task("a", kind="build", target_file="index.html")]
    st.visual = visual_payload or {}
    st.signoff = {"generated": True}
    return st


class VisualHashSemanticsTests(unittest.TestCase):
    def test_new_record_is_versioned_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "index.html").write_bytes(b"<html>ok</html>\n")
            hashes = visual.VisualService._artifact_hashes(_state(root), root)
            self.assertEqual(hashes["index.html"],
                             {"v": 2, "sha": hashlib.sha256(
                                 b"<html>ok</html>\n").hexdigest()})

    def test_legacy_unchanged_artifact_is_valid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            text = "<html>ok</html>\n"
            (root / "index.html").write_text(text, encoding="utf-8")
            legacy = s.artifact_hash(text)
            self.assertTrue(s.visual_artifact_matches(
                legacy, root / "index.html"))

    def test_legacy_crlf_normalizes_to_same_hash(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            text = "<html>ok</html>\r\n"
            (root / "index.html").write_bytes(text.encode("utf-8"))
            legacy = s.artifact_hash("<html>ok</html>\n")
            self.assertTrue(s.visual_artifact_matches(
                legacy, root / "index.html"))

    def test_legacy_changed_artifact_is_stale(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "index.html").write_text("<html>old</html>\n", encoding="utf-8")
            legacy = s.artifact_hash("<html>new</html>\n")
            self.assertFalse(s.visual_artifact_matches(
                legacy, root / "index.html"))

    def test_legacy_missing_artifact_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            legacy = s.artifact_hash("<html>ok</html>\n")
            self.assertFalse(s.visual_artifact_matches(
                legacy, root / "index.html"))

    def test_legacy_binary_artifact_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "index.html").write_bytes(b"\xff\xfe\x00")
            legacy = s.artifact_hash("whatever\n")
            self.assertFalse(s.visual_artifact_matches(
                legacy, root / "index.html"))

    def test_bytes_version_matches_raw_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "index.html").write_bytes(b"\xff\xfe\x00")
            record = {"v": 2, "sha": hashlib.sha256(b"\xff\xfe\x00").hexdigest()}
            self.assertTrue(s.visual_artifact_matches(record, root / "index.html"))

    def test_unknown_version_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "index.html").write_bytes(b"x")
            self.assertFalse(s.visual_artifact_matches(
                {"v": 99, "sha": "x" * 64}, root / "index.html"))

    def test_missing_sha_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "index.html").write_bytes(b"x")
            self.assertFalse(s.visual_artifact_matches(
                {"v": 2}, root / "index.html"))

    def test_non_numeric_version_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "index.html").write_bytes(b"x")
            # F4: malformed version metadata must not leak ValueError/TypeError.
            self.assertFalse(s.visual_artifact_matches(
                {"v": "not-a-number", "sha": "x" * 64}, root / "index.html"))

    def test_boolean_version_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "index.html").write_bytes(b"x")
            # bool is a subclass of int but is not a valid version marker.
            self.assertFalse(s.visual_artifact_matches(
                {"v": True, "sha": "x" * 64}, root / "index.html"))

    def test_float_version_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "index.html").write_bytes(b"x")
            self.assertFalse(s.visual_artifact_matches(
                {"v": 2.0, "sha": "x" * 64}, root / "index.html"))

    def test_missing_version_in_dict_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "index.html").write_bytes(b"x")
            # A dict without an explicit version is not a valid versioned record.
            self.assertFalse(s.visual_artifact_matches(
                {"sha": "x" * 64}, root / "index.html"))

    def test_malformed_sha_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "index.html").write_bytes(b"x")
            self.assertFalse(s.visual_artifact_matches(
                {"v": 2, "sha": 123}, root / "index.html"))
            self.assertFalse(s.visual_artifact_matches(
                {"v": 2, "sha": ""}, root / "index.html"))


class VisualMigrationTests(unittest.TestCase):
    def test_legacy_state_loads_without_corruption(self):
        legacy_hex = s.artifact_hash("<html>ok</html>\n")
        raw = {
            "run_id": "r", "schema_version": s.SCHEMA_VERSION,
            "engine_kind": s.ENGINE_UNIFIED, "goal": "g",
            "tasks": [{"id": "a", "kind": "build", "target_file": "index.html"}],
            "visual": {"verdict": "PASS",
                       "artifact_hashes": {"index.html": legacy_hex}},
        }
        st = s.ProjectState.from_dict(raw)
        self.assertEqual(st.visual["artifact_hashes"]["index.html"], legacy_hex)

    def test_reaudit_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "index.html").write_bytes(b"<html>ok</html>\n")
            st = _state(root)
            first = visual.VisualService._artifact_hashes(st, root)
            second = visual.VisualService._artifact_hashes(st, root)
            self.assertEqual(first, second)

    def test_mixed_legacy_and_current_records(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "index.html").write_text("<html>a</html>\n", encoding="utf-8")
            css = root / "style.css"
            # Write exact bytes so the v2 expectation is independent of the
            # host's text-mode newline translation (Windows writes CRLF).
            css.write_bytes(b"body{}\n")
            legacy = s.artifact_hash("<html>a</html>\n")
            current = {"v": 2, "sha": hashlib.sha256(
                css.read_bytes()).hexdigest()}
            st = s.ProjectState(run_id="r", goal="g",
                                engine_kind=s.ENGINE_UNIFIED,
                                project_root=str(root))
            st.tasks = [s.Task("a", kind="build", target_file="index.html"),
                        s.Task("b", kind="build", target_file="style.css")]
            st.visual = {"verdict": "PASS",
                         "artifact_hashes": {"index.html": legacy,
                                             "style.css": current}}
            st.signoff = {"generated": True}
            blockers = st.signoff_blockers()
            self.assertFalse(any("visual desatualizado" in b for b in blockers))


class SignoffVisualIntegrationTests(unittest.TestCase):
    def _build(self, root, hashes):
        st = s.ProjectState(run_id="r", goal="g", engine_kind=s.ENGINE_UNIFIED,
                            project_root=str(root))
        st.tasks = [s.Task("a", kind="build", target_file="index.html")]
        st.visual = {"verdict": "PASS", "artifact_hashes": hashes}
        st.signoff = {"generated": True}
        return st

    def test_current_valid_evidence_does_not_block_visual(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "index.html").write_bytes(b"<html>ok</html>\n")
            st = self._build(root, {
                "index.html": {"v": 2, "sha": hashlib.sha256(
                    b"<html>ok</html>\n").hexdigest()}})
            self.assertFalse(any("visual desatualizado" in b
                                 for b in st.signoff_blockers()))

    def test_legacy_valid_evidence_does_not_block_visual(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            text = "<html>ok</html>\n"
            (root / "index.html").write_text(text, encoding="utf-8")
            st = self._build(root, {"index.html": s.artifact_hash(text)})
            self.assertFalse(any("visual desatualizado" in b
                                 for b in st.signoff_blockers()))

    def test_legacy_stale_evidence_blocks_visual(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "index.html").write_text("<html>old</html>\n", encoding="utf-8")
            st = self._build(root, {"index.html": s.artifact_hash(
                "<html>new</html>\n")})
            self.assertTrue(any("visual desatualizado" in b
                                for b in st.signoff_blockers()))

    def _blocked(self, hashes):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "index.html").write_text("<html>ok</html>\n", encoding="utf-8")
            st = self._build(root, hashes)
            return any("visual desatualizado" in b
                       for b in st.signoff_blockers())

    def test_absent_entry_does_not_block(self):
        # A task with no persisted visual hash entry keeps current behaviour.
        self.assertFalse(self._blocked({"other.css": s.artifact_hash("x")}))

    def test_present_empty_string_blocks(self):
        # F7: explicitly present but empty evidence must fail closed.
        self.assertTrue(self._blocked({"index.html": ""}))

    def test_present_empty_mapping_blocks(self):
        self.assertTrue(self._blocked({"index.html": {}}))

    def test_present_null_blocks(self):
        self.assertTrue(self._blocked({"index.html": None}))

    def test_present_malformed_versioned_blocks(self):
        self.assertTrue(self._blocked(
            {"index.html": {"v": "bad", "sha": "x" * 64}}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
