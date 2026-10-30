"""Power policy backend for Windows.

This module provides the Windows implementation of the power policy.
It keeps the machine awake during runs and pauses at peak hours
(waiting awake, without suspending — Windows wake timers require
additional firmware/config validation).

Key differences from Linux backend:
* Uses SetThreadExecutionState to inhibit sleep
* Uses GetLastInputInfo for idle detection
* No RTC alarm / suspend — waits awake during peak
"""

from __future__ import annotations

import atexit
import ctypes
import ctypes.wintypes
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

UTC = timezone.utc

Emit = Callable[[str, str], None]


# --------------------------------------------------------------------------- #
#  Windows API constants and structures
# --------------------------------------------------------------------------- #

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.UINT),
        ("dwTime", ctypes.wintypes.DWORD),
    ]


# Load Windows APIs
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

SetThreadExecutionState = kernel32.SetThreadExecutionState
SetThreadExecutionState.argtypes = [ctypes.wintypes.DWORD]
SetThreadExecutionState.restype = ctypes.wintypes.DWORD

GetLastInputInfo = user32.GetLastInputInfo
GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
GetLastInputInfo.restype = ctypes.wintypes.BOOL

GetTickCount64 = kernel32.GetTickCount64
GetTickCount64.argtypes = []
GetTickCount64.restype = ctypes.wintypes.ULONGLONG


# --------------------------------------------------------------------------- #
#  Idle inhibitor (Windows)
# --------------------------------------------------------------------------- #

class WindowsIdleGuard:
    """Hold a Windows execution state to prevent sleep while a run is active."""

    def __init__(self) -> None:
        self._active = False
        self._depth = 0

    @property
    def active(self) -> bool:
        return self._active

    def acquire(self) -> None:
        self._depth += 1
        if self._active:
            return
        # ES_CONTINUOUS | ES_SYSTEM_REQUIRED = prevent system sleep
        # (display may still turn off; add ES_DISPLAY_REQUIRED if needed)
        prev = SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        if prev == 0:
            # failed; still mark active so release won't try to clear
            pass
        self._active = True

    def release(self) -> None:
        if self._depth > 0:
            self._depth -= 1
        if self._depth > 0 or not self._active:
            return
        # Restore normal sleep behaviour
        SetThreadExecutionState(ES_CONTINUOUS)
        self._active = False

    def __enter__(self) -> "WindowsIdleGuard":
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.release()
        return False


# --------------------------------------------------------------------------- #
#  Idle detection (Windows)
# --------------------------------------------------------------------------- #

def session_idle_ms() -> Optional[int]:
    """Milliseconds since the user last touched the session (Windows), or None."""
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not GetLastInputInfo(ctypes.byref(lii)):
        return None
    now = GetTickCount64()
    elapsed = now - lii.dwTime
    # Wrap-around every ~49.7 days; GetTickCount64 avoids this
    return int(elapsed)


# --------------------------------------------------------------------------- #
#  Off-peak gate (Windows) — pauses but never suspends
# --------------------------------------------------------------------------- #

