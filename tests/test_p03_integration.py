#!/usr/bin/env python3
"""P3 dependency-backed checks: telemetry, dashboard and probes use Registry."""

import sys
import types
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import main
    import registry as reg
except Exception as exc:  # pragma: no cover
    DEPS_OK = False
    DEPS_ERR = str(exc)
else:
    DEPS_OK = True
    DEPS_ERR = ""


class FakeTelemetry:
    def __init__(self):
        self.events = []

    def record(self, run_id, event, **fields):
        self.events.append((run_id, event, fields))


@unittest.skipUnless(DEPS_OK, f"runtime deps missing: {DEPS_ERR}")
class TelemetryRegistryTests(unittest.TestCase):
    def test_record_usage_uses_registry_price_and_billing(self):
        reg_ = reg.ModelRegistry()
        pool = object.__new__(main.LLMPool)
        pool.registry = reg_
        pool.telemetry = FakeTelemetry()
        col = main.AttemptCollector(run_id="r", task="t1", tag="DEV")
        ref = reg_.ref_for_identity(
            reg_.identity_for("deepseek-api", "deepseek-v4-pro"))
        aid = col.start(ref)
        col.finish(aid, "ok", served_id="deepseek-v4-pro")
        token = main._ATTEMPT_CTX.set(col)
        try:
            response = types.SimpleNamespace(
                usage_metadata={"input_tokens": 1_000_000,
                                "output_tokens": 1_000_000},
                response_metadata={"model_name": "deepseek-v4-pro"})
            main.LLMPool._record_usage(
                pool, {"run": "r", "task": "t1", "tag": "DEV"},
                response, "deepseek-v4-pro", 10)
        finally:
            main._ATTEMPT_CTX.reset(token)
        _run, event, fields = pool.telemetry.events[0]
        self.assertEqual(event, "llm_call")
        self.assertEqual(fields["billing"], "payg")
        self.assertEqual(fields["cost"], "2.80")
        self.assertEqual(fields["cost_basis"], "payg")
        self.assertTrue(fields["billable"])
        self.assertFalse(fields["quota_consumed"])
        self.assertEqual(fields["identity"]["price"]["input_per_million"], "0.60")

    def test_subscription_records_quota_not_payg_cost(self):
        reg_ = reg.ModelRegistry()
        pool = object.__new__(main.LLMPool)
        pool.registry = reg_
        pool.telemetry = FakeTelemetry()
        col = main.AttemptCollector(run_id="r", task="t1", tag="DEV")
        ref = reg_.ref_for_identity(
            reg_.identity_for("opencode-go", "kimi-k3.1-pro"))
        aid = col.start(ref)
        col.finish(aid, "ok")
        token = main._ATTEMPT_CTX.set(col)
        try:
            response = types.SimpleNamespace(
                usage_metadata={"input_tokens": 100, "output_tokens": 50},
                response_metadata={"model_name": "kimi-k3.1-pro"})
            main.LLMPool._record_usage(
                pool, {"run": "r", "task": "t1", "tag": "DEV"},
                response, "kimi-k3.1-pro", 5)
        finally:
            main._ATTEMPT_CTX.reset(token)
        fields = pool.telemetry.events[0][2]
        self.assertEqual(fields["billing"], "subscription")
        self.assertEqual(fields["cost"], "0")
        self.assertFalse(fields["billable"])
        self.assertTrue(fields["quota_consumed"])


@unittest.skipUnless(DEPS_OK, f"runtime deps missing: {DEPS_ERR}")
class DashboardBillingTests(unittest.TestCase):
    def test_dashboard_separates_payg_cost_from_quota(self):
        st = main.DashboardState(main.Settings())
        st.on_event({"event": "llm_call", "tag": "DEV",
                     "billing": "payg", "cost": "2.80", "billable": True,
                     "cost_known": True, "in_tokens": 1_000_000,
                     "out_tokens": 1_000_000, "model": "deepseek-v4-pro"})
        st.on_event({"event": "llm_call", "tag": "GEMINI",
                     "billing": "subscription", "cost": "0", "billable": False,
                     "quota_consumed": True, "in_tokens": 10,
                     "out_tokens": 5, "model": "gemini-3.5-flash"})
        self.assertEqual(st.cost, Decimal("2.80"))
        self.assertEqual(st.tier_cost["payg"], Decimal("2.80"))
        self.assertFalse(st.tier_cost["subscription"])
        self.assertEqual(st.tier_tokens["subscription"], 15)
        self.assertEqual(st.tier_tokens["payg"], 2_000_000)

    def test_legacy_event_tier_is_mapped(self):
        self.assertEqual(main._event_billing({"tier": "gemini"}), "subscription")
        self.assertEqual(main._event_billing({"tier": "free"}), "free_quota")
        self.assertEqual(main._event_billing({"tier": "paid"}), "payg")


@unittest.skipUnless(DEPS_OK, f"runtime deps missing: {DEPS_ERR}")
class ProbeAndPoolTests(unittest.TestCase):
    def test_pool_loads_registry_from_models_toml(self):
        pool = main.LLMPool(main.Settings(),
                            sink_factory=lambda tag: (lambda r: None))
        self.assertIn("models.toml", pool.registry.config_source)
        ident = pool.registry.identity_for("opencode-go", "kimi-k3.1-pro")
        self.assertTrue(ident.defined)
        self.assertEqual(ident.billing, "subscription")

    def test_active_handles_have_registered_identity(self):
        import engine
        settings = main.Settings(
            opencode_api_key="x", opencode_mode="primary",
            gemini_api_key="x", openrouter_api_key="x", deepseek_api_key="x")
        settings.keep_awake = False
        settings.sleep_offpeak = False
        pool = main.LLMPool(settings, sink_factory=lambda tag: (lambda r: None))
        handles = [pool.manager, pool.architect, pool.fast_cloud, pool.worker,
                   pool.reviewer, pool.senior_dev, pool.gemini, pool.escalate]
        seen = 0
        for handle in handles:
            for ref in engine.iter_model_refs(handle):
                seen += 1
                self.assertTrue(ref.identity, ref)
                ident = reg.ModelIdentity.from_dict(ref.identity)
                self.assertTrue(ident.defined, ref)
                self.assertNotEqual(ident.billing, reg.BILLING_UNKNOWN, ref)
        self.assertGreater(seen, 0)

    def test_big_pickle_probe_records_availability(self):
        class Fake:
            def invoke(self, messages):
                return types.SimpleNamespace(content="OK")

        pool = object.__new__(main.LLMPool)
        pool.settings = types.SimpleNamespace(senior_probe=True,
                                              senior_model="big-pickle")
        pool.cloud_enabled = True
        pool.registry = reg.ModelRegistry()
        pool.allow_unknown = False
        pool._free_cloud = lambda *a, **k: Fake()
        self.assertTrue(main.LLMPool._probe_senior(pool))

        class Boom:
            def invoke(self, messages):
                raise RuntimeError("429")

        pool._free_cloud = lambda *a, **k: Boom()
        self.assertFalse(main.LLMPool._probe_senior(pool))


if __name__ == "__main__":
    unittest.main(verbosity=2)
