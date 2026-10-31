#!/usr/bin/env python3
"""P5 dependency-backed checks: critic fallback and visual capability gate."""

import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import main
    import registry as reg
    import verify as v
except Exception as exc:  # pragma: no cover
    DEPS_OK = False
    DEPS_ERR = str(exc)
else:
    DEPS_OK = True
    DEPS_ERR = ""


class FakePool:
    def __init__(self, critics, responses):
        self._critics = critics
        self._responses = responses
        self.calls = []
        self.last_attempt_model = {}

    def critic_candidates(self, producer):
        return list(self._critics)

    def complete(self, handle, messages, meta=None):
        self.calls.append(handle)
        value = self._responses.get(handle)
        if isinstance(value, Exception):
            raise value
        return value


@unittest.skipUnless(DEPS_OK, f"runtime deps missing: {DEPS_ERR}")
class CriticFallbackTests(unittest.TestCase):
    def _verifier(self, critics, responses):
        pool = FakePool(critics, responses)
        settings = types.SimpleNamespace(visual_check=False, test_oracle=False,
                                         allow_unverified=False)
        verifier = main.Verifier(settings, pool, lambda *a: None, None)
        return verifier, pool

    def test_same_family_candidate_is_skipped_even_after_failure(self):
        reg_ = reg.ModelRegistry()
        producer = reg_.ref_for_identity(
            reg_.identity_for("gemini-plus", "gemini-3.5-flash"))
        same = reg_.ref_for_identity(
            reg_.identity_for("gemini-plus", "gemini-3.1-pro-preview"))
        other = reg_.ref_for_identity(
            reg_.identity_for("opencode-go", "kimi-k3.1-pro"))
        bad = object()
        good = object()
        verifier, pool = self._verifier(
            [(bad, same), (good, other)],
            {bad: RuntimeError("timeout"), good: '{"verdict":"PASS","objections":[]}'})
        task = types.SimpleNamespace(id="t1", title="x", description="",
                                     target_file="a.py", acceptance=["ok"])
        result = verifier.run_critic(producer, task, "code", "r")
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["critic_family"], "kimi")
        self.assertEqual(pool.calls, [good])

    def test_only_same_family_is_unavailable(self):
        reg_ = reg.ModelRegistry()
        producer = reg_.ref_for_identity(
            reg_.identity_for("gemini-plus", "gemini-3.5-flash"))
        same = reg_.ref_for_identity(
            reg_.identity_for("gemini-plus", "gemini-3.1-pro-preview"))
        verifier, pool = self._verifier([(object(), same)], {})
        task = types.SimpleNamespace(id="t1", title="x", description="",
                                     target_file="a.py", acceptance=["ok"])
        result = verifier.run_critic(producer, task, "code", "r")
        self.assertEqual(result["verdict"], "UNAVAILABLE")
        self.assertEqual(pool.calls, [])


@unittest.skipUnless(DEPS_OK, f"runtime deps missing: {DEPS_ERR}")
class VisualGateTests(unittest.TestCase):
    def _verifier(self, route_result):
        pool = types.SimpleNamespace(
            router=types.SimpleNamespace(route=lambda request: route_result))
        settings = types.SimpleNamespace(visual_check=True, test_oracle=False,
                                         allow_unverified=False)
        return main.Verifier(settings, pool, lambda *a: None, None), pool

    def test_no_vision_model_is_unverified(self):
        with tempfile.TemporaryDirectory() as td:
            html = Path(td) / "index.html"
            html.write_text("<html></html>", encoding="utf-8")
            verifier, _ = self._verifier(types.SimpleNamespace(candidates=[]))
            verifier.screenshot = staticmethod(lambda *a, **k: True)
            task = types.SimpleNamespace(id="t1", title="ui", acceptance=["renders"])
            result = verifier.run_visual(task, html, "r")
            self.assertEqual(result["verdict"], v.UNVERIFIED)

    def test_no_html_is_na(self):
        verifier, _ = self._verifier(types.SimpleNamespace(candidates=[]))
        task = types.SimpleNamespace(id="t1", title="ui", acceptance=[])
        result = verifier.run_visual(task, None, "r")
        self.assertEqual(result["verdict"], v.NA)


if __name__ == "__main__":
    unittest.main(verbosity=2)
