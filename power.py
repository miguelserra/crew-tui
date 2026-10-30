"""Power policy dispatcher.

Picks the platform backend and re-exports the peak helpers so callers can keep
using a single ``power`` import on every OS.

* Linux  -> ``power_linux`` (systemd inhibitor + RTC alarm/suspend)
* Windows-> ``power_win``   (SetThreadExecutionState; pause awake, no suspend)
"""

from __future__ import annotations

import platform
from typing import Any, Callable, Optional

from peak import is_peak_at, next_offpeak_start, parse_windows

Emit = Callable[[str, str], None]

if platform.system() == "Windows":
    from power_win import WindowsIdleGuard, WindowsOffpeakGate, make_power
else:
    from power_linux import IdleGuard, OffpeakGate, make_power, session_idle_ms

__all__ = [
    "Emit",
    "is_peak_at",
    "next_offpeak_start",
    "parse_windows",
    "make_power",
]

if platform.system() == "Windows":
    __all__ += ["WindowsIdleGuard", "WindowsOffpeakGate"]
else:
    __all__ += ["IdleGuard", "OffpeakGate", "session_idle_ms"]
