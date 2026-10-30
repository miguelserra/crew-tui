"""Windows power backend.

* Keeps the machine awake during a run via ``SetThreadExecutionState``.
* Detects user idleness via ``GetLastInputInfo``.
* Pauses the run at peak hours and **waits awake** until off-peak. There is no
  RTC-alarm/suspend support here yet: a Windows wake timer depends on firmware
  and power-plan settings, so it must be validated per machine before relying
  on it. The idle inhibitor stays held the whole time, so the PC cannot sleep
  unexpectedly while the run is paused.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from peak import next_offpeak_start

UTC = timezone.utc
Emit = Callable[[str, str], None]


# --------------------------------------------------------------------------- #
#  Windows API bindings
# --------------------------------------------------------------------------- #

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.UINT),
        ("dwTime", ctypes.wintypes.DWORD),
    ]


user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

SetThreadExecutionState = kernel32.SetThreadExecutionState
SetThreadExecutionState.argtypes = [ctypes.wintypes.DWORD]
SetThreadExecutionState.restype = ctypes.wintypes.DWORD

GetLastInputInfo = user32.GetLastInputInfo
GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
GetLastInputInfo.restype = ctypes.wintypes.BOOL

GetTickCount = kernel32.GetTickCount
GetTickCount.argtypes = []
GetTickCount.restype = ctypes.wintypes.DWORD


# --------------------------------------------------------------------------- #
#  Idle inhibitor
# --------------------------------------------------------------------------- #

class WindowsIdleGuard:
    """Prevent system sleep while a run is active (display may still turn off)."""

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
        # ES_CONTINUOUS | ES_SYSTEM_REQUIRED = keep the system awake.
        SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        self._active = True

    def release(self) -> None:
        if self._depth > 0:
            self._depth -= 1
        if self._depth > 0 or not self._active:
            return
        SetThreadExecutionState(ES_CONTINUOUS)  # restore normal sleep behaviour
        self._active = False

    def __enter__(self) -> "WindowsIdleGuard":
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.release()
        return False


# --------------------------------------------------------------------------- #
#  Idle detection
# --------------------------------------------------------------------------- #

def session_idle_ms() -> Optional[int]:
    """Milliseconds since the last user input, or None if unavailable."""
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not GetLastInputInfo(ctypes.byref(lii)):
        return None
    # GetTickCount and dwTime are both 32-bit ms counters; mask the wrap.
    return int((GetTickCount() - lii.dwTime) & 0xFFFFFFFF)


# --------------------------------------------------------------------------- #
#  Off-peak gate (pauses awake; never suspends)
# --------------------------------------------------------------------------- #

class WindowsOffpeakGate:
    """Pause a run during peak hours and resume when off-peak starts."""

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
        if self.guard is not None:
            self.guard.acquire()

    def stop(self) -> None:
        self._paused = False
        if self.guard is not None:
            self.guard.release()

    def before_step(self, label: str = "") -> None:
        if not self.settings.sleep_offpeak or not self.is_peak():
            return
        now = self._clock()
        wake = self._next_wake(now)
        delay = (wake - now).total_seconds()
        if delay < 60:
            self.emit("POWER", f"off-peak em {delay:.0f}s — a aguardar acordado")
            self._wait_until(wake)
            return
        self._pause(label, wake)

    # -- internals ---------------------------------------------------------- #

    def _default_next_wake(self, now: datetime) -> datetime:
        s = self.settings
        return next_offpeak_start(now, s.peak_windows, s.peak_weekdays_only)

    def _user_active(self) -> bool:
        idle = self._idle_ms()
        if idle is None:
            return True
        return idle < self.settings.offpeak_idle_min * 60_000

    def _wait_until(self, wake: datetime) -> None:
        while self._clock() < wake:
            remaining = (wake - self._clock()).total_seconds()
            self._sleep(max(1.0, min(30.0, remaining)))

    def _pause(self, label: str, wake: datetime) -> None:
        if self._paused:
            return
        self._paused = True
        ts = int(wake.timestamp())
        suffix = f" ({label})" if label else ""
        self.emit("POWER", f"pico: run pausado até {wake:%Y-%m-%d %H:%M} UTC{suffix}")
        self._call(self.on_pause, float(ts))
        try:
            self.emit("POWER", "Windows: a aguardar off-peak sem suspender")
            if self.settings.sleep_if_active:
                note = "modo unattended"
            elif self._user_active():
                note = "sessão ativa"
            else:
                note = "sessão inativa"
            while self._clock() < wake:
                remaining = (wake - self._clock()).total_seconds()
                if remaining <= 0:
                    break
                self.emit("POWER", f"{note} — {remaining / 60:.1f} min até off-peak")
                self._sleep(min(30.0, remaining))
        finally:
            self._call(self.on_resume)
            self._paused = False
            self.emit("POWER", "off-peak: a retomar o run")

    def _call(self, fn: Optional[Callable[..., Any]], *args: Any) -> None:
        if fn is None:
            return
        try:
            fn(*args)
        except Exception as e:
            self.emit("POWER", f"callback falhou: {type(e).__name__}: {e}")


def make_power(settings: Any, pool: Any, emit: Emit, *,
               on_pause: Optional[Callable[[float], None]] = None,
               on_resume: Optional[Callable[[], None]] = None) -> WindowsOffpeakGate:
    """Build the Windows policy object used by Factory/Orchestrator."""
    guard = WindowsIdleGuard() if settings.keep_awake else None
    return WindowsOffpeakGate(settings, is_peak=pool.is_peak, emit=emit,
                              on_pause=on_pause, on_resume=on_resume, guard=guard)
