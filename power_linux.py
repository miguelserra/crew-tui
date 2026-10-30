"""Linux power backend: keep the session awake and pause/suspend at peak.

* ``IdleGuard``   - holds ``systemd-inhibit --what=idle`` while a run is active
  so GNOME does not suspend mid-build.
* ``OffpeakGate`` - at a peak boundary, pauses the run, arms an RTC alarm via
  ``scripts/crew-sleep`` (through ``sudo -n``), suspends, and resumes on wake.

The RTC alarm needs root, hence the tiny helper installed once by
``scripts/install-crew-sleep.sh``.
"""

from __future__ import annotations

import atexit
import os
import signal
import socket
import subprocess
import threading
import time
import urllib.request
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from peak import next_offpeak_start

UTC = timezone.utc
Emit = Callable[[str, str], None]


def session_idle_ms() -> Optional[int]:
    """Milliseconds since the user last touched the session (GNOME), or None."""
    try:
        with suppress(Exception):
            out = subprocess.run(
                ["gdbus", "call", "--session",
                 "--dest", "org.gnome.Mutter.IdleMonitor",
                 "--object-path", "/org/gnome/Mutter/IdleMonitor/Core",
                 "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime"],
                capture_output=True, text=True, timeout=3, check=True).stdout
            digits = "".join(ch for ch in out if ch.isdigit())
            return int(digits) if digits else None
    except Exception:
        pass
    return None


