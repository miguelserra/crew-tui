#!/usr/bin/env python3
"""P1 integration checks that need the runtime dependencies.

No API keys and no network calls are used. Known later-phase defects are
asserted against the target semantics with ``expectedFailure``.
"""

import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import main
    import factory
    from registry import ModelRef, ref_for
    from state import StateCorruptError, TaskAttempt
except Exception as exc:  # pragma: no cover - CI without deps
    DEPS_OK = False
    DEPS_ERR = str(exc)
else:
    DEPS_OK = True
    DEPS_ERR = ""


@unittest.skipUnless(DEPS_OK, f"runtime deps missing: {DEPS_ERR}")
class AttemptIntegrationTests(unittest.TestCase):
    def test_handler_records_per_candidate_attempts(self):
        ref = ref_for("gemini-plus", "gemini-flash")
        col = main.AttemptCollector(run_id="r", task="t1", tag="DEV")
        token = main._ATTEMPT_CTX.set(col)
        try:
            h = main.AttemptHandler(ref)
            h.on_llm_start(None, None, run_id="a")
            h.on_llm_error(RuntimeError("429 rate limit"), run_id="a")
            h2 = main.AttemptHandler(ref_for("opencode-go", "kimi-k3.1-pro"))
            h2.on_llm_start(None, None, run_id="b")
            h2.on_llm_end(types.SimpleNamespace(
                llm_output={"model_name": "kimi-k3.1-pro-2026"},
                generations=[]), run_id="b")
        finally:
            main._ATTEMPT_CTX.reset(token)
        events = col.events()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].attempt.outcome, "429/quota")
        self.assertEqual(events[1].attempt.outcome, "ok")
        self.assertEqual(col.last_ok("t1").attempt.model.model_id,
                         "kimi-k3.1-pro-2026")

    def test_real_fallback_chain_records_each_candidate(self):
        from langchain_core.language_models.fake_chat_models import FakeListChatModel

        class Boom(FakeListChatModel):
            def _call(self, messages, stop=None, run_manager=None, **kwargs):
                raise RuntimeError("429 rate limit")

        col = main.AttemptCollector(run_id="r", task="t1", tag="DEV")
        h1 = main.AttemptHandler(ref_for("gemini-plus", "flash"))
        h2 = main.AttemptHandler(ref_for("opencode-go", "kimi-k3.1-pro"))
        m1 = Boom(responses=["x"], callbacks=[h1])
        m2 = FakeListChatModel(responses=["ok"], callbacks=[h2])
        chain = m1.with_fallbacks([m2])
        token = main._ATTEMPT_CTX.set(col)
        try:
            self.assertEqual(chain.invoke("hi").content, "ok")
        finally:
            main._ATTEMPT_CTX.reset(token)
        self.assertEqual([(e.attempt.outcome, e.attempt.model.model_id)
                          for e in col.events()],
                         [("429/quota", "flash"), ("ok", "kimi-k3.1-pro")])
        self.assertEqual(col.last_ok("t1").attempt.model.model_id,
                         "kimi-k3.1-pro")

    def test_complete_synthesizes_attempt_without_callbacks(self):
        seen = []
        pool = object.__new__(main.LLMPool)
        pool.settings = types.SimpleNamespace()
        pool.allow_paid = True
        pool.dispatch_guard = None
        pool.attempt_sink = seen.append
        pool.telemetry = None
        pool._local_lock = __import__("threading").Lock()
        pool.worker = None
        pool.budget_events = []
        pool.on_budget = None

        class FakeLLM:
            _model_ref = ref_for("opencode-go", "kimi-k3.1-pro")
            _billing = "subscription"
            model_name = "kimi"

            def invoke(self, messages):
                return types.SimpleNamespace(content="ok", usage_metadata={},
                                             response_metadata={})

        self.assertEqual(main.LLMPool.complete(pool, FakeLLM(), []), "ok")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].attempt.outcome, "ok")
        self.assertEqual(seen[0].attempt.model.model_id, "kimi-k3.1-pro")

    def test_unknown_billing_blocks_before_invoke(self):
        from registry import unknown_ref

        def make_pool(allow):
            pool = object.__new__(main.LLMPool)
            pool.settings = types.SimpleNamespace()
            pool.allow_paid = True
            pool.allow_unknown = allow
            pool.dispatch_guard = None
            pool.attempt_sink = None
            pool.telemetry = None
            pool._local_lock = __import__("threading").Lock()
            pool.worker = None
            pool.budget_events = []
            pool.on_budget = None
            return pool

        class H:
            _model_ref = unknown_ref()

            def invoke(self, messages):
                raise AssertionError("must not execute")

        with self.assertRaises(main.UnknownBilling):
            main.LLMPool.complete(make_pool(False), H(), [])

        class OK(H):
            def invoke(self, messages):
                return types.SimpleNamespace(content="ok", usage_metadata={},
                                             response_metadata={})

        self.assertEqual(main.LLMPool.complete(make_pool(True), OK(), []), "ok")

    def test_iter_model_refs_walks_fallback_chain(self):
        class H:
            def __init__(self, ref):
                self._model_ref = ref

        class Chain:
            runnable = H(ref_for("gemini-plus", "flash"))
            fallbacks = [H(ref_for("opencode-go", "kimi-k3.1-pro"))]

        refs = main.iter_model_refs(Chain())
        self.assertEqual({r.model_id for r in refs}, {"flash", "kimi-k3.1-pro"})


