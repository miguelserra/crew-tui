#!/usr/bin/env python3
"""P6: workflow profile resolution and phase selection."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import profiles as p


class ProfileTests(unittest.TestCase):
    def test_explicit_profiles_are_kept(self):
        for name in ("review", "script", "library", "web"):
            sel = p.resolve_profile(name)
            self.assertEqual(sel.resolved, name)

    def test_auto_web_from_targets(self):
        tasks = [type("T", (), {"target_file": "index.html", "title": "",
                                "description": ""})()]
        sel = p.resolve_profile("auto", tasks=tasks)
        self.assertEqual(sel.resolved, p.WEB)

    def test_auto_web_from_requirements(self):
        sel = p.resolve_profile("auto", goal="build a browser UI")
        self.assertEqual(sel.resolved, p.WEB)

    def test_auto_library(self):
        sel = p.resolve_profile("auto", goal="publish a public API library")
        self.assertEqual(sel.resolved, p.LIBRARY)

    def test_auto_script_default(self):
        sel = p.resolve_profile("auto", goal="a small utility")
        self.assertEqual(sel.resolved, p.SCRIPT)

    def test_phase_sets(self):
        self.assertNotIn("VISUAL", p.phases_for(p.SCRIPT))
        self.assertIn("VISUAL", p.phases_for(p.WEB))
        self.assertEqual(p.phases_for(p.REVIEW)[:2], ("SPEC", "VERIFY"))

    def test_selection_serializes(self):
        sel = p.resolve_profile("auto", goal="browser ui")
        d = sel.to_dict()
        self.assertEqual(d["requested"], "auto")
        self.assertEqual(d["resolved"], "web")
        self.assertEqual(d["profile_version"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