class IdleGuard:
    """Hold a systemd ``idle`` inhibitor while a run is in progress."""

    def __init__(self, what: str = "idle", why: str = "crew-tui: agent run") -> None:
        self.what = what
        self.why = why
        self._proc: Optional[subprocess.Popen[bytes]] = None
        self._lock = threading.Lock()
        self._depth = 0

    @property
    def active(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def acquire(self) -> None:
        with self._lock:
            self._depth += 1
            if self.active:
                return
            try:
                self._proc = subprocess.Popen(
                    ["systemd-inhibit", f"--what={self.what}", "--who=crew-tui",
                     f"--why={self.why}", "--mode=block", "sleep", "infinity"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
            except Exception:
                self._proc = None

    def release(self) -> None:
        with self._lock:
            if self._depth > 0:
                self._depth -= 1
            if self._depth > 0 or self._proc is None:
                return
            proc, self._proc = self._proc, None
            with suppress(Exception):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            with suppress(Exception):
                proc.wait(timeout=5)

    def __enter__(self) -> "IdleGuard":
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.release()
        return False


class OffpeakGate:
    """Pause a run during peak hours and resume when off-peak starts."""

    def __init__(self, settings: Any, *, is_peak: Callable[[], bool],
                 emit: Optional[Emit] = None,
                 on_pause: Optional[Callable[[float], None]] = None,
                 on_resume: Optional[Callable[[], None]] = None,
                 guard: Optional[IdleGuard] = None,
                 clock: Optional[Callable[[], datetime]] = None,
                 sleeper: Optional[Callable[[float], None]] = None,
                 idle_ms: Optional[Callable[[], Optional[int]]] = None,
                 next_wake: Optional[Callable[[datetime], datetime]] = None,
                 helper: Optional[Callable[[list[str]], bool]] = None) -> None:
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
        self._helper = helper or self._run_helper
        self._paused = False
        self._alarm_armed = False
        atexit.register(self._at_exit)

    # -- public API --------------------------------------------------------- #

    def start(self) -> None:
        """Called once at the beginning of a run."""
        if self.settings.sleep_offpeak:
            # clear any stale alarm left by a previous crashed run (idempotent)
            self._helper(["--cancel"])
        if self.guard is not None:
            self.guard.acquire()

    def stop(self) -> None:
        """Called in ``finally`` when the run ends: cancel alarm, free inhibitor."""
        self._cancel_alarm()
        self._paused = False
        if self.guard is not None:
            self.guard.release()

    def before_step(self, label: str = "") -> None:
        """Block until off-peak if the run must be paused; otherwise return."""
        if not self.settings.sleep_offpeak or not self.is_peak():
            return
        now = self._clock()
        wake = self._next_wake(now)
        delay = (wake - now).total_seconds()
        if delay < 120:
            # Too close to avoid a past-dated alarm: wait awake, no suspend.
            self.emit("POWER", f"off-peak em {delay:.0f}s — a aguardar acordado")
            self._wait_until(wake)
            return
        self._pause(label, wake)

    # -- internals ---------------------------------------------------------- #

    def _default_next_wake(self, now: datetime) -> datetime:
        s = self.settings
        return next_offpeak_start(now, s.peak_windows, s.peak_weekdays_only)

    def _cancel_alarm(self) -> bool:
        if not self._alarm_armed:
            return True
        if self._helper(["--cancel"]):
            self._alarm_armed = False
            return True
        self.emit("POWER", "FALHA: não consegui cancelar o alarme RTC")
        return False

    def _at_exit(self) -> None:
        """Best-effort alarm cleanup if the process is torn down mid-pause."""
        if self._alarm_armed:
            with suppress(Exception):
                self._helper(["--cancel"])

    def _pause(self, label: str, wake: datetime) -> None:
        if self._paused:
            return
        self._paused = True
        ts = int(wake.timestamp())
        suffix = f" ({label})" if label else ""
        self.emit("POWER", f"pico: run pausado até {wake:%Y-%m-%d %H:%M} UTC{suffix}")
        self._call(self.on_pause, float(ts))

        local_stopped = False
        guard_released = False
        try:
            if not self._helper(["--arm", str(ts)]):
                self.emit("POWER", "FALHA: alarme RTC não armado (sudo -n recusado?) — "
                                   "run pausado; a aguardar off-peak acordado")
                self._wait_until(wake)
            else:
                self._alarm_armed = True
                suspend_attempts = 0
                while self._clock() < wake:
                    if self._user_active() and not self.settings.sleep_if_active:
                        remaining = (wake - self._clock()).total_seconds()
                        if remaining <= 0:
                            break
                        self.emit("POWER", f"sessão ativa — {remaining / 60:.0f} min "
                                           "até ao off-peak (sem suspender)")
                        if local_stopped:
                            self._start_local()
                            local_stopped = False
                        self._sleep(min(30.0, remaining))
                        continue
                    if suspend_attempts >= 3:
                        self.emit("POWER", "acordou várias vezes antes do off-peak — "
                                           "a aguardar acordado")
                        if local_stopped:
                            self._start_local()
                            local_stopped = False
                        self._wait_until(wake)
                        break
                    suspend_attempts += 1
                    if not local_stopped:
                        local_stopped = self._stop_local()
                    self._release_guard()
                    guard_released = True
                    if not self._helper(["--suspend"]):
                        self.emit("POWER", "suspender falhou — a aguardar acordado")
                        self._acquire_guard()
                        guard_released = False
                        if local_stopped:
                            self._start_local()
                            local_stopped = False
                        self._wait_until(wake)
                        break
                    self._acquire_guard()
                    guard_released = False
                    if local_stopped:
                        self._start_local()
                        local_stopped = False
                    # resumed: loop re-checks the clock (early wake -> re-suspend)
                self._cancel_alarm()
        finally:
            if guard_released:
                self._acquire_guard()
            if local_stopped:
                self._start_local()
            self._wait_network()
            self._wait_local()
            self._call(self.on_resume)
            self._paused = False
            self.emit("POWER", "off-peak: a retomar o run")

    def _acquire_guard(self) -> None:
        if self.guard is not None:
            self.guard.acquire()

    def _release_guard(self) -> None:
        if self.guard is not None:
            self.guard.release()

    def _user_active(self) -> bool:
        idle = self._idle_ms()
        if idle is None:
            return True  # cannot tell -> do not hijack the session
        return idle < self.settings.offpeak_idle_min * 60_000

    def _wait_until(self, wake: datetime) -> None:
        while self._clock() < wake:
            remaining = (wake - self._clock()).total_seconds()
            self._sleep(max(1.0, min(30.0, remaining)))

    def _run_helper(self, args: list[str]) -> bool:
        path = str(self.settings.sleep_helper or "")
        if not path or not os.path.isabs(path) or not os.path.exists(path):
            return False
        # The RTC alarm is root-only; the sudoers rule grants NOPASSWD for this
        # exact path. Run through `sudo -n` so it never prompts for a password.
        timeout = 86400 if ("--suspend" in args or "--at" in args) else 120
        try:
            proc = subprocess.run(["sudo", "-n", path, *args], capture_output=True,
                                  text=True, timeout=timeout)
        except Exception as e:
            self.emit("POWER", f"helper falhou: {e}")
            return False
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "").strip()[:240]
            self.emit("POWER", f"helper recusou ({proc.returncode}): {msg}")
            return False
        return True

    def _stop_local(self) -> bool:
        s = self.settings
        if not (s.qwen_restart_on_wake and s.local_service):
            return False
        try:
            subprocess.run(["systemctl", "--user", "stop", s.local_service],
                           capture_output=True, timeout=60)
            return True
        except Exception:
            return False

    def _start_local(self) -> None:
        s = self.settings
        if not (s.qwen_restart_on_wake and s.local_service):
            return
        try:
            subprocess.run(["systemctl", "--user", "start", s.local_service],
                           capture_output=True, timeout=120)
        except Exception:
            pass

    def _wait_local(self, timeout: float = 150.0) -> None:
        s = self.settings
        if not (s.qwen_restart_on_wake and s.local_service):
            return
        url = s.local_llm_url.rstrip("/") + "/models"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as resp:
                    if resp.status == 200:
                        return
            except Exception:
                pass
            self._sleep(3.0)

    def _wait_network(self, timeout: float = 45.0) -> None:
        hosts = ["api.deepseek.com", "opencode.ai"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for host in hosts:
                try:
                    socket.create_connection((host, 443), timeout=3).close()
                    return
                except Exception:
                    pass
            self._sleep(3.0)

    def _call(self, fn: Optional[Callable[..., Any]], *args: Any) -> None:
        if fn is None:
            return
        try:
            fn(*args)
        except Exception as e:
            self.emit("POWER", f"callback falhou: {type(e).__name__}: {e}")


def make_power(settings: Any, pool: Any, emit: Emit, *,
               on_pause: Optional[Callable[[float], None]] = None,
               on_resume: Optional[Callable[[], None]] = None) -> OffpeakGate:
    """Build the Linux policy object used by Factory/Orchestrator."""
    guard = IdleGuard() if settings.keep_awake else None
    return OffpeakGate(settings, is_peak=pool.is_peak, emit=emit,
                       on_pause=on_pause, on_resume=on_resume, guard=guard)
