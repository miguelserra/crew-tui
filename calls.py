"""Durable ledger for external model calls, with an explicit outcome contract.

States:
  PENDING    intent persisted, call not yet dispatched
  SUBMITTED  request is being dispatched (may have reached the provider)
  CONFIRMED  response received and identity/result metadata persisted
  UNKNOWN    ambiguous outcome; never auto-retry, requires human resolution
  FAILED     confirmed failure without a usable result (safe to fall back)

The ledger is metadata only: result text is hashed, never stored. It does not
pretend that idempotency keys guarantee exactly-once execution; a key is
recorded for providers that may use it, but the UNKNOWN state is the real
guard when the provider may have processed and charged the request.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from store import atomic_write_json
from state import utc_now

PENDING = "PENDING"
SUBMITTED = "SUBMITTED"
CONFIRMED = "CONFIRMED"
UNKNOWN = "UNKNOWN"
FAILED = "FAILED"

UNRESOLVED = {PENDING, SUBMITTED, UNKNOWN}


class CallLedgerError(Exception):
    """The ledger is corrupt or the requested operation is invalid."""


class CallOutcomeUnknown(Exception):
    """An external call may have been processed; resolve manually before retry."""


@dataclass
class CallRecord:
    call_id: str
    run_id: str
    state: str = PENDING
    task: str = ""
    tag: str = ""
    role: str = ""
    provider: str = ""
    endpoint: str = ""
    model_id: str = ""
    family: str = ""
    billing: str = ""
    credential_profile: str = ""
    idempotency_key: str = ""
    created: str = ""
    updated: str = ""
    served: dict[str, Any] = field(default_factory=dict)
    result_sha256: str = ""
    result_chars: int = 0
    error: str = ""
    error_kind: str = ""
    resolved_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CallRecord":
        data = data or {}
        return cls(
            call_id=str(data.get("call_id") or ""),
            run_id=str(data.get("run_id") or ""),
            state=str(data.get("state") or UNKNOWN).upper(),
            task=str(data.get("task") or ""),
            tag=str(data.get("tag") or ""),
            role=str(data.get("role") or ""),
            provider=str(data.get("provider") or ""),
            endpoint=str(data.get("endpoint") or ""),
            model_id=str(data.get("model_id") or ""),
            family=str(data.get("family") or ""),
            billing=str(data.get("billing") or ""),
            credential_profile=str(data.get("credential_profile") or ""),
            idempotency_key=str(data.get("idempotency_key") or ""),
            created=str(data.get("created") or ""),
            updated=str(data.get("updated") or ""),
            served=dict(data.get("served") or {}),
            result_sha256=str(data.get("result_sha256") or ""),
            result_chars=int(data.get("result_chars") or 0),
            error=str(data.get("error") or ""),
            error_kind=str(data.get("error_kind") or ""),
            resolved_by=str(data.get("resolved_by") or ""),
        )


class CallLedger:
    """One JSON ledger per run/project, written atomically."""

    VERSION = 1

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()
        self._records: dict[str, CallRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise CallLedgerError(f"ledger ilegível: {self.path}: {exc}") from exc
        if not isinstance(data, dict):
            raise CallLedgerError(f"ledger inválido: {self.path}")
        for raw in data.get("calls") or []:
            if not isinstance(raw, dict):
                continue
            rec = CallRecord.from_dict(raw)
            if rec.call_id:
                self._records[rec.call_id] = rec

    def _save(self) -> None:
        atomic_write_json(self.path, {
            "version": self.VERSION,
            "calls": [r.to_dict() for r in self._records.values()],
        })

    def intent(self, *, run_id: str, role: str, ref: Any,
               task: str = "", tag: str = "") -> CallRecord:
        with self._lock:
            now = utc_now()
            rec = CallRecord(
                call_id="call_" + uuid.uuid4().hex,
                run_id=str(run_id or ""),
                state=PENDING,
                task=str(task or ""),
                tag=str(tag or ""),
                role=str(role or ""),
                provider=str(getattr(ref, "provider", "") or ""),
                endpoint=str(getattr(ref, "endpoint", "") or ""),
                model_id=str(getattr(ref, "model_id", "") or ""),
                family=str(getattr(ref, "family", "") or ""),
                billing=str(getattr(ref, "billing", "") or ""),
                credential_profile=str(getattr(ref, "credential_profile", "") or ""),
                idempotency_key="crew-" + uuid.uuid4().hex,
                created=now,
                updated=now,
            )
            self._records[rec.call_id] = rec
            self._save()
            return rec

    def submitted(self, call_id: str) -> CallRecord:
        return self._transition(call_id, SUBMITTED)

    def confirm(self, call_id: str, *, served: Any = None,
                text: str = "") -> CallRecord:
        with self._lock:
            rec = self._require(call_id)
            rec.state = CONFIRMED
            rec.updated = utc_now()
            rec.error = ""
            rec.error_kind = ""
            if served is not None:
                rec.served = (served.to_dict() if hasattr(served, "to_dict")
                              else dict(served))
            if text:
                rec.result_sha256 = hashlib.sha256(
                    text.encode("utf-8", "ignore")).hexdigest()
                rec.result_chars = len(text)
            self._save()
            return rec

    def fail(self, call_id: str, error: str, kind: str,
             *, ambiguous: bool) -> CallRecord:
        with self._lock:
            rec = self._require(call_id)
            rec.state = UNKNOWN if ambiguous else FAILED
            rec.error = str(error)[:500]
            rec.error_kind = str(kind or "")
            rec.updated = utc_now()
            self._save()
            return rec

    def resolve(self, call_id: str, outcome: str) -> CallRecord:
        outcome = (outcome or "").strip().lower()
        if outcome not in ("confirmed", "failed"):
            raise CallLedgerError("outcome deve ser 'confirmed' ou 'failed'")
        with self._lock:
            rec = self._require(call_id)
            if rec.state not in UNRESOLVED:
                return rec
            rec.state = CONFIRMED if outcome == "confirmed" else FAILED
            rec.resolved_by = "manual"
            rec.updated = utc_now()
            self._save()
            return rec

    def all(self) -> list[CallRecord]:
        with self._lock:
            return list(self._records.values())

    def get(self, call_id: str) -> Optional[CallRecord]:
        with self._lock:
            return self._records.get(call_id)

    def unresolved(self, run_id: Optional[str] = None) -> list[CallRecord]:
        with self._lock:
            return [r for r in self._records.values()
                    if r.state in UNRESOLVED
                    and (run_id is None or r.run_id == run_id)]

    def _require(self, call_id: str) -> CallRecord:
        rec = self._records.get(call_id)
        if rec is None:
            raise CallLedgerError(f"chamada desconhecida: {call_id}")
        return rec

    def _transition(self, call_id: str, state: str) -> CallRecord:
        with self._lock:
            rec = self._require(call_id)
            rec.state = state
            rec.updated = utc_now()
            self._save()
            return rec
