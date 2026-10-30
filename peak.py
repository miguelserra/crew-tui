"""Pure peak/off-peak window helpers shared by every power backend.

No I/O, no platform dependency: easy to unit-test anywhere.
"""

from __future__ import annotations

from datetime import datetime, timedelta


def parse_windows(spec: str) -> list[tuple[int, int]]:
    """Parse ``"01:00-04:00,06:00-10:00"`` into minute ranges (may wrap)."""
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
    """True when ``now`` falls inside any configured peak window (UTC)."""
    if weekdays_only and now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    for start, end in parse_windows(windows):
        if start <= end:
            if start <= minutes < end:
                return True
        elif minutes >= start or minutes < end:  # window wraps midnight
            return True
    return False


def next_offpeak_start(now: datetime, windows: str,
                       weekdays_only: bool = True) -> datetime:
    """First UTC minute outside the peak windows, at least one minute ahead."""
    probe = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
    for _ in range(8 * 24 * 60):  # hard stop after 8 days
        if not is_peak_at(probe, windows, weekdays_only):
            return probe
        probe += timedelta(minutes=1)
    return probe