@unittest.skipUnless(DEPS_OK, f"runtime deps missing: {DEPS_ERR}")
class FactoryStateIntegrationTests(unittest.TestCase):
    def test_factory_state_carries_engine_metadata(self):
        fs = factory.FactoryState(project="/tmp/x")
        data = fs.to_dict()
        self.assertEqual(data["engine_kind"], "legacy")
        self.assertEqual(data["engine_version"], "legacy")
        self.assertEqual(data["schema_version"], 1)

    def test_task_attempt_log_round_trip(self):
        ts = factory.TaskState(id="t1", title="x", description="",
                               target_file="a.py")
        ts.attempt_log.append({"attempt_id": "a", "outcome": "ok",
                               "model": {"model_id": "kimi"}})
        again = factory.TaskState.from_dict(ts.to_dict())
        self.assertEqual(again.attempt_log[0]["outcome"], "ok")

    def test_factory_load_corrupt_raises_and_preserves(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{broken", encoding="utf-8")
            obj = factory.Factory.__new__(factory.Factory)
            obj.state_path = path
            obj.root = Path(tmp)
            obj.emit = lambda *a, **k: None
            with self.assertRaises(StateCorruptError):
                obj._load()
            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")


@unittest.skipUnless(DEPS_OK, f"runtime deps missing: {DEPS_ERR}")
class KnownLaterDefectsTests(unittest.TestCase):
    """Target semantics for P4/P5; expected to fail until then."""

    def test_hard_tasks_start_complex_chain(self):
        import router as route_mod
        import registry as reg_mod

        class H:
            def invoke(self, messages):
                return types.SimpleNamespace(content="ok", usage_metadata={})

        registry = reg_mod.ModelRegistry()
        handles = {m.model_id: H() for m in registry.models.values()}
        rt = route_mod.Router(
            registry,
            lambda identity, tag, max_tokens, thinking: handles.get(identity.model_id),
            availability=lambda identity: None,
        )
        pool = object.__new__(main.LLMPool)
        pool.router = rt
        pool.local_up = False
        pool.settings = types.SimpleNamespace(hard_task_tokens=10,
                                              local_task_tokens=10)
        pool._task_is_hard = types.MethodType(main.LLMPool._task_is_hard, pool)
        pool._task_is_small = types.MethodType(main.LLMPool._task_is_small, pool)
        pool._route_complexity = types.MethodType(main.LLMPool._route_complexity, pool)
        task = types.SimpleNamespace(
            description="x" * 200, acceptance=["a", "b", "c", "d"],
            team="programming", target_file="a.py", review=False,
            complexity="auto", id="t1")
        llm, _tag = main.LLMPool.team_llm(pool, task)
        self.assertIsNotNone(llm)
        first = llm.result.candidates[0].identity.model_id
        self.assertEqual(first, "gemini-3.1-pro-preview")

    def _fake_orchestrator(self, critic, test_oracle=None):
        obj = main.Orchestrator.__new__(main.Orchestrator)
        obj.settings = types.SimpleNamespace(visual_check=False, test_oracle=True)
        obj.spec = types.SimpleNamespace(review=False)
        obj.emit = lambda *a, **k: None
        obj._record = lambda *a, **k: None
        obj._target_for = lambda task: getattr(task, "target_file", "")
        obj.project_root = None
        obj.verifier = types.SimpleNamespace(
            run_oracle=lambda *a, **k: main.OracleResult(True, 0, stdout=""),
            run_critic=lambda *a, **k: critic,
            run_test_oracle=lambda *a, **k: (test_oracle or {"verdict": "SKIP"}),
        )
        return obj

    @unittest.expectedFailure
    def test_critic_unavailable_should_not_pass(self):
        obj = self._fake_orchestrator({"verdict": "UNAVAILABLE", "objections": [],
                                       "note": "no critic"})
        task = types.SimpleNamespace(id="t1", target_file="", acceptance=[],
                                     title="t")
        passed, _feedback, _details = obj._check_artifact(task, "x", "GEMINI", None)
        self.assertFalse(passed)

    @unittest.expectedFailure
    def test_local_llm_test_should_be_advisory(self):
        obj = self._fake_orchestrator({"verdict": "PASS", "objections": []},
                                      {"verdict": "FAIL", "note": "weak test"})
        task = types.SimpleNamespace(id="t1", target_file="a.py",
                                     acceptance=["x"], title="t")
        passed, _feedback, _details = obj._check_artifact(task, "x", "GEMINI", None)
        self.assertTrue(passed)

    @unittest.expectedFailure
    def test_gemini_producer_should_get_cross_family_critic(self):
        pool = object.__new__(main.LLMPool)
        pool.critic_gemini = object()
        pool.critic_cloud = object()
        self.assertIsNot(main.LLMPool.critic_for(pool, "GEMINI"),
                         pool.critic_gemini)


if __name__ == "__main__":
    unittest.main(verbosity=2)
