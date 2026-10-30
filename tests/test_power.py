"""Regression tests for power.py (peak windows, off-peak gate, idle guard).

Run from the repo root with:
    .venv/bin/python tests/test_power.py
"""

import os
import subprocess
import sys
import time
import types
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import power  # noqa: E402

UTC = timezone.utc
W = "01:00-04:00,06:00-10:00"


def t(h, m=0, day=30):
    return datetime(2026, 10, day, h, m, tzinfo=UTC)  # 2026-10-30 is a Friday


def test_windows():
    assert power.parse_windows(W) == [(60, 240), (360, 600)]
    assert power.is_peak_at(t(1, 22), W)
    assert not power.is_peak_at(t(4, 0), W)
    assert power.is_peak_at(t(6, 0), W)
    assert not power.is_peak_at(t(10, 0), W)
    assert not power.is_peak_at(t(2, 0, day=31), W)          # Saturday
    assert power.next_offpeak_start(t(1, 22), W) == t(4, 0)
    assert power.next_offpeak_start(t(9, 59), W) == t(10, 0)
    assert power.next_offpeak_start(t(23, 0, day=29), W) == t(23, 1, day=29)
    assert power.next_offpeak_start(t(2, 0, day=31), W) == t(2, 1, day=31)
    assert power.is_peak_at(t(0, 30), "23:00-01:00") is True    # wraps midnight
    assert power.is_peak_at(t(1, 30), "23:00-01:00") is False
    assert power.next_offpeak_start(t(0, 30), "23:00-01:00") == t(1, 0)


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def make_settings(**kw):
    base = dict(sleep_offpeak=True, peak_windows=W, peak_weekdays_only=True,
                sleep_if_active=False, offpeak_idle_min=5,
                qwen_restart_on_wake=False, local_service="", sleep_helper="",
                local_llm_url="http://127.0.0.1:8081/v1")
    base.update(kw)
    return types.SimpleNamespace(**base)


def run_gate(idle, suspend_advance, **kw):
    clock = FakeClock(t(2, 0))
    calls = []
    wake = t(2, 4)

    def fake_helper(args):
        calls.append(args[0])
        if args[0] == "--suspend":
            clock.now = clock.now + timedelta(minutes=suspend_advance)
        return True

    events = []
    gate = power.OffpeakGate(
        make_settings(**kw), is_peak=lambda: True,
        emit=lambda tag, text: events.append((tag, text)),
        on_pause=lambda ts: calls.append(("pause", ts)),
        on_resume=lambda: calls.append("resume"),
        clock=clock, sleeper=lambda s: setattr(clock, "now", clock.now + timedelta(seconds=s)),
        idle_ms=lambda: idle, next_wake=lambda now: wake, helper=fake_helper)
    gate._wait_network = lambda timeout=45: None
    gate._wait_local = lambda timeout=150: None
    gate.before_step("test")
    return calls, events


def test_gate_suspend():
    calls, _ = run_gate(idle=10 * 60_000, suspend_advance=4)
    assert calls[0][0] == "pause", calls
    assert calls[1:] == ["--arm", "--suspend", "--cancel", "resume"], calls


def test_gate_user_active_no_suspend():
    calls, events = run_gate(idle=1_000, suspend_advance=0)
    assert "--suspend" not in calls, calls
    assert calls[0][0] == "pause" and calls[-2:] == ["--cancel", "resume"], calls
    assert any("sessão ativa" in text for _, text in events), events


def test_gate_sleep_if_active():
    calls, _ = run_gate(idle=1_000, suspend_advance=4, sleep_if_active=True)
    assert "--suspend" in calls and "resume" in calls, calls


def test_gate_early_wake_re_suspends():
    """Waking before off-peak must re-suspend, not resume during peak."""
    clock = FakeClock(t(2, 0))
    wake = t(2, 4)
    calls = []
    suspends = [0]

    def fake_helper(args):
        calls.append(args[0])
        if args[0] == "--suspend":
            suspends[0] += 1
            clock.now = clock.now + timedelta(minutes=1)  # 1 min per wake, never reaches wake
        return True

    gate = power.OffpeakGate(
        make_settings(), is_peak=lambda: True,
        emit=lambda tag, text: None, on_pause=lambda ts: None,
        on_resume=lambda: calls.append("resume"),
        clock=clock, sleeper=lambda s: setattr(clock, "now", clock.now + timedelta(seconds=s)),
        idle_ms=lambda: 10 * 60_000, next_wake=lambda now: wake, helper=fake_helper)
    gate._wait_network = lambda timeout=45: None
    gate._wait_local = lambda timeout=150: None
    gate.before_step("test")
    # 3 suspend attempts, then the gate gives up and waits awake until wake.
    assert suspends[0] == 3, suspends
    assert calls.count("--suspend") == 3
    assert "resume" in calls
    assert calls[-2:] == ["--cancel", "resume"], calls


def test_idle_guard():
    if shutil.which("systemd-inhibit") is None:
        return  # power integration is Linux/systemd-specific
    if os.environ.get("CI") and not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return  # no login session / logind in CI runners
    try:
        probe = subprocess.run(["systemd-inhibit", "--list"], capture_output=True,
                               text=True, timeout=5)
    except Exception:
        return
    if probe.returncode != 0:
        return
    guard = power.IdleGuard()
    guard.acquire()
    time.sleep(0.7)
    guard_pid = guard._proc.pid if guard._proc is not None else -1
    out = subprocess.run(["systemd-inhibit", "--list"], capture_output=True, text=True).stdout
    assert str(guard_pid) in out, out
    guard.release()
    time.sleep(0.3)
    out = subprocess.run(["systemd-inhibit", "--list"], capture_output=True, text=True).stdout
    assert str(guard_pid) not in out, out


