#!/usr/bin/env python3
"""P1: target semantics that must already be green after this phase.

Later-phase defects (hard routing, cross-family critic, oracle policy, visual
capabilities) are expected failures in the integration suite.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine as e
import state as s
from attempts import AttemptCollector
from registry import ModelRef, ref_for, unknown_ref
from store import StateManager


class TargetSemanticsTests(unittest.TestCase):
    def test_engine_kind_and_engine_version_are_distinct(self):
        st = s.ProjectState(run_id="r", engine_kind=s.ENGINE_LEGACY,
                            engine_version="1.0")
        self.assertEqual(st.engine_kind, "legacy")
        self.assertEqual(st.engine_version, "1.0")
        self.assertNotEqual(st.engine_kind, st.engine_version)

    def test_build_target_rules(self):
        with self.assertRaises(s.StateValidationError):
            s.validate_tasks([s.Task(id="t", kind="build", target_file="")])
        with self.assertRaises(s.StateValidationError):
            s.validate_tasks([
                s.Task(id="a", kind="build", target_file="src/A.py"),
                s.Task(id="b", kind="build", target_file="src/a.py"),
            ])
        s.validate_tasks([s.Task(id="r", kind="review", target_file="")])

    def test_attempt_identity_is_the_effective_producer(self):
        col = AttemptCollector(task="t1")
        a = col.start(ref_for("gemini-plus", "flash"))
        col.finish(a, "429/quota")
        b = col.start(ref_for("opencode-go", "kimi"))
        col.finish(b, "ok")
        self.assertEqual(col.last_ok("t1").attempt.model.model_id, "kimi")

    def test_unknown_billing_requires_approval(self):
        st = s.ProjectState(run_id="r")
        engine = e.WorkflowEngine(st, StateManager(), adapter=None)
        with self.assertRaises(e.UnknownBilling):
            engine.check_refs([unknown_ref()])
        st.approve_unknown_billing(unknown_ref())
        engine.check_refs([unknown_ref()])

    def test_resume_engine_is_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "p"
            project.mkdir()
            store = StateManager(base / "workspace", base / "logs")
            store.save_project(s.ProjectState(run_id="r", project_root=str(project),
                                              engine_kind=s.ENGINE_UNIFIED,
                                              engine_version="1.0"))
            old = os.environ.get("CREW_ENGINE")
            os.environ["CREW_ENGINE"] = "legacy"
            try:
                decision = e.open_execution(store, str(project))
            finally:
                if old is None:
                    os.environ.pop("CREW_ENGINE", None)
                else:
                    os.environ["CREW_ENGINE"] = old
            self.assertEqual(decision.engine_kind, s.ENGINE_UNIFIED)

    def test_known_billing_is_not_an_unknown(self):
        engine = e.WorkflowEngine(s.ProjectState(run_id="r"), StateManager(), adapter=None)
        engine.check_refs([ModelRef("google", "gemini", "m", billing="subscription")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
