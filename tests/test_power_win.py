"""Windows-backend tests (pure logic + smoke; no suspension).

Run on Windows with:
    python tests/test_power_win.py
"""

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import power_win  # noqa: E402  (Windows-only: uses ctypes.windll)

UTC = timezone.utc
W = "01:00-04:00,06:00-10:00"


def t(h, m=0, day=30):
    return datetime(2026, 10, day, h, m, tzinfo=UTC)  # Friday


def make_settings(**kw):
    base = dict(sleep_offpeak=False, keep_awake=True, peak_windows=W,
                peak_weekdays_only=True, sleep_if_active=False,
                offpeak_idle_min=5, qwen_restart_on_wake=False,
                local_service="", sleep_helper="",
                local_llm_url="http://127.0.0.1:8081/v1")
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_peak_helpers():
    assert power_win.parse_windows(W) == [(60, 240), (360, 600)]
    assert power_win.is_peak_at(t(1, 22), W)
    assert not power_win.is_peak_at(t(4, 0), W)
    assert not power_win.is_peak_at(t(2, 0, day=31), W)  # Saturday
    assert power_win.next_offpeak_start(t(1, 22), W) == t(4, 0)


def test_make_power_type_and_guard():
    pool = types.SimpleNamespace(is_peak=lambda: False)
    gate = power_win.make_power(make_settings(), pool, lambda tag, text: None)
    assert type(gate).__name__ == "WindowsOffpeakGate"
    gate.start()
    assert gate.guard.active is True
    gate.before_step("noop")  # sleep_offpeak=False -> return immediately
    gate.stop()
    assert gate.guard.active is False


def test_guard_execution_state():
    guard = power_win.WindowsIdleGuard()
    assert not guard.active
    guard.acquire()
    assert guard.active
    guard.release()
    assert not guard.active


def test_idle_ms_is_none_or_non_negative():
    value = power_win.session_idle_ms()
    assert value is None or value >= 0


def test_pause_waits_awake_without_suspending():
    clock = types.SimpleNamespace(now=t(2, 0))
    wake = t(2, 4)
    events = []
    gate = power_win.WindowsOffpeakGate(
        make_settings(sleep_offpeak=True, sleep_if_active=True),  # force pause
        is_peak=lambda: True,
        emit=lambda tag, text: events.append(text),
        on_pause=lambda ts: events.append("pause"),
        on_resume=lambda: events.append("resume"),
        guard=None,
        clock=lambda: clock.now,
        sleeper=lambda s: setattr(clock, "now", clock.now + timedelta(seconds=s)),
        idle_ms=lambda: 10 * 60_000,
        next_wake=lambda now: wake,
    )
    gate.before_step("test")
    assert "pause" in events and "resume" in events
    assert clock.now >= wake
    assert any("sem suspender" in e for e in events)


if __name__ == "__main__":
    test_peak_helpers()
    test_make_power_type_and_guard()
    test_guard_execution_state()
    test_idle_ms_is_none_or_non_negative()
    test_pause_waits_awake_without_suspending()
    print("POWER_WIN_TESTS_OK")