def test_gate_arm_failure_waits_awake():
    """If the helper cannot arm (sudo -n refused), stay awake, never suspend."""
    clock = FakeClock(t(2, 0))
    wake = t(2, 4)
    calls = []

    def fake_helper(args):
        calls.append(args[0])
        return False  # simulate missing sudoers rule

    gate = power.OffpeakGate(
        make_settings(), is_peak=lambda: True, emit=lambda tag, text: None,
        on_pause=lambda ts: None, on_resume=lambda: calls.append("resume"),
        clock=clock, sleeper=lambda s: setattr(clock, "now", clock.now + timedelta(seconds=s)),
        idle_ms=lambda: 10 * 60_000, next_wake=lambda now: wake, helper=fake_helper)
    gate._wait_network = lambda timeout=45: None
    gate._wait_local = lambda timeout=150: None
    gate.before_step("test")
    assert calls[0] == "--arm" and "--suspend" not in calls, calls
    assert "resume" in calls
    assert gate._alarm_armed is False


def test_gate_arm_failure_keeps_idle_guard():
    """A failed RTC arm must not release GNOME's idle inhibitor while waiting."""
    class FakeGuard:
        def __init__(self):
            self.depth = 0

        def acquire(self):
            self.depth += 1

        def release(self):
            self.depth -= 1

    clock = FakeClock(t(2, 0))
    wake = t(2, 4)
    guard = FakeGuard()

    def fake_helper(args):
        return False

    def sleep_while_guarded(seconds):
        assert guard.depth == 1, f"idle inhibitor released while waiting: {guard.depth}"
        clock.now = clock.now + timedelta(seconds=seconds)

    gate = power.OffpeakGate(
        make_settings(), is_peak=lambda: True, emit=lambda tag, text: None,
        on_pause=lambda ts: None, on_resume=lambda: None, guard=guard,
        clock=clock, sleeper=sleep_while_guarded,
        idle_ms=lambda: 10 * 60_000, next_wake=lambda now: wake, helper=fake_helper)
    gate._wait_network = lambda timeout=45: None
    gate._wait_local = lambda timeout=150: None
    gate.start()
    gate.before_step("test")
    assert guard.depth == 1
    gate.stop()
    assert guard.depth == 0


def test_cancel_failure_keeps_alarm_marked_armed():
    results = [False, True]
    gate = power.OffpeakGate(make_settings(), is_peak=lambda: False,
                             emit=lambda tag, text: None,
                             helper=lambda args: results.pop(0))
    gate._alarm_armed = True
    assert gate._cancel_alarm() is False
    assert gate._alarm_armed is True
    assert gate._cancel_alarm() is True
    assert gate._alarm_armed is False


def test_gate_guard_balance():
    """The idle inhibitor must be released only across suspend and restored exactly."""
    class FakeGuard:
        def __init__(self):
            self.d = 0

        def acquire(self):
            self.d += 1

        def release(self):
            self.d -= 1

    clock = FakeClock(t(2, 0))
    wake = t(2, 4)
    guard = FakeGuard()

    def fake_helper(args):
        if args[0] == "--suspend":
            clock.now = clock.now + timedelta(minutes=4)
        return True

    gate = power.OffpeakGate(
        make_settings(), is_peak=lambda: True, emit=lambda tag, text: None,
        on_pause=lambda ts: None, on_resume=lambda: None, guard=guard,
        clock=clock, sleeper=lambda s: setattr(clock, "now", clock.now + timedelta(seconds=s)),
        idle_ms=lambda: 10 * 60_000, next_wake=lambda now: wake, helper=fake_helper)
    gate._wait_network = lambda timeout=45: None
    gate._wait_local = lambda timeout=150: None
    gate.start()
    gate.before_step("test")
    gate.stop()
    assert guard.d == 0, guard.d


def test_run_helper_uses_sudo():
    """The RTC helper must be invoked through `sudo -n` (root-only sysfs)."""
    import tempfile
    import power_linux as pw

    with tempfile.NamedTemporaryFile() as tf:
        settings = types.SimpleNamespace(sleep_helper=tf.name)
        gate = power.OffpeakGate(settings, is_peak=lambda: False)
        captured = {}

        def fake_run(argv, capture_output, text, timeout):
            captured["argv"] = argv

            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()

        orig = pw.subprocess.run
        pw.subprocess.run = fake_run
        try:
            assert gate._run_helper(["--arm", "123"]) is True
        finally:
            pw.subprocess.run = orig
    assert captured["argv"][:2] == ["sudo", "-n"], captured
    assert captured["argv"][-2:] == ["--arm", "123"]


def test_crew_sleep_at_dry_run():
    script = Path(__file__).resolve().parents[1] / "scripts" / "crew-sleep"
    epoch = str(int(time.time()) + 3600)
    proc = subprocess.run(["bash", str(script), "--dry-run", "--at", epoch],
                          capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
    assert "dry-run: arm" in proc.stdout
    assert "dry-run: suspend" in proc.stdout
    assert "dry-run: cancel" in proc.stdout


if __name__ == "__main__":
    test_windows()
    test_gate_suspend()
    test_gate_user_active_no_suspend()
    test_gate_sleep_if_active()
    test_gate_early_wake_re_suspends()
    test_gate_arm_failure_waits_awake()
    test_gate_arm_failure_keeps_idle_guard()
    test_gate_guard_balance()
    test_cancel_failure_keeps_alarm_marked_armed()
    test_run_helper_uses_sudo()
    test_crew_sleep_at_dry_run()
    test_idle_guard()
    print("POWER_TESTS_OK")
