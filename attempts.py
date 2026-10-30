"""Per-call attempt collection (stdlib only).

Every candidate invoked inside a fallback chain must produce its own
``TaskAttempt``. The LangChain callback adapter lives in ``main.py``; this
module is the dependency-free scoreboard it feeds.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

from registry import ModelRef, ref_from_handle
from state import TaskAttempt, new_id, utc_now


@dataclass
class AttemptEvent:
    attempt: TaskAttempt
    task: str = ""
    tag: str = ""
    run_id: str = ""

    def to_dict(self) -> dict:
        return {
            "attempt": self.attempt.to_dict(),
            "task": self.task,
            "tag": self.tag,
            "run_id": self.run_id,
        }


class AttemptCollector:
    """Thread-safe open/close bookkeeping for one logical model call."""

    def __init__(self, run_id: str = "", task: str = "", tag: str = ""):
        self.run_id = run_id
        self.task = task
        self.tag = tag
        self._lock = threading.Lock()
        self._open: dict[str, AttemptEvent] = {}
        self._done: list[AttemptEvent] = []

    def start(self, ref: ModelRef, *, task: str = "", tag: str = "",
              run_id: str = "") -> str:
        attempt = TaskAttempt(attempt_id=new_id("att_"), model=ref,
                              started_at=utc_now(), outcome="running")
        event = AttemptEvent(attempt=attempt, task=task or self.task,
                             tag=tag or self.tag, run_id=run_id or self.run_id)
        with self._lock:
            self._open[attempt.attempt_id] = event
        return attempt.attempt_id

    def finish(self, attempt_id: str, outcome: str,
               error: Optional[str] = None,
               served_id: str = "") -> Optional[AttemptEvent]:
        with self._lock:
            event = self._open.pop(attempt_id, None)
        if event is None:
            return None
        event.attempt.completed_at = utc_now()
        event.attempt.outcome = outcome
        if error:
            event.attempt.errors.append(str(error)[:500])
        if served_id:
            event.attempt.model = ref_from_handle(
                _RefCarrier(event.attempt.model), served_id=served_id)
        with self._lock:
            self._done.append(event)
        return event

    def events(self) -> list[AttemptEvent]:
        with self._lock:
            return list(self._done)

    def last_ok(self, task: Optional[str] = None) -> Optional[AttemptEvent]:
        with self._lock:
            for event in reversed(self._done):
                if event.attempt.outcome == "ok" and (task is None or event.task == task):
                    return event
        return None

    def synthesize(self, ref: ModelRef, outcome: str,
                   error: Optional[str] = None) -> AttemptEvent:
        """Used when no callback fired (e.g. fake/non-LangChain handles)."""
        attempt = TaskAttempt(attempt_id=new_id("att_"), model=ref,
                              started_at=utc_now(), completed_at=utc_now(),
                              outcome=outcome)
        if error:
            attempt.errors.append(str(error)[:500])
        event = AttemptEvent(attempt=attempt, task=self.task,
                             tag=self.tag, run_id=self.run_id)
        with self._lock:
            self._done.append(event)
        return event


class _RefCarrier:
    """Minimal handle-like object for ref_from_handle(served_id=...)."""

    def __init__(self, ref: ModelRef):
        self._model_ref = ref
