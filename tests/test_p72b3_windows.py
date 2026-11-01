#!/usr/bin/env python3
"""P7.2b.3 V05: native filesystem atomic-write tests.

These tests exercise the *real* ``os.replace`` path (no ``platform="nt"``
mocking). They run on Linux and on the native Windows CI runner; on Windows they
are the native evidence that ``atomic_write_json`` / ``_replace_with_retry``
succeed against the actual filesystem and leave no temp files behind.

They are not a substitute for reproducing a transient Windows PermissionError
(which is only attempted, not fabricated, in ``test_p72b2_reliability``).
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import store as store_mod


class NativeAtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_native_atomic_write_succeeds_and_cleans_temp(self):
        path = self.root / "state.json"
        store_mod.atomic_write_json(path, {"a": 1, "b": "x"})
        self.assertTrue(path.is_file())
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                         {"a": 1, "b": "x"})
        self.assertEqual(list(self.root.glob(".state.json.*.tmp")), [])

    def test_native_replace_with_retry_uses_real_platform(self):
        tmp = self.root / "tmp"; tmp.write_text("payload", encoding="utf-8")
        dst = self.root / "dst"
        store_mod._replace_with_retry(tmp, dst)
        self.assertTrue(dst.is_file())
        self.assertEqual(dst.read_text(encoding="utf-8"), "payload")

    def test_native_atomic_write_roundtrip_utf8(self):
        path = self.root / "state.json"
        payload = {"nota": "olá, mundo ☕", "n": 42}
        store_mod.atomic_write_json(path, payload)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
