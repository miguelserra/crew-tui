"""Central routing policy: fallback, escalation, breaker and PAYG grants.

The Router does not construct LangChain clients. It receives identities from
the Model Registry and handles from the provider/LLMPool layer.

Fallback is operational (timeout/429/5xx/network). Escalation is a quality
decision after verification failure and always uses a stronger chain.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from budget import BudgetSuspended
from calls import (CallOutcomeUnknown, invocation_fingerprint,
                   set_call_context)
from registry import (
    BILLING_LOCAL,
    BILLING_PAYG,
    BILLING_UNKNOWN,
    CostEstimate,
    ModelIdentity,
    ModelRef,
    ModelRegistry,
    normalize_billing,
)

# --------------------------------------------------------------------------- #
#  Errors
# --------------------------------------------------------------------------- #


class UnknownModelError(Exception):
    """A candidate whose billing identity cannot be proven."""


class RouterExhausted(Exception):
    def __init__(self, message: str, last_error: Optional[BaseException] = None):
        super().__init__(message)
        self.last_error = last_error


ERROR_KINDS = ("rate_limit", "quota_exhausted", "timeout", "connection",
               "auth", "bad_request", "server", "empty", "unknown")

QUOTA_PATTERNS = (
    "insufficient_quota", "insufficient quota", "quota exceeded",
    "quota exhausted", "exceeded your current quota", "billing hard limit",
    "credit balance", "no credit", "out of credits",
)
RATE_PATTERNS = ("429", "rate limit", "too many requests", "resource exhausted",
                 "resource_exhausted")


@dataclass(frozen=True)
class ErrorInfo:
    kind: str
    status: Optional[int] = None
    retry_after: float = 0.0
    detail: str = ""


def _retry_after(error: BaseException) -> float:
    value: Any = None
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None) or {}
    try:
        value = headers.get("retry-after") or headers.get("Retry-After")
    except Exception:
        value = None
    if value is None:
        text = f"{error}"
        m = re.search(r"retry[- ]after[:\s]+([0-9]+(?:\.[0-9]+)?)", text, re.I)
        if m:
            value = m.group(1)
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _status(error: BaseException) -> Optional[int]:
    value = getattr(error, "status_code", None)
    if value is None:
        response = getattr(error, "response", None)
        value = getattr(response, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def classify_error(error: BaseException) -> ErrorInfo:
    text = f"{type(error).__name__}: {error}".lower()
    status = _status(error)
    retry = _retry_after(error)
    if any(p in text for p in QUOTA_PATTERNS):
        return ErrorInfo("quota_exhausted", status, retry, text[:400])
    if any(p in text for p in RATE_PATTERNS) or status == 429:
        return ErrorInfo("rate_limit", status, retry, text[:400])
    if "empty response" in text or "empty content" in text or "empty completion" in text:
        return ErrorInfo("empty", status, retry, text[:400])
    if "timeout" in text or "timed out" in text:
        return ErrorInfo("timeout", status, retry, text[:400])
    if status in (401, 403) or "unauthorized" in text or "invalid api key" in text \
            or "forbidden" in text:
        return ErrorInfo("auth", status, retry, text[:400])
    if status == 400 or "bad request" in text or "invalid request" in text:
        return ErrorInfo("bad_request", status, retry, text[:400])
    if status in (500, 502, 503, 504) or "server error" in text \
            or "service unavailable" in text or "bad gateway" in text:
        return ErrorInfo("server", status, retry, text[:400])
    if "connection" in text or "unreachable" in text or "refused" in text \
            or "network" in text or "dns" in text:
        return ErrorInfo("connection", status, retry, text[:400])
    return ErrorInfo("unknown", status, retry, text[:400])


# --------------------------------------------------------------------------- #
#  Circuit breaker (persistent)
# --------------------------------------------------------------------------- #


def breaker_key(identity: ModelIdentity) -> str:
    return f"{identity.provider}|{identity.endpoint}|{identity.credential_profile}"


def identity_key(identity: ModelIdentity) -> str:
    """Unambiguous PAYG identity: provider + endpoint + profile + model."""
    return (f"{identity.provider}|{identity.endpoint}|"
            f"{identity.credential_profile}|{identity.model_id}")


@dataclass
class BreakerEntry:
    state: str = "closed"        # closed|open|half_open
    consecutive: int = 0
    window_start: float = 0.0
    cooldown_until: float = 0.0
    quota_until: float = 0.0
    half_open: bool = False
    disabled: bool = False
    last_error: str = ""
    updated: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "BreakerEntry":
        data = data or {}
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


class CircuitBreaker:
    """3 consecutive rate limits -> cooldown; quota and auth are distinct."""

    def __init__(self, path: str | Path | None = None, *,
                 threshold: int = 3, window: float = 300.0,
                 cooldown: float = 900.0, quota_cooldown: float = 3600.0,
                 clock: Callable[[], float] = time.time):
        self.path = Path(path).expanduser() if path else None
        self.threshold = threshold
        self.window = window
        self.cooldown = cooldown
        self.quota_cooldown = quota_cooldown
        self.clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, BreakerEntry] = {}
        self._load()

    # -- persistence -------------------------------------------------------- #

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            raw = data.get("breakers") if isinstance(data, dict) else None
            self._entries = {k: BreakerEntry.from_dict(v)
                             for k, v in (raw or {}).items()}
        except Exception:
            self._entries = {}

    def _save_locked(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        payload = {"version": 1,
                   "breakers": {k: v.to_dict() for k, v in self._entries.items()}}
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    # -- API ---------------------------------------------------------------- #

    def entry(self, identity: ModelIdentity) -> BreakerEntry:
        return self._entries.get(breaker_key(identity), BreakerEntry())

    def allow(self, identity: ModelIdentity) -> bool:
        key = breaker_key(identity)
        now = self.clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return True
            if entry.disabled or now < entry.quota_until or now < entry.cooldown_until:
                return False
            if entry.state == "open":
                entry.state = "half_open"
                entry.half_open = True
                entry.updated = now
                self._save_locked()
                return True
            if entry.state == "half_open":
                if entry.half_open:
                    return False
                entry.half_open = True
                self._save_locked()
                return True
            return True

    def record_success(self, identity: ModelIdentity) -> None:
        key = breaker_key(identity)
        with self._lock:
            self._entries[key] = BreakerEntry(updated=self.clock())
            self._save_locked()

    def record_failure(self, identity: ModelIdentity, info: ErrorInfo,
                       clock: Optional[float] = None) -> None:
        key = breaker_key(identity)
        now = self.clock() if clock is None else clock
        with self._lock:
            entry = self._entries.get(key) or BreakerEntry()
            entry.last_error = info.detail or info.kind
            entry.updated = now
            if info.kind == "auth":
                entry.disabled = True
                entry.state = "open"
                entry.cooldown_until = max(entry.cooldown_until,
                                           now + self.cooldown)
                self._entries[key] = entry
                self._save_locked()
                return
            if info.kind == "quota_exhausted":
                entry.quota_until = max(entry.quota_until, now + self.quota_cooldown,
                                        now + info.retry_after)
                entry.state = "open"
                entry.half_open = False
                self._entries[key] = entry
                self._save_locked()
                return
            if info.kind == "rate_limit":
                if entry.state == "half_open" or entry.half_open:
                    entry.state = "open"
                    entry.consecutive = max(entry.consecutive, self.threshold)
                else:
                    if not entry.window_start or now - entry.window_start > self.window:
                        entry.window_start = now
                        entry.consecutive = 0
                    entry.consecutive += 1
                if entry.consecutive >= self.threshold or entry.state == "half_open":
                    entry.state = "open"
                    entry.cooldown_until = max(entry.cooldown_until,
                                               now + self.cooldown,
                                               now + info.retry_after)
                entry.half_open = False
                self._entries[key] = entry
                self._save_locked()
                return
            if entry.state == "half_open" or entry.half_open:
                # A failed half-open probe reopens the breaker.
                entry.state = "open"
                entry.half_open = False
                entry.cooldown_until = max(entry.cooldown_until, now + self.cooldown)
            self._entries[key] = entry
            self._save_locked()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {k: v.to_dict() for k, v in self._entries.items()}


# --------------------------------------------------------------------------- #
#  PAYG grants (explicit scope)
# --------------------------------------------------------------------------- #


@dataclass
class PaidGrant:
    scope: str                 # "task" | "run"
    scope_id: str
    keys: frozenset[str]       # identity_key values; {"*"} for any
    max_cost: Optional[Decimal] = None
    spent: Decimal = Decimal("0")
    created: float = 0.0


def request_scope_id(request: "RouteRequest") -> str:
    if request.task_id:
        return f"task:{request.task_id}"
    return f"role:{request.role}:{request.run_id or '-'}"


class GrantStore:
    """In-memory, explicit-scope PAYG approvals (never a global allow flag)."""

    def __init__(self, allow_paid: bool = False):
        self._grants: list[PaidGrant] = []
        if allow_paid:
            self.add("run", "*", {"*"})

    def add(self, scope: str, scope_id: str, keys: Iterable[str],
            max_cost: Optional[Decimal] = None) -> PaidGrant:
        grant = PaidGrant(scope=scope, scope_id=scope_id,
                          keys=frozenset(keys or {"*"}),
                          max_cost=max_cost, created=time.time())
        self._grants.append(grant)
        return grant

    def approve_task(self, request: "RouteRequest", keys: Iterable[str],
                     max_cost: Optional[Decimal] = None) -> PaidGrant:
        return self.add("task", request_scope_id(request), keys, max_cost)

    def allows(self, request: "RouteRequest", identity: ModelIdentity) -> bool:
        if identity.billing != BILLING_PAYG:
            return True
        for grant in self._grants:
            if grant.max_cost is not None and grant.spent >= grant.max_cost:
                continue
            if grant.scope == "run":
                if grant.scope_id not in ("*", request.run_id or ""):
                    continue
            elif grant.scope == "task":
                if grant.scope_id != request_scope_id(request):
                    continue
            else:
                continue
            if "*" in grant.keys or identity_key(identity) in grant.keys:
                return True
        return False

    def charge(self, request: "RouteRequest", identity: ModelIdentity,
               cost: CostEstimate) -> None:
        if identity.billing != BILLING_PAYG or not cost.billable:
            return
        for grant in self._grants:
            if grant.scope == "run":
                if grant.scope_id not in ("*", request.run_id or ""):
                    continue
            elif grant.scope == "task":
                if grant.scope_id != request_scope_id(request):
                    continue
            else:
                continue
            if "*" in grant.keys or identity_key(identity) in grant.keys:
                grant.spent += cost.total
                return


# --------------------------------------------------------------------------- #
#  Route contracts
# --------------------------------------------------------------------------- #


@dataclass
class RouteRequest:
    role: str
    complexity: str = "auto"
    required_capabilities: frozenset[str] = frozenset({"text"})
    estimated_context: int = 0
    billing_policy: str = "free_first"
    task_id: str = ""
    run_id: str = ""
    producer: Optional[ModelRef] = None
    escalation: bool = False
    tag: str = ""
    max_tokens: int = 8192
    thinking: bool = False


@dataclass
class RouteCandidate:
    identity: ModelIdentity
    ref: ModelRef
    handle: Any
    reasons: list[str] = field(default_factory=list)


@dataclass
class RouteResult:
    request: RouteRequest
    candidates: list[RouteCandidate]
    skipped: list[tuple[str, str]] = field(default_factory=list)
    router: Optional["Router"] = None

    @property
    def chain(self) -> "RouteChain":
        return RouteChain(self)


# Curated policy. The Router still filters by identity/capabilities/context/
# billing; this table only expresses the intended order.
NORMAL_CHAIN = [
    ("gemini-plus", "gemini-3.5-flash"),
    ("gemini-plus", "gemini-3.8-flash"),
    ("gemini-plus", "gemini-3.1-pro-preview"),
    ("openrouter", "nvidia/nemotron-3-ultra"),
    ("opencode-go", "gemini-4-argon"),
]
COMPLEX_CHAIN = [
    ("gemini-plus", "gemini-3.1-pro-preview"),
    ("openrouter", "nvidia/nemotron-3-ultra"),
    ("opencode-go", "kimi-k3.1-pro"),
    ("opencode-go", "gemini-4-argon"),
    ("deepseek-api", "deepseek-v4-pro"),
]
ARCH_CHAIN = COMPLEX_CHAIN
SPEC_CHAIN = [
    ("gemini-plus", "gemini-3.1-pro-preview"),
    ("openrouter", "nvidia/nemotron-3-ultra"),
    ("opencode-go", "kimi-k3.1-pro"),
    ("deepseek-api", "deepseek-v4-pro"),
]
FIXER_CHAIN = [
    ("opencode-free", "big-pickle"),
    ("gemini-plus", "gemini-3.1-pro-preview"),
    ("openrouter", "nvidia/nemotron-3-ultra"),
    ("opencode-go", "kimi-k3.1-pro"),
    ("deepseek-api", "deepseek-v4-pro"),
]
REVIEW_CHAIN = [
    ("gemini-plus", "gemini-3.5-flash"),
    ("gemini-plus", "gemini-3.8-flash"),
    ("openrouter", "nvidia/nemotron-3-ultra"),
    ("opencode-go", "gemini-4-argon"),
]
VISUAL_CHAIN = [
    ("gemini-plus", "gemini-3.5-flash"),
    ("gemini-plus", "gemini-3.1-pro-preview"),
]
LOCAL_CHAIN = [("local", "qwen3.6-35b-a3b")]


class Router:
    def __init__(self, registry: ModelRegistry,
                 handle_provider: Callable[[ModelIdentity, str, int, bool], Any],
                 *, breaker: Optional[CircuitBreaker] = None,
                 grants: Optional[GrantStore] = None,
                 availability: Optional[Callable[[ModelIdentity], Optional[bool]]] = None,
                 request_grant: Optional[Callable[[RouteRequest, RouteCandidate], Optional[str]]] = None,
                 allow_unknown: bool = False):
        self.registry = registry
        self.handle_provider = handle_provider
        self.breaker = breaker or CircuitBreaker()
        self.grants = grants or GrantStore()
        self.availability = availability or (lambda _i: None)
        self.request_grant = request_grant
        self.allow_unknown = allow_unknown

    # -- policy ------------------------------------------------------------- #

    def _chain_for(self, request: RouteRequest) -> list[tuple[str, str]]:
        role = (request.role or "").lower()
        cap = {c.lower() for c in request.required_capabilities}
        complexity = (request.complexity or "auto").lower()
        if request.billing_policy == "local_only":
            return LOCAL_CHAIN
        if request.escalation:
            return FIXER_CHAIN if role in ("fixer", "escalation", "senior") else COMPLEX_CHAIN
        if "vision" in cap or role in ("visual", "vision"):
            return VISUAL_CHAIN
        if role in ("fixer", "senior"):
            return FIXER_CHAIN
        if role in ("architecture", "architect", "chief"):
            return ARCH_CHAIN
        if role in ("spec", "specification", "manager", "requirements", "analyst"):
            return SPEC_CHAIN
        if role in ("review", "reviewer"):
            return REVIEW_CHAIN
        if complexity in ("low", "tiny") or role in ("tiny", "local"):
            return LOCAL_CHAIN + NORMAL_CHAIN
        if complexity in ("high", "complex", "hard"):
            return COMPLEX_CHAIN
        return NORMAL_CHAIN

    def route(self, request: RouteRequest) -> RouteResult:
        result = RouteResult(request=request, candidates=[])
        seen: set[tuple[str, str]] = set()
        for profile, model_id in self._chain_for(request):
            key = (profile, model_id)
            if key in seen:
                continue
            seen.add(key)
            identity = self.registry.identity_for(profile, model_id)
            if not identity.defined:
                result.skipped.append((f"{profile}/{model_id}", "não registado"))
                continue
            if identity.billing == BILLING_UNKNOWN and not self.allow_unknown:
                result.skipped.append((f"{profile}/{model_id}", "billing unknown"))
                continue
            if request.billing_policy == "free_only" and identity.billing == BILLING_PAYG:
                result.skipped.append((f"{profile}/{model_id}", "payg excluído pela política"))
                continue
            if not set(request.required_capabilities).issubset(identity.capabilities):
                result.skipped.append((f"{profile}/{model_id}", "capacidade em falta"))
                continue
            if (request.estimated_context and identity.context_window
                    and request.estimated_context > identity.context_window):
                result.skipped.append((f"{profile}/{model_id}", "contexto excede janela"))
                continue
            producer = request.producer
            if (request.escalation and producer is not None
                    and identity.credential_profile == producer.credential_profile
                    and identity.model_id == producer.model_id):
                result.skipped.append((f"{profile}/{model_id}", "produtor excluído na escalação"))
                continue
            available = self.availability(identity)
            if identity.credential_profile == "opencode-free" and available is not True:
                result.skipped.append((f"{profile}/{model_id}", "big-pickle indisponível"))
                continue
            try:
                handle = self.handle_provider(identity, request.tag or request.role.upper(),
                                              request.max_tokens, request.thinking)
            except Exception as exc:
                result.skipped.append((f"{profile}/{model_id}", f"handle: {exc}"))
                handle = None
            if handle is None:
                result.skipped.append((f"{profile}/{model_id}", "endpoint indisponível"))
                continue
            ref = self.registry.ref_for_identity(identity, available=available)
            result.candidates.append(RouteCandidate(identity, ref, handle))
        result.router = self
        return result

    # -- grants ------------------------------------------------------------- #

    def authorize(self, request: RouteRequest, candidate: RouteCandidate) -> bool:
        identity = candidate.identity
        if identity.billing == BILLING_UNKNOWN:
            raise UnknownModelError(
                f"billing desconhecido para {identity.credential_profile}/"
                f"{identity.model_id}")
        if identity.billing != BILLING_PAYG:
            return True
        if self.grants.allows(request, identity):
            return True
        chosen = self.request_grant(request, candidate) if self.request_grant else None
        if not chosen:
            return False
        key = None
        if chosen == "*" or chosen == identity_key(identity):
            key = identity_key(identity)
        elif chosen == identity.model_id:
            key = identity_key(identity)
        else:
            matches = [i for i in self.registry.models.values()
                       if i.model_id == chosen and i.billing == BILLING_PAYG]
            if len(matches) == 1:
                key = identity_key(matches[0])
        if key is None:
            return False
        self.grants.approve_task(request, {key})
        return self.grants.allows(request, identity)

    def charge(self, request: RouteRequest, candidate: RouteCandidate,
               response: Any) -> CostEstimate:
        usage = getattr(response, "usage_metadata", None) or {}
        in_t = int(usage.get("input_tokens") or usage.get("input_token_count") or 0)
        out_t = int(usage.get("output_tokens") or usage.get("output_token_count") or 0)
        cost = self.registry.estimate(candidate.identity, in_t, out_t)
        self.grants.charge(request, candidate.identity, cost)
        return cost


def _content(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content or "")


class ReplayedResponse:
    """A previously confirmed response, replayed without contacting a provider."""

    def __init__(self, text: str, served: Any = None):
        self.content = text
        self.usage_metadata: dict[str, Any] = {}
        self.response_metadata = {
            "model_name": getattr(served, "model_id", "") or "",
            "replayed": True,
        }


class RouteChain:
    """Executes candidates candidate-by-candidate (fallback only)."""

    is_route_chain = True

    # An ambiguous outcome may already have been processed and billed. For a
    # PAYG candidate we stop instead of retrying/falling back silently.
    AMBIGUOUS_KINDS = {"timeout", "connection", "server", "empty", "unknown"}

    def __init__(self, result: RouteResult):
        self.result = result
        self.request = result.request
        self.router = result.router

    @property
    def model_refs(self) -> list[ModelRef]:
        return [c.ref for c in self.result.candidates]

    def invoke(self, messages: Any) -> Any:
        last_error: Optional[BaseException] = None
        denied_paid = False
        ledger = getattr(self.router, "call_ledger", None)
        avoid: set[tuple[str, str]] = set()
        fingerprint = invocation_fingerprint(messages, self.request)
        if ledger is not None and self.request.run_id:
            avoid = ledger.failed_refs(
                self.request.run_id, self.request.task_id,
                self.request.tag or self.request.role)
            replay = ledger.find_replay(
                self.request.run_id, self.request.task_id,
                self.request.tag or self.request.role, fingerprint)
            if replay is not None:
                text = ledger.result_text(replay.call_id)
                if text is not None:
                    served = (ModelRef.from_dict(replay.served)
                              if replay.served else None)
                    set_call_context(replay.call_id, served)
                    return ReplayedResponse(text, served)
        for candidate in self.result.candidates:
            identity = candidate.identity
            if (identity.credential_profile, identity.model_id) in avoid:
                continue
            if self.router is not None and not self.router.breaker.allow(identity):
                continue
            try:
                allowed = self.router.authorize(self.request, candidate)
            except UnknownModelError:
                raise
            if not allowed:
                denied_paid = True
                continue
            for attempt in range(2):
                record = None
                if (ledger is not None and identity.billing == BILLING_PAYG
                        and self.request.run_id):
                    record = ledger.intent(
                        run_id=self.request.run_id,
                        role=self.request.role,
                        ref=candidate.ref,
                        task=self.request.task_id,
                        tag=self.request.tag or self.request.role,
                        request_sha256=fingerprint,
                    )
                    ledger.submitted(record.call_id)
                try:
                    response = candidate.handle.invoke(messages)
                    content = _content(response)
                    if not content.strip():
                        raise RuntimeError("empty response from model")
                except Exception as exc:
                    info = classify_error(exc)
                    ambiguous = info.kind in self.AMBIGUOUS_KINDS
                    if record is not None:
                        ledger.fail(record.call_id, str(exc), info.kind,
                                    ambiguous=ambiguous)
                    self.router.breaker.record_failure(identity, info)
                    last_error = exc
                    if ambiguous and record is not None:
                        raise CallOutcomeUnknown(
                            f"{self.request.role}: resultado ambíguo em "
                            f"{identity.model_id} ({info.kind}); resolução "
                            f"manual necessária (call {record.call_id})") from exc
                    if info.kind == "bad_request":
                        raise
                    if info.kind in ("timeout", "empty") and attempt == 0:
                        continue
                    break
                self.router.breaker.record_success(identity)
                if record is not None:
                    raw_content = getattr(response, "content", None)
                    tool_calls = (getattr(response, "tool_calls", None)
                                  or (getattr(response, "additional_kwargs", {}) or {})
                                  .get("tool_calls"))
                    replayable = isinstance(raw_content, str) and not tool_calls
                    set_call_context(record.call_id, candidate.ref)
                    ledger.confirm(record.call_id, served=candidate.ref,
                                   text=content, replayable=replayable)
                self.router.charge(self.request, candidate, response)
                return response
        if denied_paid:
            raise BudgetSuspended(
                f"{self.request.role}: modelo PAYG sem autorização para esta tarefa")
        if last_error is not None:
            raise last_error
        reasons = "; ".join(f"{name}: {why}" for name, why in self.result.skipped[:6])
        raise RouterExhausted(
            f"não há candidatos disponíveis para {self.request.role}"
            + (f" ({reasons})" if reasons else ""))

def bind_chain(result: RouteResult, router: Router) -> RouteChain:
    result.router = router
    return result.chain
