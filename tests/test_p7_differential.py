#!/usr/bin/env python3
"""P7.1d seam: FactoryAdapter reference vs NativeAdapter candidate.

Both engines receive the same LLM cassette (FakePool) and the same verifier
script (FakeVerifier), run in separate workspaces, and never publish. The
comparator normalises UUIDs/timestamps and compares semantic evidence.

Remaining matrix slots (fallback, interruption/resume, publish) are still open;
this file documents and covers the first slices.
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


MODEL_KEYS = ("provider", "endpoint", "model_id", "family", "billing",
              "credential_profile")


def canon_model(ref) -> tuple:
    d = ref.to_dict() if hasattr(ref, "to_dict") else (ref or {})
    return tuple((k, d.get(k)) for k in MODEL_KEYS)


def canon_attempts(task) -> list:
    return [(canon_model(a.model), a.outcome) for a in task.attempts]


def canon_verification(project, task_id) -> dict:
    rec = project.verifications[task_id]
    critic = rec.critic or {}
    return {
        "verdict": rec.verdict,
        "oracle_passed": rec.oracle.get("passed"),
        "critic_verdict": critic.get("verdict"),
        "authorized_unverified": rec.authorized_unverified,
    }


class FakeVerifier:
    def __init__(self, fail_first=False, unverified=False):
        self.fail_first = fail_first
        self.unverified = unverified
        self._calls = {}

    def run_oracle(self, task_id, artifact, run_id=None, suffix=""):
        return types.SimpleNamespace(passed=True, exit_code=0)

    def run_critic(self, producer, task, artifact, run_id=None, review=False):
        if self.unverified:
            return {"verdict": "UNAVAILABLE", "critic": None,
                    "producer_family": "", "critic_family": "",
                    "producer_known": False, "objections": []}
        key = getattr(task, "id", task)
        n = self._calls.get(key, 0) + 1
        self._calls[key] = n
        if self.fail_first and n == 1:
            return {"verdict": "FAIL", "critic": {"ok": False},
                    "producer_family": "fake", "critic_family": "critic",
                    "producer_known": True,
                    "objections": [{"claim": "bad", "evidence": "n/a"}]}
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
            candidates=[object()], chain=None))

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

    def _settings(self, base: Path):
        settings = main.Settings()
        settings.keep_awake = False
        settings.sleep_offpeak = False
        settings.test_oracle = False
        settings.allow_unverified = False
        settings.workspace_dir = str(base / "workspaces")
        settings.log_dir = str(base / "logs")
        return settings

    def _pair(self, base, *, fail_first=False, unverified=False):
        proj_f = base / "proj_f"; proj_f.mkdir()
        proj_n = base / "proj_n"; proj_n.mkdir()
        pool_f = FakePool()
        pool_n = FakePool()
        ver_f = FakeVerifier(fail_first=fail_first, unverified=unverified)
        ver_n = FakeVerifier(fail_first=fail_first, unverified=unverified)
        ref = engine.FactoryAdapter(
            self._settings(base), pool_f, lambda *a: None,
            self._state(proj_f, base / "ws_f"), isolate_workspace=True)
        native = native_adapter.NativeAdapter(
            self._settings(base), pool_n, lambda *a: None,
            self._state(proj_n, base / "ws_n"), verifier=ver_n,
            isolate_workspace=True)
        ref.factory.verifier = ver_f

        def native_sink(event):
            task = next((t for t in native.project.tasks if t.id == event.task),
                        None)
            if task is not None:
                task.attempts.append(event.attempt)
                if event.attempt.outcome == "ok":
                    task.delivered_by = event.attempt.model

        pool_n.attempt_sink = native_sink

        def reference_ask(role, system, user, tag, task_id="", complexity="auto"):
            return pool_f.complete(None, [], {"tag": tag, "task": task_id,
                                              "prompt": user})

        ref.factory._ask = reference_ask
        return ref, native, (base / "ws_f", base / "ws_n")

    def test_factory_visual_rechecks_after_fix(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = base / "proj"; proj.mkdir()
            ws = base / "ws"; ws.mkdir()
            (proj / "index.html").write_text("<html></html>", encoding="utf-8")
            settings = self._settings(base)
            settings.visual_check = True
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
            ref, native, (ws_f, ws_n) = self._pair(base)

            for phase in ("BUILD", "VERIFY", "INTEGRATE", "VISUAL", "SIGNOFF"):
                self.assertTrue(ref.step(phase), phase)
                self.assertTrue(native.step(phase), phase)
            ref.sync_to_project()

            for name in ("app.py", "util.py"):
                self.assertEqual((ws_f / name).read_text(),
                                 (ws_n / name).read_text(), name)
            self.assertEqual([(t.id, t.status) for t in ref.project.tasks],
                             [(t.id, t.status) for t in native.project.tasks])
            for a, b in zip(ref.project.tasks, native.project.tasks):
                self.assertEqual(canon_model(a.delivered_by),
                                 canon_model(b.delivered_by))
                self.assertEqual(canon_attempts(a), canon_attempts(b))
                self.assertEqual(canon_verification(ref.project, a.id),
                                 canon_verification(native.project, b.id))
            self.assertEqual(ref.project.integration.get("verdict"),
                             native.project.integration.get("verdict"))
            self.assertFalse((ws_n / ".factory").exists())

    def test_build_quality_recovery_fail_then_fix(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            ref, native, (ws_f, ws_n) = self._pair(base, fail_first=True)

            self.assertTrue(ref.step("BUILD"))
            self.assertTrue(native.step("BUILD"))
            ref.sync_to_project()

            for name in ("app.py", "util.py"):
                self.assertEqual((ws_f / name).read_text(),
                                 (ws_n / name).read_text(), name)
            for a, b in zip(ref.project.tasks, native.project.tasks):
                self.assertEqual(canon_attempts(a), canon_attempts(b))
                self.assertEqual(canon_verification(ref.project, a.id),
                                 canon_verification(native.project, b.id))

    def test_build_unverified_blocks_both(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            ref, native, (ws_f, ws_n) = self._pair(base, unverified=True)

            self.assertFalse(ref.step("BUILD"))
            self.assertFalse(native.step("BUILD"))
            self.assertFalse((ws_f / "app.py").exists())
            self.assertFalse((ws_n / "app.py").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