class WindowsOffpeakGate:
    """Pause a run during peak hours and resume when off-peak starts.

    On Windows we do not suspend (no RTC alarm). The run simply waits awake
    until the next off-peak window. The idle inhibitor stays active the
    entire time so the machine never sleeps unexpectedly.
    """

    def __init__(self, settings: Any, *, is_peak: Callable[[], bool],
                 emit: Optional[Emit] = None,
                 on_pause: Optional[Callable[[float], None]] = None,
                 on_resume: Optional[Callable[[], None]] = None,
                 guard: Optional[WindowsIdleGuard] = None,
                 clock: Optional[Callable[[], datetime]] = None,
                 sleeper: Optional[Callable[[float], None]] = None,
                 idle_ms: Optional[Callable[[], Optional[int]]] = None,
                 next_wake: Optional[Callable[[datetime], datetime]] = None) -> None:
        self.settings = settings
        self.is_peak = is_peak
        self.emit = emit or (lambda tag, text: None)
        self.on_pause = on_pause
        self.on_resume = on_resume
        self.guard = guard
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleeper or time.sleep
        self._idle_ms = idle_ms or session_idle_ms
        self._next_wake = next_wake or self._default_next_wake
        self._paused = False

    # -- public API --------------------------------------------------------- #

    def start(self) -> None:
        """Called once at the beginning of a run."""
        if self.guard is not None:
            self.guard.acquire()

    def stop(self) -> None:
        """Called in ``finally`` when the run ends: free inhibitor."""
        if self.guard is not None:
            self.guard.release()

    def before_step(self, label: str = "") -> None:
        """Block until off-peak if the run must be paused; otherwise return."""
        if not self.settings.sleep_offpeak or not self.is_peak():
            return
        now = self._clock()
        wake = self._next_wake(now)
        delay = (wake - now).total_seconds()
        if delay < 60:
            # Too close to avoid a past-dated wake: wait awake.
            self.emit("POWER", f"off-peak em {delay:.0f}s — a aguardar acordado")
            self._wait_until(wake)
            return
        self._pause(label, wake)

    # -- internals ---------------------------------------------------------- #

    def _default_next_wake(self, now: datetime) -> datetime:
        s = self.settings
        return next_offpeak_start(now, s.peak_windows, s.peak_weekdays_only)

    def _pause(self, label: str, wake: datetime) -> None:
        if self._paused:
            return
        self._paused = True
        ts = int(wake.timestamp())
        suffix = f" ({label})" if label else ""
        self.emit("POWER", f"pico: run pausado até {wake:%Y-%m-%d %H:%M} UTC{suffix}")
        self._call(self.on_pause, float(ts))

        try:
            self.emit("POWER", "pico: a aguardar off-peak (sem suspender no Windows)")
            while self._clock() < wake:
                remaining = (wake - self._clock()).total_seconds()
                if remaining <= 0:
                    break
                self.emit("POWER", f"pico ativo — {remaining / 60:.0f} min até off-peak")
                self._sleep(min(30.0, remaining))
        finally:
            self._call(self.on_resume)
            self._paused = False
            self.emit("POWER", "off-peak: a retomar o run")

    def _wait_until(self, wake: datetime) -> None:
        while self._clock() < wake:
            remaining = (wake - self._clock()).total_seconds()
            self._sleep(max(1.0, min(30.0, remaining)))

    def _call(self, fn: Optional[Callable[..., Any]], *args: Any) -> None:
        if fn is None:
            return
        try:
            fn(*args)
        except Exception as e:
            self.emit("POWER", f"callback falhou: {type(e).__name__}: {e}")


def make_power(settings: Any, pool: Any, emit: Emit, *,
               on_pause: Optional[Callable[[float], None]] = None,
               on_resume: Optional[Callable[[], None]] = None) -> Any:
    """Build the policy object used by Factory/Orchestrator on Windows."""
    guard = WindowsIdleGuard() if settings.keep_awake else None
    return WindowsOffpeakGate(settings, is_peak=pool.is_peak, emit=emit,
                              on_pause=on_pause, on_resume=on_resume, guard=guard)


# Re-export peak window helpers from power module (avoid duplication)
def parse_windows(spec: str) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if "-" not in part:
            continue
        start_s, end_s = (x.strip() for x in part.split("-", 1))
        try:
            sh, sm = (int(v) for v in start_s.split(":"))
            eh, em = (int(v) for v in end_s.split(":"))
        except ValueError:
            continue
        out.append((sh * 60 + sm, eh * 60 + em))
    return out


def is_peak_at(now: datetime, windows: str, weekdays_only: bool = True) -> bool:
    if weekdays_only and now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    for start, end in parse_windows(windows):
        if start <= end:
            if start <= minutes < end:
                return True
        elif minutes >= start or minutes < end:  # wraps midnight
            return True
    return False


def next_offpeak_start(now: datetime, windows: str,
                       weekdays_only: bool = True) -> datetime:
    probe = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
    for _ in range(8 * 24 * 60):
        if not is_peak_at(probe, windows, weekdays_only):
            return probe
        probe += timedelta(minutes=1)
    return probe


Emit = Callable[[str, str], None]