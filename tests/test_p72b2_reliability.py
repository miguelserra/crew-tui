#!/usr/bin/env python3
"""P7.2b.2 reliability hardening regression tests."""

import hashlib
import json
import sys
import tempfile
import threading
import types
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import registry as reg
import router as r
import store as store_mod
import visual
import state as s


def _payg_identity():
    price = reg.ModelPrice(currency="USD",
                           input_per_million=Decimal("1"),
                           output_per_million=Decimal("1"),
                           source="test", updated="test")
    return reg.ModelIdentity(
        provider="deepseek-api", endpoint="deepseek-api",
        model_id="deepseek-v4-pro", family="deepseek",
        credential_profile="deepseek-api", billing=reg.BILLING_PAYG,
        capabilities=frozenset({"text"}), context_window=100000,
        price=price, defined=True)


class GrantBudgetTests(unittest.TestCase):
    def test_reservations_do_not_authorize_beyond_budget(self):
        ident = _payg_identity()
        req = r.RouteRequest("programming", task_id="t", run_id="r")
        grants = r.GrantStore()
        grants.approve_task(req, {r.identity_key(ident)},
                            max_cost=Decimal("10"))
        first = grants.reserve(req, ident, amount=Decimal("6"))
        self.assertIsNotNone(first)
        second = grants.reserve(req, ident, amount=Decimal("6"))
        self.assertIsNone(second)
        grants.release(first)
        second = grants.reserve(req, ident, amount=Decimal("6"))
        self.assertIsNotNone(second)
        grants.commit(second, reg.CostEstimate(
            "USD", Decimal("6"), Decimal("0"), Decimal("6"), "test", True,
            False, True))
        self.assertIsNone(grants.reserve(req, ident, amount=Decimal("5")))

    def test_release_and_ambiguous_reservation_semantics(self):
        ident = _payg_identity()
        req = r.RouteRequest("programming", task_id="t", run_id="r")
        grants = r.GrantStore()
        grants.approve_task(req, {r.identity_key(ident)},
                            max_cost=Decimal("10"))
        reservation = grants.reserve(req, ident, amount=Decimal("6"))
        self.assertIsNotNone(reservation)
        grants.release(reservation)
        self.assertIsNotNone(grants.reserve(req, ident, amount=Decimal("6")))
        # An ambiguous outcome intentionally leaves the reservation held.
        held = grants.reserve(req, ident, amount=Decimal("4"))
        self.assertIsNotNone(held)
        self.assertIsNone(grants.reserve(req, ident, amount=Decimal("1")))
        grants.release(held)
        self.assertIsNotNone(grants.reserve(req, ident, amount=Decimal("4")))

    def test_compatibility_authorize_reserves_budget(self):
        ident = _payg_identity()
        registry = reg.ModelRegistry(models=[ident])
        registry.estimate = lambda identity, i, o: reg.CostEstimate(
            "USD", Decimal("4"), Decimal("0"), Decimal("4"),
            "test", True, False, True)
        req = r.RouteRequest("programming", complexity="high",
                             task_id="t", run_id="r", max_tokens=100)
        grants = r.GrantStore()
        grants.approve_task(req, {r.identity_key(ident)},
                            max_cost=Decimal("10"))
        router = r.Router(registry, lambda *a: None, grants=grants)
        ref = registry.ref_for_identity(ident)
        c1 = r.RouteCandidate(ident, ref, None)
        c2 = r.RouteCandidate(ident, ref, None)
        c3 = r.RouteCandidate(ident, ref, None)
        self.assertTrue(router.authorize(req, c1))
        self.assertTrue(router.authorize(req, c2))
        self.assertFalse(router.authorize(req, c3))
        router.charge(req, c1, types.SimpleNamespace(usage_metadata={}))
        self.assertEqual(grants._grants[0].spent, Decimal("4"))

    def test_concurrent_charges_are_not_lost(self):
        ident = _payg_identity()
        req = r.RouteRequest("programming", task_id="t", run_id="r")
        grants = r.GrantStore()
        grant = grants.approve_task(req, {r.identity_key(ident)})
        cost = reg.CostEstimate("USD", Decimal("1"), Decimal("0"),
                                Decimal("1"), "test", True, False, True)
        barrier = threading.Barrier(20)

        def worker():
            barrier.wait()
            grants.charge(req, ident, cost)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(grant.spent, Decimal("20"))

    def test_router_concurrent_calls_respect_budget_and_do_not_overflow(self):
        ident = _payg_identity()
        registry = reg.ModelRegistry(models=[ident])
        registry.estimate = lambda identity, i, o: reg.CostEstimate(
            "USD", Decimal("4"), Decimal("0"), Decimal("4"),
            "test", True, False, True)
        req = r.RouteRequest("programming", complexity="high",
                             task_id="t", run_id="r", max_tokens=100)
        grants = r.GrantStore()
        grants.approve_task(req, {r.identity_key(ident)},
                            max_cost=Decimal("10"))
        calls = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def provider(identity, tag, max_tokens, thinking):
            class Handle:
                def invoke(self, messages):
                    with lock:
                        calls.append(identity.model_id)
                    barrier.wait(timeout=5)
                    return types.SimpleNamespace(
                        content="ok", usage_metadata={},
                        response_metadata={})
            return Handle()

        router = r.Router(registry, provider, grants=grants,
                          breaker=r.CircuitBreaker(),
                          availability=lambda i: True)
        results = []
        def worker():
            try:
                results.append(router.route(req).chain.invoke([]).content)
            except Exception as exc:
                results.append(type(exc).__name__)
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertLessEqual(len(calls), 2)
        self.assertLessEqual(grants._grants[0].spent, Decimal("10"))
        self.assertEqual(grants._grants[0].spent, Decimal("8"))


class VisualReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "index.html").write_text("<html></html>", encoding="utf-8")
        self.old_serve = visual.native_util.serve
        class Server:
            def shutdown(self): pass
            def server_close(self): pass
        visual.native_util.serve = lambda root: (Server(), 1234)

    def tearDown(self):
        visual.native_util.serve = self.old_serve
        self.tmp.cleanup()

    def _playwright(self, *, goto_error=None, screenshot_error=None,
                    context_error=None, browser_close_error=None,
                    ctx_closed=None, browser_closed=None):
        mod = types.ModuleType("playwright")
        sync = types.ModuleType("playwright.sync_api")
        closed = {"ctx": 0, "browser": 0}
        class Page:
            def on(self, *a, **k): pass
            def goto(self, *a, **k):
                if goto_error: raise goto_error
            def wait_for_timeout(self, *a, **k): pass
            def screenshot(self, path=None, **k):
                if screenshot_error: raise screenshot_error
                Path(path).write_bytes(b"png")
        class Ctx:
            def new_page(self): return Page()
            def close(self):
                closed["ctx"] += 1
                if ctx_closed is not None: ctx_closed.append(1)
        class Browser:
            def new_context(self, *a, **k):
                if context_error: raise context_error
                return Ctx()
            def close(self):
                closed["browser"] += 1
                if browser_close_error: raise browser_close_error
                if browser_closed is not None: browser_closed.append(1)
        class Chromium:
            def launch(self, *a, **k): return Browser()
        class P:
            chromium = Chromium()
        class Sync:
            def __enter__(self): return P()
            def __exit__(self, *a): return False
        sync.sync_playwright = lambda: Sync()
        mod.sync_api = sync
        return mod, sync, closed

    def _run(self, **kwargs):
        mod, sync, closed = self._playwright(**kwargs)
        old = dict(sys.modules)
        sys.modules["playwright"] = mod
        sys.modules["playwright.sync_api"] = sync
        try:
            report = visual.playwright_audit(self.root, self.root / "logs")
        finally:
            sys.modules.clear()
            sys.modules.update(old)
        return report, closed

    def test_success_closes_browser_and_context(self):
        report, closed = self._run()
        self.assertEqual(report["verdict"], "PASS", report)
        self.assertEqual(closed, {"ctx": 1, "browser": 1})

    def test_navigation_failure_closes_resources(self):
        report, closed = self._run(goto_error=RuntimeError("navigation"))
        self.assertIn("navigation", report["note"])
        self.assertEqual(closed, {"ctx": 1, "browser": 1})

    def test_screenshot_failure_closes_resources(self):
        report, closed = self._run(screenshot_error=RuntimeError("shot"))
        self.assertIn("shot", report["note"])
        self.assertEqual(closed, {"ctx": 1, "browser": 1})

    def test_context_failure_closes_browser(self):
        report, closed = self._run(context_error=RuntimeError("context"))
        self.assertIn("context", report["note"])
        self.assertEqual(closed, {"ctx": 0, "browser": 1})

    def test_browser_close_failure_does_not_mask_original(self):
        report, closed = self._run(goto_error=RuntimeError("navigation"),
                                   browser_close_error=OSError("close"))
        self.assertIn("navigation", report["note"])
        self.assertNotIn("close", report["note"])
        self.assertEqual(closed["browser"], 1)


