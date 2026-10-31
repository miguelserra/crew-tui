#!/usr/bin/env python3
"""P5: critic validity, delivery gate and UNVERIFIED semantics."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import verify as v


def critic(verdict="PASS", objections=None, pf="gemini", cf="kimi",
           known=True, identity=True):
    return {"verdict": verdict, "objections": objections or [],
            "producer_family": pf, "critic_family": cf,
            "producer_known": known,
            "critic": {"model_id": "critic"} if identity else None}


class CriticTests(unittest.TestCase):
    def test_same_family_is_invalid(self):
        self.assertFalse(v.critic_is_valid("gemini", "gemini"))
        self.assertTrue(v.critic_is_valid("opencode", "gemini"))
        self.assertFalse(v.critic_is_valid("gemini", "gemini", producer_known=False))

    def test_same_family_cannot_pass(self):
        d = v.decide_build(True, critic(cf="gemini"))
        self.assertEqual(d.verdict, v.UNVERIFIED)
        self.assertFalse(d.deliver)

    def test_unavailable_is_unverified_not_pass(self):
        d = v.decide_build(True, critic(verdict="UNAVAILABLE", identity=False))
        self.assertEqual(d.verdict, v.UNVERIFIED)
        self.assertFalse(d.deliver)

    def test_allow_unverified_is_explicit(self):
        d = v.decide_build(True, critic(verdict="UNAVAILABLE", identity=False),
                           allow_unverified=True)
        self.assertEqual(d.verdict, v.UNVERIFIED)
        self.assertTrue(d.deliver)


class BuildGateTests(unittest.TestCase):
    def test_pass_requires_oracle_and_critic(self):
        self.assertTrue(v.decide_build(True, critic()).deliver)
        self.assertFalse(v.decide_build(False, critic()).deliver)
        self.assertFalse(v.decide_build(True, critic(verdict="FAIL")).deliver)

    def test_visual_na_vs_unverified(self):
        d = v.decide_build(True, critic(), visual={"verdict": v.NA})
        self.assertTrue(d.deliver)
        d = v.decide_build(True, critic(), visual={"verdict": v.UNVERIFIED})
        self.assertEqual(d.verdict, v.UNVERIFIED)
        self.assertFalse(d.deliver)
        self.assertTrue(v.decide_build(True, critic(),
                                       visual={"verdict": v.UNVERIFIED},
                                       allow_unverified=True).deliver)

    def test_visual_fail_blocks(self):
        d = v.decide_build(True, critic(), visual={"verdict": v.FAIL})
        self.assertEqual(d.verdict, v.FAIL)
        self.assertFalse(d.deliver)

    def test_local_advisory_failure_does_not_block(self):
        d = v.decide_build(True, critic(),
                           tests={"verdict": "FAIL", "advisory": True})
        self.assertTrue(d.deliver)
        d = v.decide_build(True, critic(),
                           tests={"verdict": "FAIL", "advisory": False})
        self.assertFalse(d.deliver)


class ReviewGateTests(unittest.TestCase):
    def test_review_with_objections_is_delivered(self):
        d = v.decide_review(critic(verdict="FAIL", objections=[{"claim": "x"}]))
        self.assertEqual(d.verdict, v.FAIL)
        self.assertTrue(d.deliver)

    def test_review_without_critic_is_unverified(self):
        d = v.decide_review(critic(verdict="UNAVAILABLE", identity=False))
        self.assertEqual(d.verdict, v.UNVERIFIED)
        self.assertFalse(d.deliver)


if __name__ == "__main__":
    unittest.main(verbosity=2)
