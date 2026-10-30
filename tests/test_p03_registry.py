#!/usr/bin/env python3
"""P3: model identity, billing, capabilities, Decimal pricing and snapshots."""

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import registry as r

try:
    import tomllib  # type: ignore
    TOML_OK = True
except Exception:
    try:
        import tomli  # type: ignore
        TOML_OK = True
    except Exception:
        TOML_OK = False


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.reg = r.ModelRegistry()

    def test_identity_key_is_provider_endpoint_model_profile(self):
        go = self.reg.identity_for("opencode-go", "deepseek-v4-pro")
        api = self.reg.identity_for("deepseek-api", "deepseek-v4-pro")
        self.assertEqual(go.model_id, api.model_id)
        self.assertNotEqual(go.key, api.key)
        self.assertNotEqual(go.billing, api.billing)
        self.assertEqual(go.billing, r.BILLING_SUBSCRIPTION)
        self.assertEqual(api.billing, r.BILLING_PAYG)

    def test_all_billing_classes(self):
        self.assertEqual(self.reg.identity_for("local", "qwen3.6-35b-a3b").billing,
                         r.BILLING_LOCAL)
        self.assertEqual(self.reg.identity_for("opencode-free", "big-pickle").billing,
                         r.BILLING_FREE)
        self.assertEqual(self.reg.identity_for("gemini-plus", "gemini-3.5-flash").billing,
                         r.BILLING_SUBSCRIPTION)
        self.assertEqual(self.reg.identity_for("deepseek-api", "deepseek-v4-pro").billing,
                         r.BILLING_PAYG)
        unknown = self.reg.identity_for("opencode-go", "not-registered")
        self.assertEqual(unknown.billing, r.BILLING_UNKNOWN)
        self.assertFalse(unknown.defined)

    def test_capabilities_are_explicit(self):
        flash = self.reg.identity_for("gemini-plus", "gemini-3.5-flash")
        self.assertIn("vision", flash.capabilities)
        deepseek = self.reg.identity_for("deepseek-api", "deepseek-v4-pro")
        self.assertNotIn("vision", deepseek.capabilities)
        unknown = self.reg.identity_for("opencode-go", "not-registered")
        self.assertEqual(unknown.capabilities, frozenset())

    def test_subscription_is_not_zero_cost_payg(self):
        identity = self.reg.identity_for("opencode-go", "kimi-k3.1-pro")
        cost = self.reg.estimate(identity, 1_000_000, 1_000_000)
        self.assertEqual(cost.total, Decimal("0"))
        self.assertFalse(cost.billable)
        self.assertTrue(cost.quota_consumed)
        self.assertEqual(cost.basis, "subscription")
        self.assertTrue(cost.known)

    def test_payg_uses_decimal_prices(self):
        identity = self.reg.identity_for("deepseek-api", "deepseek-v4-pro")
        cost = self.reg.estimate(identity, 1_000_000, 1_000_000)
        self.assertEqual(cost.total, Decimal("2.80"))
        self.assertTrue(cost.billable)
        self.assertTrue(cost.known)
        self.assertIsInstance(identity.price.input_per_million, Decimal)
        self.assertIsInstance(identity.price.output_per_million, Decimal)

    def test_unknown_cost_is_not_known(self):
        identity = self.reg.identity_for("opencode-go", "not-registered")
        cost = self.reg.estimate(identity, 10, 10)
        self.assertFalse(cost.known)
        self.assertFalse(cost.billable)

    def test_snapshot_lives_in_model_ref(self):
        identity = self.reg.identity_for("deepseek-api", "deepseek-v4-pro")
        ref = self.reg.ref_for_identity(identity)
        snap = r.ModelIdentity.from_dict(ref.identity)
        self.assertEqual(snap.billing, r.BILLING_PAYG)
        self.assertEqual(snap.price.input_per_million, Decimal("0.60"))
        self.assertIn("json", snap.capabilities)
        again = r.ModelRef.from_dict(ref.to_dict())
        self.assertEqual(again.identity["price"]["input_per_million"], "0.60")


class OpenRouterTests(unittest.TestCase):
    def setUp(self):
        self.reg = r.ModelRegistry()

    def test_free_only_when_both_prices_zero(self):
        free = self.reg.openrouter_identity(
            "nvidia/nemotron-3-ultra", {"prompt": "0", "completion": "0"})
        self.assertEqual(free.billing, r.BILLING_FREE)
        self.assertEqual(free.price.input_per_million, Decimal("0"))
        paid = self.reg.openrouter_identity(
            "nvidia/nemotron-3-ultra", {"prompt": "0.000001", "completion": "0"})
        self.assertEqual(paid.billing, r.BILLING_PAYG)
        self.assertEqual(paid.price.input_per_million, Decimal("1"))

    def test_unregistered_openrouter_model_stays_unknown(self):
        ident = self.reg.openrouter_identity("vendor/new", {"prompt": "0", "completion": "0"})
        self.assertEqual(ident.billing, r.BILLING_UNKNOWN)
        self.assertFalse(ident.defined)


@unittest.skipUnless(TOML_OK, "tomllib/tomli not available")
class TomlRegistryTests(unittest.TestCase):
    def test_repository_models_toml_loads(self):
        path = Path(__file__).resolve().parents[1] / "models.toml"
        reg = r.ModelRegistry.load_toml(path)
        self.assertGreaterEqual(len(reg.models), 10)
        api = reg.identity_for("deepseek-api", "deepseek-v4-pro")
        self.assertEqual(api.billing, r.BILLING_PAYG)
        self.assertEqual(api.price.input_per_million, Decimal("0.60"))
        gem = reg.identity_for("gemini-plus", "gemini-3.5-flash")
        self.assertEqual(gem.billing, r.BILLING_SUBSCRIPTION)
        self.assertIn("vision", gem.capabilities)

    def test_load_default_uses_toml_when_available(self):
        reg = r.ModelRegistry.load_default()
        self.assertIn("models.toml", reg.config_source)
        self.assertTrue(reg.identity_for("opencode-go", "kimi-k3.1-pro").defined)


if __name__ == "__main__":
    unittest.main(verbosity=2)
