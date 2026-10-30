#!/usr/bin/env python3
"""P0/P1: model identity and credential profiles (no network, no deps)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import registry as r


class IdentityTests(unittest.TestCase):
    def test_unknown_billing_is_never_coerced(self):
        self.assertEqual(r.normalize_billing("nonsense"), r.BILLING_UNKNOWN)
        self.assertEqual(r.normalize_billing(None), r.BILLING_UNKNOWN)
        self.assertEqual(r.ModelRef("p", "e", "m", billing="free").billing,
                         r.BILLING_FREE)

    def test_billing_comes_from_profile_not_name(self):
        gemini = r.ref_for("gemini-plus", "gemini-3.5-flash")
        self.assertEqual(gemini.provider, "google")
        self.assertEqual(gemini.endpoint, "gemini")
        self.assertEqual(gemini.billing, r.BILLING_SUBSCRIPTION)
        argon = r.ref_for("opencode-go", "gemini-4-argon")
        self.assertEqual(argon.billing, r.BILLING_SUBSCRIPTION)
        self.assertEqual(argon.provider, "opencode")

        deepseek_api = r.ref_for("deepseek-api", "deepseek-v4-pro")
        deepseek_go = r.ref_for("opencode-go", "deepseek-v4-pro")
        self.assertNotEqual(deepseek_api, deepseek_go)
        self.assertEqual(deepseek_api.billing, r.BILLING_PAYG)
        self.assertEqual(deepseek_go.billing, r.BILLING_SUBSCRIPTION)

    def test_openrouter_free_vs_paid(self):
        free = r.ref_for("openrouter", "x/y:free", billing=r.BILLING_FREE)
        paid = r.ref_for("openrouter", "openai/gpt-4o", billing=r.BILLING_PAYG)
        self.assertEqual(free.billing, r.BILLING_FREE)
        self.assertEqual(paid.billing, r.BILLING_PAYG)

    def test_unknown_profile_and_ref(self):
        ref = r.unknown_ref()
        self.assertEqual(ref.billing, r.BILLING_UNKNOWN)
        self.assertEqual(ref.credential_profile, "unknown")
        self.assertEqual(r.profile("does-not-exist").billing, r.BILLING_UNKNOWN)

    def test_ref_from_handle_keeps_requested_vs_served(self):
        class Handle:
            _model_ref = r.ref_for("opencode-go", "kimi-k3.1-pro")

        served = r.ref_from_handle(Handle(), served_id="kimi-k3.1-pro-20260101")
        self.assertEqual(served.model_id, "kimi-k3.1-pro-20260101")
        self.assertEqual(served.requested_id, "kimi-k3.1-pro")
        self.assertEqual(served.billing, r.BILLING_SUBSCRIPTION)

    def test_family(self):
        self.assertEqual(r.family_of("gemini-3.1-pro"), "gemini")
        self.assertEqual(r.family_of("deepseek-v4-pro"), "deepseek")
        self.assertEqual(r.family_of("kimi-k3.1-pro"), "kimi")
        self.assertEqual(r.family_of("nvidia/nemotron-3-ultra"), "nvidia")


if __name__ == "__main__":
    unittest.main(verbosity=2)
