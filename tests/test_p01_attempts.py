#!/usr/bin/env python3
"""P0/P1: every candidate call in a chain produces its own attempt."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from attempts import AttemptCollector
from registry import ref_for


class AttemptCollectorTests(unittest.TestCase):
    def test_three_candidates_three_attempts_and_last_ok(self):
        col = AttemptCollector(run_id="r1", task="t1", tag="DEV")
        flash = ref_for("gemini-plus", "gemini-flash")
        pro = ref_for("gemini-plus", "gemini-pro")
        kimi = ref_for("opencode-go", "kimi-k3.1-pro")

        a1 = col.start(flash)
        col.finish(a1, "429/quota", error="429 rate limit")
        a2 = col.start(pro)
        col.finish(a2, "timeout", error="timed out")
        a3 = col.start(kimi)
        col.finish(a3, "ok", served_id="kimi-k3.1-pro-20260101")

        events = col.events()
        self.assertEqual(len(events), 3)
        self.assertEqual([e.attempt.outcome for e in events],
                         ["429/quota", "timeout", "ok"])
        last = col.last_ok("t1")
        self.assertEqual(last.attempt.model.model_id, "kimi-k3.1-pro-20260101")
        self.assertEqual(last.attempt.model.requested_id, "kimi-k3.1-pro")

    def test_no_ok_attempt_returns_none(self):
        col = AttemptCollector(task="t1")
        aid = col.start(ref_for("gemini-plus", "flash"))
        col.finish(aid, "error", error="boom")
        self.assertIsNone(col.last_ok("t1"))

    def test_synthesize_is_used_without_callbacks(self):
        col = AttemptCollector(task="t1")
        col.synthesize(ref_for("local", "qwen"), "ok")
        self.assertEqual(len(col.events()), 1)
        self.assertEqual(col.last_ok("t1").attempt.model.provider, "local")


if __name__ == "__main__":
    unittest.main(verbosity=2)