class ArtifactHashTests(unittest.TestCase):
    def _state(self, root):
        st = s.ProjectState(run_id="r", goal="g", engine_kind=s.ENGINE_UNIFIED,
                            project_root=str(root))
        st.tasks = [s.Task("a", kind="build", target_file="index.html")]
        return st

    def _hash(self, root, data):
        (root / "index.html").write_bytes(data)
        return visual.VisualService._artifact_hashes(self._state(root), root)["index.html"]

    def test_hash_is_exact_bytes_for_utf8(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = "olá\n".encode("utf-8")
            self.assertEqual(self._hash(root, data),
                             {"v": 2, "sha": hashlib.sha256(data).hexdigest()})

    def test_invalid_utf8_and_binary_are_hashable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = b"\xff\xfe\x00binary"
            self.assertEqual(self._hash(root, data),
                             {"v": 2, "sha": hashlib.sha256(data).hexdigest()})

    def test_different_bytes_do_not_collapse_after_decoding(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = self._hash(root, b"\xff")
            b = self._hash(root, b"\xfe")
            self.assertNotEqual(a["sha"], b["sha"])

    def test_empty_file_is_hashed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(self._hash(root, b""),
                             {"v": 2, "sha": hashlib.sha256(b"").hexdigest()})


class BreakerPersistenceTests(unittest.TestCase):
    def test_persistence_failure_does_not_abort_and_degrades(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "breaker.json"
            breaker = r.CircuitBreaker(path)
            ident = _payg_identity()
            original = r.os.replace
            def fail(src, dst):
                raise PermissionError("busy")
            r.os.replace = fail
            try:
                with self.assertLogs("crew_tui.router", level="WARNING"):
                    breaker.record_success(ident)
            finally:
                r.os.replace = original
            self.assertTrue(breaker.persistence_degraded)
            self.assertTrue(breaker.allow(ident))
            breaker.record_success(ident)
            self.assertFalse(breaker.persistence_degraded)
            self.assertTrue(path.is_file())


class AtomicReplaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_windows_transient_permission_error_retries(self):
        tmp = self.root / "tmp"; tmp.write_text("x")
        dst = self.root / "dst"
        original = store_mod.os.replace
        calls = {"n": 0}
        sleeps = []
        def flaky(src, target):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError("busy")
            original(src, target)
        store_mod.os.replace = flaky
        try:
            store_mod._replace_with_retry(tmp, dst, platform="nt",
                                          sleep=sleeps.append,
                                          initial_delay=0.01)
        finally:
            store_mod.os.replace = original
        self.assertEqual(calls["n"], 3)
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(dst.is_file())

    def test_windows_exhausted_retry_propagates(self):
        tmp = self.root / "tmp"; tmp.write_text("x")
        dst = self.root / "dst"
        original = store_mod.os.replace
        def fail(src, target):
            raise PermissionError("busy")
        store_mod.os.replace = fail
        try:
            with self.assertRaises(PermissionError):
                store_mod._replace_with_retry(tmp, dst, platform="nt",
                                              sleep=lambda _: None,
                                              attempts=3,
                                              initial_delay=0.01)
        finally:
            store_mod.os.replace = original

    def test_non_permission_error_does_not_retry(self):
        tmp = self.root / "tmp"; tmp.write_text("x")
        dst = self.root / "dst"
        original = store_mod.os.replace
        calls = {"n": 0}
        def fail(src, target):
            calls["n"] += 1
            raise OSError("io")
        store_mod.os.replace = fail
        try:
            with self.assertRaises(OSError):
                store_mod._replace_with_retry(tmp, dst, platform="nt",
                                              sleep=lambda _: None,
                                              attempts=4,
                                              initial_delay=0.01)
        finally:
            store_mod.os.replace = original
        self.assertEqual(calls["n"], 1)

    def test_non_windows_permission_error_does_not_retry(self):
        tmp = self.root / "tmp"; tmp.write_text("x")
        dst = self.root / "dst"
        original = store_mod.os.replace
        calls = {"n": 0}
        def fail(src, target):
            calls["n"] += 1
            raise PermissionError("busy")
        store_mod.os.replace = fail
        try:
            with self.assertRaises(PermissionError):
                store_mod._replace_with_retry(tmp, dst, platform="posix",
                                              sleep=lambda _: None,
                                              attempts=4,
                                              initial_delay=0.01)
        finally:
            store_mod.os.replace = original
        self.assertEqual(calls["n"], 1)

    def test_atomic_write_cleans_temp_on_failure(self):
        path = self.root / "state.json"
        original = store_mod.os.replace
        store_mod.os.replace = lambda src, dst: (_ for _ in ()).throw(
            PermissionError("busy"))
        try:
            with self.assertRaises(PermissionError):
                store_mod.atomic_write_json(path, {"a": 1})
        finally:
            store_mod.os.replace = original
        self.assertFalse(list(self.root.glob(".state.json.*.tmp")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
