#!/usr/bin/env python3
"""P7.1d seam: FactoryAdapter reference vs NativeAdapter candidate.

This first cassette covers BUILD + INTEGRATE + SIGNOFF. Publication is never
called. More failure/resume scenarios belong here before removing Factory.
"""

import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import main
    import engine
    import native_adapter
    import state as s
    from registry import ModelRef
    from attempts import AttemptEvent
except Exception as exc:  # pragma: no cover
    DEPS_OK = False
    DEPS_ERR = str(exc)
else:
    DEPS_OK = True
    DEPS_ERR = ""


class FakeVerifier:
    def run_oracle(self, task_id, artifact, run_id=None, suffix=""):
        return types.SimpleNamespace(passed=True, exit_code=0)

    def run_critic(self, producer, task, artifact, run_id=None, review=False):
        return {"verdict": "PASS", "critic": {"ok": True},
                "producer_family": "fake", "critic_family": "critic",
                "producer_known": True, "objections": []}

    def run_test_oracle(self, *a, **k):
        return {"verdict": "SKIP", "advisory": True}


class FakePool:
    def __init__(self):
        self.telemetry = None
        self.attempt_sink = None
        self.dispatch_guard = None
        self.last_attempt_model = {}
        self.router = types.SimpleNamespace(route=lambda req: types.SimpleNamespace(
            candidates=[], chain=None))

    def is_peak(self):
        return False

    def refresh_peak(self):
        return None

    def team_llm(self, task, run_id=""):
        return ("chain", "DEV")

    def complete(self, chain, messages, meta=None):
        meta = meta or {}
        task = meta.get("task") or ""
        tag = meta.get("tag") or ""
        model = ModelRef(provider="fake", endpoint="local", model_id="fake-1",
                         family="fake", billing="local",
                         credential_profile="default")
        if task:
            self.last_attempt_model[task] = model
            if self.attempt_sink:
                from state import TaskAttempt, new_id, utc_now
                ev = AttemptEvent(TaskAttempt(
                    attempt_id=new_id("att_"), model=model,
                    started_at=utc_now(), completed_at=utc_now(),
                    outcome="ok"), task=task, tag=tag, run_id="r")
                self.attempt_sink(ev)
        if tag == "SIGN":
            return '{"what_it_is": "demo", "how_to_run": ["python app.py"]}'
        blob = str(meta.get("prompt") or "") + str(messages)
        if "Deliverable file: util.py" in blob:
            return "```python\nx = 2\n```"
        return "```python\nprint('ok')\n```"


@unittest.skipUnless(DEPS_OK, f"runtime deps missing: {DEPS_ERR}")
class DifferentialSmokeTests(unittest.TestCase):
    def _state(self, root: Path, ws: Path):
        st = s.ProjectState(run_id="r", project_root=str(root), goal="demo",
                            engine_kind=s.ENGINE_UNIFIED,
                            engine_version=engine.ENGINE_VERSION,
                            phase="BUILD", profile="script")
        st.requirements = s.RequirementContract(objective="demo")
        st.architecture = s.ArchitectureContract(
            approach="two files", modules=[{"path": "app.py"}, {"path": "util.py"}],
            interfaces=[{"name": "main", "file": "app.py"}])
        st.tasks = [
            s.Task("a", kind="build", target_file="app.py", acceptance=["runs"]),
            s.Task("b", kind="build", target_file="util.py", acceptance=["runs"]),
        ]
        st.approve_architecture()
        st.extensions["workspace_root"] = str(ws)
        return st

    def test_factory_visual_rechecks_after_fix(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = base / "proj"; proj.mkdir()
            ws = base / "ws"; ws.mkdir()
            (proj / "index.html").write_text("<html></html>", encoding="utf-8")
            settings = main.Settings()
            settings.keep_awake = False
            settings.sleep_offpeak = False
            settings.visual_check = True
            settings.allow_unverified = False
            settings.workspace_dir = str(base / "workspaces")
            settings.log_dir = str(base / "logs")
            st = self._state(proj, ws)
            st.phase = "VISUAL"
            pool = FakePool()
            adapter = engine.FactoryAdapter(
                settings, pool, lambda *a: None, st, isolate_workspace=False)
            adapter.factory.verifier = FakeVerifier()
            audits = [{"verdict": "FAIL", "screenshot": str(proj / "one.png")},
                      {"verdict": "PASS", "screenshot": str(proj / "two.png")}]
            reviews = [{"verdict": "FAIL", "objections": [{"claim": "bad"}]},
                       {"verdict": "PASS"}]
            adapter.factory.playwright_audit = lambda root: audits.pop(0)
            adapter.factory._vision_review = lambda png: reviews.pop(0)
            adapter.factory._visual_fix = lambda v: None
            self.assertTrue(adapter.step("VISUAL"))
            adapter.sync_to_project()
            self.assertEqual(st.visual["vision_retry"]["verdict"], "FAIL")
            self.assertEqual(st.visual["vision"]["verdict"], "PASS")

    def test_build_integrate_signoff_match(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj_f = base / "proj_f"; proj_f.mkdir()
            proj_n = base / "proj_n"; proj_n.mkdir()
            settings = main.Settings()
            settings.keep_awake = False
            settings.sleep_offpeak = False
            settings.test_oracle = False
            settings.allow_unverified = False
            settings.workspace_dir = str(base / "workspaces")
            settings.log_dir = str(base / "logs")
            pool = FakePool()
            ref = engine.FactoryAdapter(
                settings, pool, lambda *a: None,
                self._state(proj_f, base / "ws_f"), isolate_workspace=True)
            native = native_adapter.NativeAdapter(
                settings, pool, lambda *a: None,
                self._state(proj_n, base / "ws_n"), verifier=FakeVerifier(),
                isolate_workspace=True)
            ref.factory.verifier = FakeVerifier()

            def reference_ask(role, system, user, tag, task_id="", complexity="auto"):
                return pool.complete(None, [], {"tag": tag, "task": task_id,
                                                "prompt": user})

            ref.factory._ask = reference_ask

            for phase in ("BUILD", "INTEGRATE", "SIGNOFF"):
                self.assertTrue(ref.step(phase), phase)
                self.assertTrue(native.step(phase), phase)
            ref.sync_to_project()

            for name in ("app.py", "util.py"):
                a = (base / "ws_f" / name).read_text()
                b = (base / "ws_n" / name).read_text()
                self.assertEqual(a, b, name)
            self.assertEqual([(t.id, t.status) for t in ref.project.tasks],
                             [(t.id, t.status) for t in native.project.tasks])
            for a, b in zip(ref.project.tasks, native.project.tasks):
                self.assertEqual(a.delivered_by.model_id if a.delivered_by else None,
                                 b.delivered_by.model_id if b.delivered_by else None)
                self.assertEqual(ref.project.verifications[a.id].verdict,
                                 native.project.verifications[b.id].verdict)
            self.assertEqual(ref.project.integration.get("verdict"),
                             native.project.integration.get("verdict"))
            self.assertFalse((base / "ws_n" / ".factory").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
