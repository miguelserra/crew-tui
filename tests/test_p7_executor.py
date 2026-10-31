#!/usr/bin/env python3
"""P7.1b dependency-backed native BUILD/FIX smoke."""

import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import main
    import executor
    import state as s
except Exception as exc:  # pragma: no cover
    DEPS_OK = False
    DEPS_ERR = str(exc)
else:
    DEPS_OK = True
    DEPS_ERR = ""


class FakePool:
    def __init__(self, code="```python\nprint('ok')\n```"):
        self.code = code
        self.calls = 0
        self.last_attempt_model = {}
        self.router = types.SimpleNamespace(route=lambda req: types.SimpleNamespace(
            candidates=[object()], chain=object()))

    def team_llm(self, task, run_id=""):
        return object(), "DEV"

    def complete(self, llm, messages, meta=None):
        self.calls += 1
        if meta:
            import registry as reg
            reg_ = reg.ModelRegistry()
            self.last_attempt_model[meta.get("task", "")] = reg_.ref_for_identity(
                reg_.identity_for("opencode-go", "kimi-k3.1-pro"))
        return self.code


class FakeVerifier:
    def __init__(self, verdicts=None):
        self.verdicts = list(verdicts or ["PASS"])
        self.calls = 0
        self.run_oracle = lambda *a, **k: main.OracleResult(True, 0, stdout="")
        self.run_test_oracle = lambda *a, **k: {"verdict": "SKIP", "advisory": True}

    def run_critic(self, *a, **k):
        v = self.verdicts[min(self.calls, len(self.verdicts) - 1)]
        self.calls += 1
        return {"verdict": v, "objections": ([{"claim": "bad"}] if v == "FAIL" else []),
                "producer_family": "kimi", "critic_family": "gemini",
                "critic": {"model_id": "gemini-3.1-pro-preview"}}


@unittest.skipUnless(DEPS_OK, f"runtime deps missing: {DEPS_ERR}")
class ExecutorTests(unittest.TestCase):
    def _project(self):
        st = s.ProjectState(run_id="r", goal="demo", project_root="",
                            engine_kind=s.ENGINE_UNIFIED)
        st.requirements = s.RequirementContract(
            objective="demo", acceptance=[s.AcceptanceCriterion(id="ac1", text="runs")])
        st.architecture = s.ArchitectureContract(
            approach="script", modules=[{"path": "app.py"}],
            interfaces=[{"name": "main", "file": "app.py"}])
        st.tasks = [s.Task(id="t1", kind="build", target_file="app.py",
                           acceptance=["runs"], requirement_ids=["ac1"])]
        return st

    def test_build_installs_after_critic(self):
        with tempfile.TemporaryDirectory() as td:
            settings = types.SimpleNamespace(test_oracle=False, allow_unverified=False)
            pool = FakePool()
            verifier = FakeVerifier(["PASS"])
            svc = executor.ExecutorService(settings, pool, verifier, lambda *a: None, "r")
            st = self._project()
            self.assertTrue(svc.build(st, td))
            self.assertTrue((Path(td) / "app.py").is_file())
            self.assertEqual(st.verifications["t1"].verdict, "PASS")
            self.assertEqual(st.tasks[0].status, "done")

    def test_fixer_is_used_after_critic_fail(self):
        with tempfile.TemporaryDirectory() as td:
            settings = types.SimpleNamespace(test_oracle=False, allow_unverified=False)
            pool = FakePool("```python\nprint('fixed')\n```")
            verifier = FakeVerifier(["FAIL", "PASS"])
            svc = executor.ExecutorService(settings, pool, verifier, lambda *a: None, "r")
            st = self._project()
            self.assertTrue(svc.build(st, td))
            self.assertIn("fixed", (Path(td) / "app.py").read_text())
            self.assertGreaterEqual(pool.calls, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
