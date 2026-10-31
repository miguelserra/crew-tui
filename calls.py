"""Durable ledger + result artifacts for external model calls.

States:
  PENDING    intent persisted, call not yet dispatched
  SUBMITTED  request is being dispatched (may have reached the provider)
  CONFIRMED  response received and its result artifact persisted
  UNKNOWN    ambiguous outcome; never auto-retry, requires human resolution
  FAILED     confirmed failure without a usable result (safe to fall back)

Result artifacts live under ``.crew/call_results`` (excluded from publication
by the existing ``.crew`` rule). They are local-only and may contain generated
code; remove ``.crew`` when discarding a run. The ledger records only metadata,
a request fingerprint and a result hash.

The ledger does not claim exactly-once execution. A recorded idempotency key is
not sent unless the provider declares support; ``UNKNOWN`` plus manual
resolution is the actual guard when a request may have been charged.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from store import atomic_write_json
from state import utc_now

PENDING = "PENDING"
SUBMITTED = "SUBMITTED"
CONFIRMED = "CONFIRMED"
UNKNOWN = "UNKNOWN"
FAILED = "FAILED"

UNRESOLVED = {PENDING, SUBMITTED, UNKNOWN}

_LAST_CALL: contextvars.ContextVar[str] = contextvars.ContextVar(
    "crew_last_call", default="")
_LAST_SERVED: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "crew_last_served", default=None)


def set_call_context(call_id: str, served: Any = None) -> None:
    _LAST_CALL.set(call_id or "")
    if served is not None:
        _LAST_SERVED.set(served)


def last_call_id() -> str:
    return _LAST_CALL.get()


def last_served_ref() -> Any:
    return _LAST_SERVED.get()


def clear_call_context() -> None:
    _LAST_CALL.set("")
    _LAST_SERVED.set(None)


def message_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return json.dumps(content, ensure_ascii=False, sort_keys=True,
                          default=str)
    return str(content)


def full_identity_key(provider: str, endpoint: str,
                      credential_profile: str, model_id: str) -> str:
    """Canonical model identity shared by ledger, router and registry flows."""
    return f"{provider}|{endpoint}|{credential_profile}|{model_id}"


def request_fingerprint(messages: Iterable[Any]) -> str:
    payload = [message_text(m) for m in messages]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()


def invocation_fingerprint(messages: Iterable[Any], request: Any = None) -> str:
    """Fingerprint of the messages plus every generation parameter that changes
    the semantic contract of the response (not routing/bookkeeping fields)."""
    payload: dict[str, Any] = {"messages": [message_text(m) for m in messages]}
    if request is not None:
        payload["contract"] = {
            "role": getattr(request, "role", ""),
            "max_tokens": getattr(request, "max_tokens", None),
            "thinking": bool(getattr(request, "thinking", False)),
            "capabilities": sorted(getattr(request, "required_capabilities", ()) or ()),
            "escalation": bool(getattr(request, "escalation", False)),
            "response_format": getattr(request, "response_format", None),
            "tools": getattr(request, "tools", None),
        }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()


class CallLedgerError(Exception):
    """The ledger/result store is corrupt or the requested operation is invalid."""


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
    request_sha256: str = ""
    created: str = ""
    updated: str = ""
    served: dict[str, Any] = field(default_factory=dict)
    result_sha256: str = ""
    result_chars: int = 0
    replayable: bool = True
    consumed: bool = False
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
            request_sha256=str(data.get("request_sha256") or ""),
            created=str(data.get("created") or ""),
            updated=str(data.get("updated") or ""),
            served=dict(data.get("served") or {}),
            result_sha256=str(data.get("result_sha256") or ""),
            result_chars=int(data.get("result_chars") or 0),
            replayable=bool(data.get("replayable", True)),
            consumed=bool(data.get("consumed") or False),
            error=str(data.get("error") or ""),
            error_kind=str(data.get("error_kind") or ""),
            resolved_by=str(data.get("resolved_by") or ""),
        )


class CallLedger:
    """One JSON ledger + result directory per project/run, written atomically."""

    VERSION = 2

    def __init__(self, path: str | Path, result_dir: str | Path | None = None):
        self.path = Path(path).expanduser()
        self.result_dir = (Path(result_dir).expanduser() if result_dir is not None
                           else self.path.parent / "call_results")
        self._lock = threading.RLock()
        self._records: dict[str, CallRecord] = {}
        self._load()
        self.reconcile()

    # -- persistence -------------------------------------------------------- #

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

    def _result_path(self, call_id: str) -> Path:
        return self.result_dir / f"{call_id}.json"

    def _write_result(self, rec: CallRecord, text: str,
                      replayable: bool) -> None:
        atomic_write_json(self._result_path(rec.call_id), {
            "version": 2,
            "call_id": rec.call_id,
            "request_sha256": rec.request_sha256,
            "replayable": bool(replayable),
            "sha256": hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest(),
            "text": text,
        })

    def _read_result(self, call_id: str) -> Optional[tuple[str, bool]]:
        path = self._result_path(call_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        text = data.get("text")
        if not isinstance(text, str):
            return None
        digest = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
        if data.get("sha256") and data.get("sha256") != digest:
            return None
        rec = self.get(call_id)
        if rec is not None and rec.request_sha256 and data.get("request_sha256") \
                and data.get("request_sha256") != rec.request_sha256:
            return None
        return text, bool(data.get("replayable", True))

    def result_text(self, call_id: str) -> Optional[str]:
        """Text available for replay; structured responses are explicit non-goals."""
        raw = self._read_result(call_id)
        if raw is None:
            return None
        text, replayable = raw
        return text if replayable else None

    # -- lifecycle ---------------------------------------------------------- #

    def intent(self, *, run_id: str, role: str, ref: Any,
               task: str = "", tag: str = "",
               request_sha256: str = "") -> CallRecord:
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
                request_sha256=str(request_sha256 or ""),
                created=now,
                updated=now,
            )
            self._records[rec.call_id] = rec
            self._save()
            return rec

    def submitted(self, call_id: str) -> CallRecord:
        return self._transition(call_id, SUBMITTED)

    def confirm(self, call_id: str, *, served: Any = None,
                text: str = "", replayable: bool = True) -> CallRecord:
        with self._lock:
            rec = self._require(call_id)
            if served is not None:
                rec.served = (served.to_dict() if hasattr(served, "to_dict")
                              else dict(served))
            rec.replayable = bool(replayable)
            if text:
                try:
                    self._write_result(rec, text, rec.replayable)
                except Exception as exc:
                    rec.state = UNKNOWN
                    rec.error = f"resultado não persistido: {exc}"[:500]
                    rec.error_kind = "persist"
                    rec.updated = utc_now()
                    self._save()
                    raise CallLedgerError(
                        f"resultado de {call_id} não persistido") from exc
                rec.result_sha256 = hashlib.sha256(
                    text.encode("utf-8", "ignore")).hexdigest()
                rec.result_chars = len(text)
            rec.state = CONFIRMED
            rec.updated = utc_now()
            rec.error = ""
            rec.error_kind = ""
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
            if outcome == "confirmed" and self.result_text(call_id) is None:
                raise CallLedgerError(
                    "não posso confirmar sem o resultado persistido; "
                    "resolve como failed ou repõe o artefacto")
            if outcome == "failed" and self.result_text(call_id) is not None:
                raise CallLedgerError(
                    "existe resultado replayable; 'failed' contradiz a evidência")
            rec.state = CONFIRMED if outcome == "confirmed" else FAILED
            rec.resolved_by = "manual"
            rec.updated = utc_now()
            self._save()
            return rec

    def reconcile(self) -> list[str]:
        """Promote SUBMITTED/UNKNOWN records whose result artifact survived."""
        promoted: list[str] = []
        with self._lock:
            for rec in self._records.values():
                if rec.state not in (SUBMITTED, UNKNOWN):
                    continue
                raw = self._read_result(rec.call_id)
                if raw is None:
                    continue
                text, replayable = raw
                rec.state = CONFIRMED
                rec.replayable = replayable
                rec.result_sha256 = hashlib.sha256(
                    text.encode("utf-8", "ignore")).hexdigest()
                rec.result_chars = len(text)
                rec.updated = utc_now()
                promoted.append(rec.call_id)
            if promoted:
                self._save()
        return promoted

    def mark_consumed(self, call_id: str) -> Optional[CallRecord]:
        with self._lock:
            rec = self._records.get(call_id)
            if rec is None or rec.consumed:
                return rec
            rec.consumed = True
            rec.updated = utc_now()
            self._save()
            return rec

    def mark_consumed_many(self, call_ids: Iterable[str]) -> None:
        with self._lock:
            changed = False
            for call_id in call_ids:
                rec = self._records.get(call_id)
                if rec is not None and not rec.consumed:
                    rec.consumed = True
                    changed = True
            if changed:
                self._save()

    # -- queries ------------------------------------------------------------ #

    def find_replay(self, run_id: str, task: str, tag: str,
                    request_sha256: str) -> Optional[CallRecord]:
        with self._lock:
            for rec in self._records.values():
                if (rec.state == CONFIRMED and not rec.consumed
                        and rec.run_id == str(run_id or "")
                        and rec.task == str(task or "")
                        and rec.tag == str(tag or "")
                        and rec.request_sha256 == str(request_sha256 or "")):
                    if self.result_text(rec.call_id) is not None:
                        return rec
        return None

    def failed_refs(self, run_id: str, task: str = "",
                    tag: str = "") -> set[str]:
        """Full PAYG identities already confirmed-failed for this task/tag.

        The key is provider+endpoint+profile+model on purpose: two endpoints
        that share a credential profile and model id must not suppress each
        other. A failed PAYG call (automatic or manually resolved) must not be
        silently repeated when the workflow resumes; routing skips it and
        moves to the next candidate instead.
        """
        with self._lock:
            return {
                full_identity_key(rec.provider, rec.endpoint,
                                  rec.credential_profile, rec.model_id)
                for rec in self._records.values()
                if rec.state == FAILED
                and rec.run_id == str(run_id or "")
                and rec.task == str(task or "")
                and rec.tag == str(tag or "")
            }

    def recovery_required(self, run_id: Optional[str] = None) -> list[CallRecord]:
        with self._lock:
            out = []
            for rec in self._records.values():
                if rec.state != CONFIRMED or rec.consumed:
                    continue
                if run_id is not None and rec.run_id != run_id:
                    continue
                if self.result_text(rec.call_id) is None:
                    out.append(rec)
            return out

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
