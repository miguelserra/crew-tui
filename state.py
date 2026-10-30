"""Canonical execution state for the unified engine (stdlib only).

Two identities are kept deliberately separate:

* ``engine_kind`` — which engine runs the resumed execution (legacy|unified);
* ``engine_version`` — which implementation/version wrote the state.

A corrupt state never produces an empty state: loaders raise
``StateCorruptError`` and callers must not overwrite the original file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from registry import ModelRef

SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1

ENGINE_LEGACY = "legacy"
ENGINE_UNIFIED = "unified"
ENGINE_KINDS = {ENGINE_LEGACY, ENGINE_UNIFIED}

PHASES = (
    "SPEC",
    "ARCHITECTURE",
    "SCAFFOLD",
    "BUILD",
    "VERIFY",
    "INTEGRATE",
    "VISUAL",
    "SIGNOFF",
    "DONE",
    "PAUSED",
)

TASK_KINDS = {"build", "review", "prose"}
TASK_STATUSES = {
    "pending",
    "running",
    "built",
    "verified",
    "failed",
    "failed-verification",
    "skipped",
    "done",
}


class StateError(Exception):
    pass


class StateCorruptError(StateError):
    def __init__(self, path: str, detail: str):
        super().__init__(f"estado ilegível em {path}: {detail}; ficheiro preservado")
        self.path = path


class StateSchemaError(StateError):
    pass


class StateValidationError(StateError):
    pass


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_id(prefix: str = "") -> str:
    return f"{prefix}{int(time.time() * 1000):x}{time.perf_counter_ns() % 100000:05x}"


def sha256_json(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
#  Attempts
# --------------------------------------------------------------------------- #


@dataclass
class TaskAttempt:
    attempt_id: str
    model: ModelRef
    started_at: str = ""
    completed_at: Optional[str] = None
    outcome: str = "unknown"  # ok|error|timeout|quota|historical|rejected
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "model": self.model.to_dict(),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "outcome": self.outcome,
            "errors": list(self.errors),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "TaskAttempt":
        data = data or {}
        return cls(
            attempt_id=str(data.get("attempt_id") or new_id("att_")),
            model=ModelRef.from_dict(data.get("model")),
            started_at=str(data.get("started_at") or ""),
            completed_at=data.get("completed_at"),
            outcome=str(data.get("outcome") or "unknown"),
            errors=[str(e) for e in (data.get("errors") or [])],
        )


# --------------------------------------------------------------------------- #
#  Contracts
# --------------------------------------------------------------------------- #


@dataclass
class RequirementContract:
    objective: str = ""
    scope_in: list[str] = field(default_factory=list)
    scope_out: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    non_negotiables: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    answers: list[dict[str, str]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out = dict(self.raw)
        out.update({
            "objective": self.objective,
            "scope_in": list(self.scope_in),
            "scope_out": list(self.scope_out),
            "constraints": list(self.constraints),
            "non_negotiables": list(self.non_negotiables),
            "acceptance": list(self.acceptance),
            "assumptions": list(self.assumptions),
            "open_questions": list(self.open_questions),
            "answers": list(self.answers),
        })
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RequirementContract":
        data = data or {}
        return cls(
            objective=str(data.get("objective") or data.get("goal") or ""),
            scope_in=[str(x) for x in (data.get("scope_in") or [])],
            scope_out=[str(x) for x in (data.get("scope_out") or [])],
            constraints=[str(x) for x in (data.get("constraints") or [])],
            non_negotiables=[str(x) for x in (data.get("non_negotiables") or [])],
            acceptance=[str(x) for x in (data.get("acceptance")
                                         or data.get("acceptance_criteria") or [])],
            assumptions=[str(x) for x in (data.get("assumptions") or [])],
            open_questions=[str(x) for x in (data.get("open_questions")
                                             or data.get("questions") or [])],
            answers=[dict(x) for x in (data.get("answers") or [])],
            raw=dict(data),
        )


@dataclass
class ArchitectureContract:
    approach: str = ""
    modules: list[dict[str, Any]] = field(default_factory=list)
    interfaces: list[dict[str, Any]] = field(default_factory=list)
    test_plan: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out = dict(self.raw)
        out.update({
            "approach": self.approach,
            "modules": list(self.modules),
            "interfaces": list(self.interfaces),
            "test_plan": list(self.test_plan),
        })
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ArchitectureContract":
        data = data or {}
        return cls(
            approach=str(data.get("approach") or ""),
            modules=[dict(x) for x in (data.get("modules") or [])],
            interfaces=[dict(x) for x in (data.get("interfaces") or [])],
            test_plan=[dict(x) for x in (data.get("test_plan") or [])],
            raw=dict(data),
        )

    def content_hash(self) -> str:
        """Hash only the architecture content, not volatile metadata."""
        return sha256_json({
            "approach": self.approach,
            "modules": self.modules,
            "interfaces": self.interfaces,
            "test_plan": self.test_plan,
        })


# --------------------------------------------------------------------------- #
#  Task
# --------------------------------------------------------------------------- #


def normalize_target_file(value: str) -> str:
    """Normalize a project-relative target path and reject escapes."""
    raw = (value or "").strip().replace("\\", "/")
    if not raw:
        return ""
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise StateValidationError(f"target_file absoluto proibido: {value!r}")
    path = PurePosixPath(raw)
    if any(part in ("..", "") for part in path.parts):
        raise StateValidationError(f"target_file com '..' ou segmento vazio: {value!r}")
    if str(path) in (".", ""):
        raise StateValidationError(f"target_file inválido: {value!r}")
    return str(path)


@dataclass
class Task:
    id: str
    title: str = ""
    description: str = ""
    kind: str = "build"  # build|review|prose
    team: str = "programming"
    role: str = "senior"
    complexity: str = "medium"
    target_file: str = ""
    acceptance: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending"
    errors: list[str] = field(default_factory=list)
    attempts: list[TaskAttempt] = field(default_factory=list)
    delivered_by: Optional[ModelRef] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "kind": self.kind,
            "team": self.team,
            "role": self.role,
            "complexity": self.complexity,
            "target_file": self.target_file,
            "acceptance": list(self.acceptance),
            "depends_on": list(self.depends_on),
            "status": self.status,
            "errors": list(self.errors),
            "attempts": [a.to_dict() for a in self.attempts],
            "delivered_by": self.delivered_by.to_dict() if self.delivered_by else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Task":
        data = data or {}
        return cls(
            id=str(data.get("id") or new_id("t_")),
            title=str(data.get("title") or ""),
            description=str(data.get("description") or ""),
            kind=str(data.get("kind") or ("build" if data.get("target_file") else "prose")),
            team=str(data.get("team") or "programming"),
            role=str(data.get("role") or "senior"),
            complexity=str(data.get("complexity") or "medium"),
            target_file=str(data.get("target_file") or ""),
            acceptance=[str(x) for x in (data.get("acceptance") or [])],
            depends_on=[str(x) for x in (data.get("depends_on") or [])],
            status=str(data.get("status") or "pending"),
            errors=[str(x) for x in (data.get("errors") or [])],
            attempts=[TaskAttempt.from_dict(x) for x in (data.get("attempts") or [])],
            delivered_by=(ModelRef.from_dict(data["delivered_by"])
                          if data.get("delivered_by") else None),
        )


def _inside_root(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
    except Exception:
        return False
    root_s = os.path.normcase(str(root.resolve()))
    dest_s = os.path.normcase(str(resolved))
    return dest_s == root_s or dest_s.startswith(root_s + os.sep)


def validate_tasks(tasks: list[Task],
                   project_root: str | Path | None = None) -> list[Task]:
    """Validate target paths and enforce one file per build task.

    Review/prose tasks may omit ``target_file``. Uniqueness is
    case-insensitive so a project behaves the same on Linux and Windows.
    When ``project_root`` is known, the *resolved* path must stay inside it
    (a symlink can otherwise escape an apparently relative path).
    """
    root = Path(project_root).expanduser().resolve() if project_root else None
    seen: dict[str, str] = {}
    normalized: list[Task] = []
    for task in tasks:
        if task.kind not in TASK_KINDS:
            raise StateValidationError(f"tarefa {task.id}: kind inválido {task.kind!r}")
        if task.kind == "build":
            if not task.target_file.strip():
                raise StateValidationError(f"tarefa {task.id}: build exige target_file")
        target = normalize_target_file(task.target_file)
        task.target_file = target
        if target and root is not None:
            if not _inside_root(root / target, root):
                raise StateValidationError(
                    f"target_file escapa da raiz do projeto (symlink?): {target!r}")
        if target:
            key = target.casefold()
            if task.kind == "build":
                if key in seen:
                    raise StateValidationError(
                        f"target_file duplicado (case-insensitive): {target!r} "
                        f"em {seen[key]} e {task.id}")
                seen[key] = task.id
        normalized.append(task)
    return normalized


# --------------------------------------------------------------------------- #
#  Approvals / pauses / verification
# --------------------------------------------------------------------------- #


@dataclass
class Approval:
    kind: str
    artifact_hash: str = ""
    actor: str = "human"
    decision: str = "approved"  # approved|rejected
    timestamp: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Approval":
        data = data or {}
        return cls(
            kind=str(data.get("kind") or ""),
            artifact_hash=str(data.get("artifact_hash") or ""),
            actor=str(data.get("actor") or "human"),
            decision=str(data.get("decision") or "approved"),
            timestamp=str(data.get("timestamp") or ""),
            detail=str(data.get("detail") or ""),
        )


@dataclass
class PauseState:
    reason: str = ""
    phase: str = ""
    since: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PauseState":
        data = data or {}
        return cls(
            reason=str(data.get("reason") or ""),
            phase=str(data.get("phase") or ""),
            since=str(data.get("since") or ""),
            detail=str(data.get("detail") or ""),
        )


@dataclass
class VerificationRecord:
    verdict: str = "UNVERIFIED"  # PASS|FAIL|UNVERIFIED|SKIP
    oracle: dict[str, Any] = field(default_factory=dict)
    critic: dict[str, Any] = field(default_factory=dict)
    visual: dict[str, Any] = field(default_factory=dict)
    integration: dict[str, Any] = field(default_factory=dict)
    tests: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "VerificationRecord":
        data = data or {}
        return cls(
            verdict=str(data.get("verdict") or "UNVERIFIED"),
            oracle=dict(data.get("oracle") or {}),
            critic=dict(data.get("critic") or {}),
            visual=dict(data.get("visual") or {}),
            integration=dict(data.get("integration") or {}),
            tests=dict(data.get("tests") or {}),
            reason=str(data.get("reason") or ""),
            at=str(data.get("at") or ""),
        )


# --------------------------------------------------------------------------- #
#  Project state
# --------------------------------------------------------------------------- #


@dataclass
class ProjectState:
    run_id: str
    project_root: str = ""
    goal: str = ""
    profile: str = "auto"
    phase: str = "SPEC"
    schema_version: int = SCHEMA_VERSION
    engine_kind: str = ENGINE_UNIFIED
    engine_version: str = ""
    requirements: RequirementContract = field(default_factory=RequirementContract)
    architecture: ArchitectureContract = field(default_factory=ArchitectureContract)
    scaffold: dict[str, Any] = field(default_factory=dict)
    tasks: list[Task] = field(default_factory=list)
    integration: dict[str, Any] = field(default_factory=dict)
    visual: dict[str, Any] = field(default_factory=dict)
    signoff: dict[str, Any] = field(default_factory=dict)
    approvals: list[Approval] = field(default_factory=list)
    verifications: dict[str, VerificationRecord] = field(default_factory=dict)
    pause: PauseState = field(default_factory=PauseState)
    history: list[dict[str, Any]] = field(default_factory=list)
    migrations: list[dict[str, Any]] = field(default_factory=list)
    created: str = ""
    updated: str = ""

    def __post_init__(self) -> None:
        if self.engine_kind not in ENGINE_KINDS:
            raise StateValidationError(f"engine_kind inválido: {self.engine_kind!r}")
        if self.phase not in PHASES:
            raise StateValidationError(f"phase inválida: {self.phase!r}")
        self.tasks = validate_tasks(self.tasks, project_root=self.project_root or None)
        if not self.created:
            self.created = utc_now()
        if not self.updated:
            self.updated = self.created

    # -- architecture approval -------------------------------------------- #

    def architecture_hash(self) -> str:
        return self.architecture.content_hash()

    def architecture_approved(self) -> bool:
        h = self.architecture_hash()
        return any(a.kind == "architecture" and a.decision == "approved"
                   and a.artifact_hash == h for a in self.approvals)

    def approve_architecture(self, actor: str = "human", detail: str = "") -> Approval:
        approval = Approval(kind="architecture", artifact_hash=self.architecture_hash(),
                            actor=actor, decision="approved",
                            timestamp=utc_now(), detail=detail)
        self.approvals.append(approval)
        self.pause = PauseState()
        return approval

    def unknown_billing_approved(self, ref: ModelRef) -> bool:
        return any(a.kind == "unknown_billing" and a.decision == "approved"
                   and a.artifact_hash in ("", sha256_json(ref.to_dict()))
                   for a in self.approvals)

    def approve_unknown_billing(self, ref: ModelRef, actor: str = "human") -> Approval:
        approval = Approval(kind="unknown_billing",
                            artifact_hash=sha256_json(ref.to_dict()),
                            actor=actor, decision="approved", timestamp=utc_now())
        self.approvals.append(approval)
        return approval

    def tighten_architecture_gate(self) -> bool:
        """Re-close the gate if the architecture changed after approval."""
        if not self.architecture_approved():
            self.pause = PauseState(reason="architecture_approval",
                                    phase="ARCHITECTURE", since=utc_now())
            return True
        return False

    # -- serialization ----------------------------------------------------- #

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "project_root": self.project_root,
            "goal": self.goal,
            "profile": self.profile,
            "phase": self.phase,
            "schema_version": SCHEMA_VERSION,
            "engine_kind": self.engine_kind,
            "engine_version": self.engine_version,
            "requirements": self.requirements.to_dict(),
            "architecture": self.architecture.to_dict(),
            "scaffold": self.scaffold,
            "tasks": [t.to_dict() for t in self.tasks],
            "integration": self.integration,
            "visual": self.visual,
            "signoff": self.signoff,
            "approvals": [a.to_dict() for a in self.approvals],
            "verifications": {k: v.to_dict() for k, v in self.verifications.items()},
            "pause": self.pause.to_dict(),
            "history": self.history,
            "migrations": self.migrations,
            "created": self.created,
            "updated": utc_now(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ProjectState":
        if not isinstance(data, dict):
            raise StateSchemaError("estado não é um objeto JSON")
        schema = int(data.get("schema_version") or 0)
        if schema > SCHEMA_VERSION:
            raise StateSchemaError(
                f"schema_version {schema} > suportado {SCHEMA_VERSION}")
        kind = str(data.get("engine_kind") or "")
        if not kind:
            raise StateSchemaError("estado sem engine_kind")
        return cls(
            run_id=str(data.get("run_id") or new_id("run_")),
            project_root=str(data.get("project_root") or ""),
            goal=str(data.get("goal") or ""),
            profile=str(data.get("profile") or "auto"),
            phase=str(data.get("phase") or "SPEC"),
            schema_version=SCHEMA_VERSION,
            engine_kind=kind,
            engine_version=str(data.get("engine_version") or ""),
            requirements=RequirementContract.from_dict(data.get("requirements")),
            architecture=ArchitectureContract.from_dict(data.get("architecture")),
            scaffold=dict(data.get("scaffold") or {}),
            tasks=[Task.from_dict(t) for t in (data.get("tasks") or [])],
            integration=dict(data.get("integration") or {}),
            visual=dict(data.get("visual") or {}),
            signoff=dict(data.get("signoff") or {}),
            approvals=[Approval.from_dict(a) for a in (data.get("approvals") or [])],
            verifications={str(k): VerificationRecord.from_dict(v)
                           for k, v in (data.get("verifications") or {}).items()},
            pause=PauseState.from_dict(data.get("pause")),
            history=[dict(x) for x in (data.get("history") or [])],
            migrations=[dict(x) for x in (data.get("migrations") or [])],
            created=str(data.get("created") or ""),
            updated=str(data.get("updated") or ""),
        )


# --------------------------------------------------------------------------- #
#  Legacy migration (format only; engine_kind stays legacy by default)
# --------------------------------------------------------------------------- #

# old factory step -> next phase. The mapping depends on the engine that will
# continue the execution: migrated legacy runs keep the old order, while
# unified runs use the corrected architecture-before-scaffold order.
LEGACY_STEP_TO_PHASE_LEGACY = {
    "REQUIREMENTS": "SCAFFOLD",
    "SCAFFOLD": "ARCHITECTURE",
    "ARCHITECT": "BUILD",
    "BUILD": "BUILD",
    "INTEGRATE": "INTEGRATE",
    "VISUAL": "VISUAL",
    "SIGN_OFF": "SIGNOFF",
    "DONE": "DONE",
}
LEGACY_STEP_TO_PHASE_UNIFIED = {
    "REQUIREMENTS": "ARCHITECTURE",
    "SCAFFOLD": "ARCHITECTURE",   # old scaffold-before-architect defect
    "ARCHITECT": "SCAFFOLD",      # architecture done; scaffold must run after
    "BUILD": "BUILD",
    "INTEGRATE": "INTEGRATE",
    "VISUAL": "VISUAL",
    "SIGN_OFF": "SIGNOFF",
    "DONE": "DONE",
}


def _legacy_task_to_task(data: dict[str, Any]) -> Task:
    target = str(data.get("target_file") or "")
    kind = "build" if target else "prose"
    attempts: list[TaskAttempt] = []
    raw_attempts = data.get("attempt_log")
    if not isinstance(raw_attempts, list):
        raw_attempts = data.get("attempts")
    if isinstance(raw_attempts, list):
        for raw in raw_attempts:
            if isinstance(raw, dict):
                attempts.append(TaskAttempt.from_dict(raw))
    delivered = data.get("delivered_by")
    return Task(
        id=str(data.get("id") or new_id("t_")),
        title=str(data.get("title") or ""),
        description=str(data.get("description") or ""),
        kind=kind,
        team=str(data.get("team") or "programming"),
        role=str(data.get("role") or "senior"),
        complexity=str(data.get("complexity") or "medium"),
        target_file=target,
        acceptance=[str(x) for x in (data.get("acceptance") or [])],
        depends_on=[str(x) for x in (data.get("depends_on") or [])],
        status=str(data.get("status") or "pending"),
        errors=[str(x) for x in (data.get("errors") or [])],
        attempts=attempts,
        delivered_by=(ModelRef.from_dict(delivered) if delivered else None),
    )


def from_legacy_factory(data: dict[str, Any],
                        engine_kind: str = ENGINE_LEGACY,
                        engine_version: str = "legacy") -> ProjectState:
    """Convert an old FactoryState dict into schema v2 without touching the file."""
    if not isinstance(data, dict):
        raise StateSchemaError("FactoryState não é um objeto")
    step = str(data.get("step") or "REQUIREMENTS")
    step_map = (LEGACY_STEP_TO_PHASE_UNIFIED if engine_kind == ENGINE_UNIFIED
                else LEGACY_STEP_TO_PHASE_LEGACY)
    phase = step_map.get(step, "BUILD")
    has_req = bool(data.get("requirements"))
    has_arch = bool(data.get("architecture"))
    if not has_req:
        phase = "SPEC"
    elif step == "REQUIREMENTS":
        phase = "ARCHITECTURE" if engine_kind == ENGINE_UNIFIED else "SCAFFOLD"
    elif step == "SCAFFOLD" and not has_arch:
        phase = "ARCHITECTURE"
    elif step == "ARCHITECT" and not has_arch:
        phase = "ARCHITECTURE"
    req = RequirementContract.from_dict(data.get("requirements"))
    if data.get("answers") and not req.answers:
        req.answers = [dict(x) for x in data.get("answers") or []]
    arch = ArchitectureContract.from_dict(data.get("architecture"))
    tasks = [_legacy_task_to_task(t) for t in (data.get("tasks") or [])]
    st = ProjectState(
        run_id=str(data.get("run_id") or new_id("run_")),
        project_root=str(data.get("project") or data.get("project_root") or ""),
        goal=str((data.get("requirements") or {}).get("objective") or data.get("goal") or ""),
        phase=phase,
        engine_kind=engine_kind,
        engine_version=engine_version,
        requirements=req,
        architecture=arch,
        scaffold=dict(data.get("scaffold") or {}),
        tasks=tasks,
        integration=dict(data.get("integration") or {}),
        visual=dict(data.get("visual") or {}),
        signoff=dict(data.get("signoff") or {}),
        pause=PauseState(reason=str(data.get("pause_reason") or ""),
                         phase=phase if data.get("pause_reason") else "",
                         since=utc_now() if data.get("pause_reason") else ""),
        history=[dict(x) for x in (data.get("history") or [])],
        created=str(data.get("created") or ""),
        updated=str(data.get("updated") or ""),
    )
    st.migrations.append({
        "from": "factory", "to": "project", "at": utc_now(),
        "legacy_step": step, "legacy_schema": LEGACY_SCHEMA_VERSION,
    })
    return st


def from_legacy_session(session: dict[str, Any],
                        spec: Optional[dict[str, Any]] = None,
                        engine_kind: str = ENGINE_LEGACY) -> ProjectState:
    """Convert an Orchestrator session (+ optional spec) into schema v2."""
    if not isinstance(session, dict):
        raise StateSchemaError("sessão não é um objeto")
    spec = spec or {}
    tasks: list[Task] = []
    for raw in spec.get("tasks") or []:
        target = str(raw.get("target_file") or "")
        kind = "review" if spec.get("review") else ("build" if target else "prose")
        tasks.append(Task(
            id=str(raw.get("id") or new_id("t_")),
            title=str(raw.get("title") or ""),
            description=str(raw.get("description") or ""),
            kind=kind,
            team=str(raw.get("team") or "programming"),
            target_file=target,
            acceptance=[str(x) for x in (raw.get("acceptance") or [])],
        ))
    results = session.get("results") or {}
    if results:
        for t in tasks:
            r = results.get(t.id) or {}
            if r.get("status"):
                t.status = str(r["status"])
    phase = "DONE" if tasks and all(t.status in ("verified", "verified-degraded")
                                    for t in tasks) else ("SPEC" if not tasks else "BUILD")
    st = ProjectState(
        run_id=str(session.get("run_id") or new_id("run_")),
        project_root=str(session.get("project_root") or ""),
        goal=str(session.get("goal") or spec.get("goal") or ""),
        phase=phase,
        engine_kind=engine_kind,
        engine_version="legacy",
        requirements=RequirementContract.from_dict(spec),
        tasks=tasks,
        history=[dict(x) for x in (session.get("history") or [])],
    )
    st.migrations.append({"from": "session", "to": "project", "at": utc_now()})
    return st


def with_attempt(task: Task, attempt: TaskAttempt) -> None:
    task.attempts.append(attempt)
    if attempt.outcome == "ok":
        task.delivered_by = attempt.model


def summary_dict(state: ProjectState) -> dict[str, Any]:
    """Small status payload for dashboards and logs."""
    return {
        "run_id": state.run_id,
        "engine_kind": state.engine_kind,
        "engine_version": state.engine_version,
        "schema_version": state.schema_version,
        "phase": state.phase,
        "tasks": len(state.tasks),
        "verified": sum(1 for t in state.tasks if t.status in ("verified", "done")),
        "paused": state.pause.reason,
    }
