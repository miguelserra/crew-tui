#!/usr/bin/env python3
"""P2 dependency-backed checks: legacy view <-> canonical ProjectState."""

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import main
    import factory
    import engine
    import state
except Exception as exc:  # pragma: no cover
    DEPS_OK = False
    DEPS_ERR = str(exc)
else:
    DEPS_OK = True
    DEPS_ERR = ""


@unittest.skipUnless(DEPS_OK, f"runtime deps missing: {DEPS_ERR}")
class FactoryViewIntegrationTests(unittest.TestCase):
    def _state(self):
        st = state.ProjectState(run_id="r", goal="demo", phase="BUILD",
                                engine_kind=state.ENGINE_LEGACY,
                                engine_version="legacy")
        st.requirements = state.RequirementContract(
            objective="demo",
            acceptance=[state.AcceptanceCriterion(id="ac1", text="runs")])
        st.architecture = state.ArchitectureContract(
            approach="single file", modules=[{"path": "a.py"}])
        st.tasks = [state.Task(id="t1", kind="build", target_file="a.py",
                               acceptance=["runs"])]
        return st

    def test_compat_dict_round_trips_through_legacy_factory_state(self):
        st = self._state()
        compat = state.factory_compat_dict(st)
        fs = factory.FactoryState.from_dict(compat)
        self.assertEqual(fs.requirements["acceptance"], ["runs"])
        self.assertEqual(fs.tasks[0].requirement_ids, [])
        again = state.from_legacy_factory(fs.to_dict())
        self.assertEqual(again.schema_version, 3)
        self.assertEqual(again.tasks[0].requirement_ids, ["ac1"])

    def test_adapter_sync_links_canonical_traceability(self):
        settings = main.Settings()
        settings.keep_awake = False
        settings.sleep_offpeak = False
        pool = types.SimpleNamespace(is_peak=lambda: False, local_up=False,
                                     attempt_sink=None, dispatch_guard=None)
        st = self._state()
        adapter = engine.FactoryAdapter(settings, pool, lambda *a: None, st,
                                        legacy_order=True)
        adapter.sync_to_project()
        self.assertEqual(st.requirements.acceptance[0].task_ids, ["t1"])
        self.assertEqual(st.tasks[0].requirement_ids, ["ac1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
