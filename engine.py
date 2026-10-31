"""Engine selection, approvals and workflow adapters (stdlib only).

``engine_kind`` decides which engine resumes a run. ``engine_version`` is the
implementation version that wrote the state. They are never conflated.

P1 is a strangler: the unified engine drives the existing Factory steps via an
adapter, in the corrected phase order, persisting only ``.crew/state.json``.
The legacy engine keeps its old order and ``.factory`` persistence.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol

from attempts import AttemptEvent
from registry import BILLING_UNKNOWN, ModelRef
from state import (
    ENGINE_LEGACY,
    ENGINE_UNIFIED,
    Approval,
    ArchitectureContract,
    PauseState,
    PHASES,
    SCHEMA_VERSION,
    ProjectState,
    RequirementContract,
    StateValidationError,
    Task,
    VerificationRecord,
    sha256_json,
    TaskAttempt,
    new_id,
    link_traceability,
    validate_traceability,
    validate_dependencies,
    utc_now,
)
from store import ResumeTarget, StateManager, atomic_write_json

ENGINE_VERSION = "1.0"


class UnknownBilling(Exception):
    """A credential with unknown billing was about to execute without approval."""


class EngineError(Exception):
    pass


def resolve_engine(value: Optional[str] = None) -> str:
    """Read CREW_ENGINE once for *new* executions. Invalid values are fatal."""
    raw = (value if value is not None else os.environ.get("CREW_ENGINE", ENGINE_LEGACY))
    kind = (raw or "").strip().lower()
    if kind in ("", ENGINE_LEGACY):
        return ENGINE_LEGACY
    if kind == ENGINE_UNIFIED:
        return ENGINE_UNIFIED
    raise EngineError(
        f"CREW_ENGINE inválido: {raw!r} (esperado 'legacy' ou 'unified')")


def iter_model_refs(llm: Any) -> list[ModelRef]:
    """Collect the effective refs of a handle or fallback chain."""
    out: list[ModelRef] = []
    seen: set[str] = set()

    def add(ref: ModelRef) -> None:
        key = f"{ref.provider}|{ref.endpoint}|{ref.model_id}|{ref.credential_profile}"
        if key not in seen:
            seen.add(key)
            out.append(ref)

    def walk(obj: Any) -> None:
        if obj is None:
            return
        ref = getattr(obj, "_model_ref", None)
        if isinstance(ref, ModelRef):
            add(ref)
            return
        refs = getattr(obj, "model_refs", None)
        if isinstance(refs, list):
            for item in refs:
                if isinstance(item, ModelRef):
                    add(item)
            return
        for candidate in getattr(obj, "candidates", []) or []:
            cref = getattr(candidate, "ref", None)
            if isinstance(cref, ModelRef):
                add(cref)
            else:
                walk(candidate)
        runnable = getattr(obj, "runnable", None)
        if runnable is not None:
            walk(runnable)
        for fallback in getattr(obj, "fallbacks", None) or []:
            walk(fallback)

    walk(llm)
    return out


@dataclass
class ExecDecision:
    engine_kind: str
    path: Path
    state: Optional[ProjectState] = None
    legacy_raw: Optional[dict[str, Any]] = None


class WorkflowEngine:
    """Phase driver over an adapter. Persists exactly one canonical state."""

    def __init__(self, state: ProjectState, store: StateManager, adapter: "WorkflowAdapter",
                 emit: Optional[Any] = None, allow_unknown: bool = False,
                 persistence: str = "crew"):
        self.state = state
        self.store = store
        self.adapter = adapter
        self.emit = emit or (lambda *a, **k: None)
        self.allow_unknown = allow_unknown
        self.persistence = persistence if persistence in ("crew", "factory") else "crew"
        self._last_ok: dict[str, ModelRef] = {}

    # -- pool wiring -------------------------------------------------------- #

    def apply_pool(self, pool: Any) -> None:
        if pool is None:
            return
        pool.dispatch_guard = self.check_refs
        pool.attempt_sink = self.on_attempt

    def check_refs(self, refs: Iterable[ModelRef]) -> None:
        for ref in refs:
            if ref.billing != BILLING_UNKNOWN:
                continue
            if self.allow_unknown:
                continue
            if self.state.unknown_billing_approved(ref):
                continue
            raise UnknownBilling(
                f"billing desconhecido para {ref.provider}/{ref.endpoint}/"
                f"{ref.model_id}; autoriza explicitamente (--allow-unknown)")

    # -- attempts ----------------------------------------------------------- #

    def on_attempt(self, event: AttemptEvent) -> None:
        attempt: TaskAttempt = event.attempt
        if event.tag in ("CRITIC", "VISUAL", "READER", "ORACLE", "TEST"):
            self.state.history.append({"kind": "attempt", **event.to_dict(), "at": utc_now()})
            self.save()
            return
        task = next((t for t in self.state.tasks if t.id == event.task), None)
        if task is None:
            self.state.history.append({"kind": "attempt", **event.to_dict(), "at": utc_now()})
        else:
            task.attempts.append(attempt)
            if attempt.outcome == "ok":
                self._last_ok[task.id] = attempt.model
                task.delivered_by = attempt.model
                self.adapter.on_last_ok(task.id, attempt.model)
        self.save()

    def mark_delivered(self, task_id: str) -> None:
        task = next((t for t in self.state.tasks if t.id == task_id), None)
        if task is None:
            return
        ref = self._last_ok.get(task_id)
        if ref is not None:
            task.delivered_by = ref
            self.save()

    # -- persistence -------------------------------------------------------- #

    def save(self) -> None:
        self.state.updated = utc_now()
        if self.persistence == "factory" and self.state.project_root:
            self.store.save_factory_from_project(self.state.project_root, self.state)
            return
        if self.state.project_root:
            final = self.store.project_state_path(self.state.project_root)
            staging = self.store.staging_path(self.state.run_id)
            if staging.is_file() and not final.is_file():
                self.store.promote_staging(staging, self.state.project_root, self.state)
            else:
                self.store.save_project(self.state)
        else:
            self.store.save_staging(self.state)

    # -- approvals ---------------------------------------------------------- #

    def _has_approval(self, kind: str) -> bool:
        if kind == "spec":
            return self.state.spec_approved()
        return any(a.kind == kind and a.decision == "approved" for a in self.state.approvals)

    def approve_spec(self, actor: str = "human") -> None:
        self.state.approvals.append(Approval(kind="spec", actor=actor,
                                            artifact_hash=self.state.requirements.content_hash(),
                                            decision="approved", timestamp=utc_now()))
        if self.state.pause.reason in ("spec_questions", "spec_approval"):
            self.state.pause = PauseState()
        if self.state.phase == "SPEC" and self.adapter is not None:
            self.state.phase = self.adapter.next_phase("SPEC")
        self.save()

    def approve_architecture(self, actor: str = "human") -> None:
        self.state.approve_architecture(actor=actor)
        self.state.pause = PauseState()
        if self.state.phase == "ARCHITECTURE":
            self.state.phase = self.adapter.next_phase("ARCHITECTURE")
        self.save()

    def approve_unknown_billing(self, ref: ModelRef, actor: str = "human") -> None:
        self.state.approve_unknown_billing(ref, actor=actor)
        self.save()

    def _pause(self, reason: str, detail: str = "") -> None:
        self.state.pause = PauseState(reason=reason, phase=self.state.phase,
                                      since=utc_now(), detail=detail)
        self.save()

    # -- phase loop --------------------------------------------------------- #

    def resume(self) -> None:
        if not self.state.pause.reason:
            return
        if self.state.pause.reason in ("spec_questions", "spec_approval"):
            if self._has_approval("spec"):
                self.state.pause = PauseState()
                self.state.phase = self.adapter.next_phase("SPEC")
        elif self.state.pause.reason == "architecture_approval":
            if self.state.architecture_approved():
                self.state.pause = PauseState()
                if self.state.phase == "ARCHITECTURE":
                    self.state.phase = self.adapter.next_phase("ARCHITECTURE")
        elif self.state.pause.reason.endswith("_failed"):
            self.state.pause = PauseState()
        self.save()

    def spec_step(self, feedback: str = "") -> dict[str, Any]:
        """Run the requirements step and keep the human gate closed."""
        req = self.adapter.prepare(self.state.goal, feedback)
        self.adapter.sync_to_project()
        self.save()
        questions = self.state.requirements.open_questions
        if questions and not self.state.requirements.answers:
            self._pause("spec_questions", "; ".join(questions[:5]))
        elif not self._has_approval("spec"):
            self._pause("spec_approval", "contrato de requisitos pronto")
        else:
            self.state.phase = self.adapter.next_phase("SPEC")
            self.save()
        return req

    def prepare(self, feedback: str = "") -> dict[str, Any]:
        return self.spec_step(feedback)

    def step(self) -> str:
        """Execute one phase. Returns 'done' | 'paused' | 'ok' | 'failed'."""
        phase = self.state.phase
        if phase == "DONE":
            return "done"
        if self.state.pause.reason:
            return "paused"
        if (phase == "SPEC" and self.state.requirements.objective
                and self._has_approval("spec")):
            self.state.phase = self.adapter.next_phase("SPEC")
            self.save()
            return "ok"
        if (phase == "ARCHITECTURE" and _has_architecture(self.state)
                and self.state.architecture_approved()):
            self.state.phase = self.adapter.next_phase("ARCHITECTURE")
            self.save()
            return "ok"
        if phase == "SPEC":
            self.spec_step()
            return "paused" if self.state.pause.reason else "ok"

        if phase == "ARCHITECTURE":
            ok = self.adapter.step("ARCHITECTURE")
            self.adapter.sync_to_project()
            if not ok or not _has_architecture(self.state):
                self._pause("architecture_failed")
                return "failed"
            if not self.state.architecture_approved():
                self._pause("architecture_approval", self.state.architecture_hash())
                return "paused"
            self.state.phase = self.adapter.next_phase("ARCHITECTURE")
            self.save()
            return "ok"

        if phase == "VERIFY":
            # P1: build/integrate still run the existing verification battery.
            self.state.phase = self.adapter.next_phase("VERIFY")
            self.save()
            return "ok"

        # Re-close the architecture gate if the architecture changed since approval.
        if phase in ("SCAFFOLD", "BUILD", "INTEGRATE", "VISUAL", "SIGNOFF"):
            if _has_architecture(self.state) and not self.state.architecture_approved():
                self._pause("architecture_approval", self.state.architecture_hash())
                return "paused"

        ok = self.adapter.step(phase)
        self.adapter.sync_to_project()
        if not ok:
            self._pause(f"{phase.lower()}_failed")
            return "failed"
        self.state.phase = self.adapter.next_phase(phase)
        self.save()
        return "ok"

    def run(self, max_steps: int = 64) -> str:
        for _ in range(max_steps):
            result = self.step()
            if result in ("done", "paused", "failed"):
                return result
        return "paused"

    # -- reporting ---------------------------------------------------------- #

    def status(self) -> dict[str, Any]:
        return {
            "run_id": self.state.run_id,
            "engine_kind": self.state.engine_kind,
            "engine_version": self.state.engine_version,
            "schema_version": self.state.schema_version,
            "phase": self.state.phase,
            "pause": self.state.pause.reason,
            "tasks": len(self.state.tasks),
        }


class StateView:
    """Read-only view shaped like the legacy FactoryState for the TUI."""

    def __init__(self, state: ProjectState):
        self._state = state

    @property
    def tasks(self):
        return self._state.tasks

    @property
    def goal(self):
        return self._state.goal

    @property
    def requirements(self):
        return self._state.requirements.to_dict() if self._state.requirements.objective else {}

    @property
    def step(self):
        return self._state.phase

    @property
    def integration(self):
        return self._state.integration

    @property
    def visual(self):
        return self._state.visual

    @property
    def signoff(self):
        return self._state.signoff

    @property
    def answers(self):
        return self._state.requirements.answers


class EngineFacade:
    """Legacy-shaped facade used by the Textual dashboard."""

    def __init__(self, engine: WorkflowEngine, store: StateManager):
        self.engine = engine
        self.store = store
        root = engine.state.project_root
        self.root = Path(root) if root else Path(".")
        if engine.persistence == "factory" and root:
            self.state_path = store.legacy_factory_path(root)
        else:
            self.state_path = (store.project_state_path(root) if root
                               else store.staging_path(engine.state.run_id))

    @property
    def state(self) -> StateView:
        return StateView(self.engine.state)

    @property
    def pause_reason(self) -> str:
        return self.engine.state.pause.reason

    def prepare(self, goal: str, feedback: str = "") -> dict[str, Any]:
        if goal:
            self.engine.state.goal = goal
        return self.engine.prepare(feedback)

    def build(self) -> bool:
        reason = self.engine.state.pause.reason
        if reason in ("spec_questions", "spec_approval"):
            self.engine.approve_spec()
        if reason == "architecture_approval":
            self.engine.approve_architecture()
        elif reason.endswith("_failed"):
            self.engine.resume()
        return self.engine.run() == "done"

    def answer(self, question: str, answer: str) -> None:
        self.engine.adapter.answer(question, answer)
        self.engine.adapter.sync_to_project()
        self.engine.save()

    def approve_architecture(self, actor: str = "human") -> None:
        self.engine.approve_architecture(actor=actor)

    def status(self) -> dict[str, Any]:
        return self.engine.status()


class WorkflowAdapter(Protocol):
    order: tuple[str, ...]

    def prepare(self, goal: str, feedback: str = "") -> dict[str, Any]: ...
    def step(self, phase: str) -> bool: ...
    def sync_to_project(self) -> None: ...
    def next_phase(self, phase: str) -> str: ...
    def on_last_ok(self, task_id: str, ref: ModelRef) -> None: ...


PHASE_ORDER_UNIFIED = ("SPEC", "ARCHITECTURE", "SCAFFOLD", "BUILD", "VERIFY",
                       "INTEGRATE", "VISUAL", "SIGNOFF", "DONE")
PHASE_ORDER_LEGACY = ("SPEC", "SCAFFOLD", "ARCHITECTURE", "BUILD", "VERIFY",
                      "INTEGRATE", "VISUAL", "SIGNOFF", "DONE")


class FactoryAdapter:
    """Adapts the existing Factory steps to a ProjectState."""

    def __init__(self, settings: Any, pool: Any, emit: Any, project: ProjectState,
                 *, legacy_order: bool = False):
        self.settings = settings
        self.pool = pool
        self.emit = emit
        self.project = project
        self.order = PHASE_ORDER_LEGACY if legacy_order else PHASE_ORDER_UNIFIED
        self._last_ok: dict[str, ModelRef] = {}

        from factory import Factory, FactoryState, TaskState  # lazy: heavy deps

        _FS = FactoryState

        class EphemeralFactory(Factory):
            def save(self_inner) -> None:  # engine owns persistence
                return None

            def _load(self_inner) -> Any:  # never read .factory on unified runs
                return _FS(project=str(getattr(self_inner, "root", "")))

        self._FactoryState = FactoryState
        self._TaskState = TaskState
        self.factory = EphemeralFactory(settings, pool, emit, project.project_root,
                                        telemetry=getattr(pool, "telemetry", None))
        self.factory.state = self._to_factory_state(project)
        self.pool.attempt_sink = self._on_attempt_from_pool

    # -- conversion --------------------------------------------------------- #

    def _to_factory_state(self, project: ProjectState) -> Any:
        tasks = []
        for t in project.tasks:
            tasks.append(self._TaskState(
                id=t.id, title=t.title, description=t.description,
                target_file=t.target_file, acceptance=list(t.acceptance),
                depends_on=list(t.depends_on), complexity=t.complexity,
                role=t.role, status=t.status, errors=list(t.errors),
                attempt_log=[a.to_dict() for a in t.attempts],
                delivered_by=t.delivered_by.to_dict() if t.delivered_by else None,
                requirement_ids=list(t.requirement_ids),
                legacy_extra=dict(t.legacy_extra),
                verification=(project.verifications[t.id].to_dict()
                              if t.id in project.verifications else None),
            ))
        return self._FactoryState(
            project=str(project.project_root), goal=project.goal,
            step=project.phase, run_id=project.run_id,
            requirements=project.requirements.to_legacy_dict(),
            answers=list(project.requirements.answers),
            scaffold=dict(project.scaffold),
            architecture=project.architecture.to_dict(),
            tasks=tasks, integration=dict(project.integration),
            visual=dict(project.visual), signoff=dict(project.signoff),
            pause_reason=project.pause.reason,
            history=list(project.history),
            created=project.created, updated=project.updated,
        )

    def sync_to_project(self) -> None:
        fs = self.factory.state
        req = RequirementContract.from_dict(fs.requirements)
        req.answers = [dict(x) for x in (fs.answers or [])]
        self.project.requirements = req
        self.project.architecture = ArchitectureContract.from_dict(fs.architecture)
        self.project.scaffold = dict(fs.scaffold)
        existing = {t.id: t for t in self.project.tasks}
        tasks: list[Task] = []
        for ts in fs.tasks:
            prev = existing.get(ts.id)
            tasks.append(Task(
                id=ts.id, title=ts.title, description=ts.description,
                kind=prev.kind if prev else ("build" if ts.target_file else "prose"),
                team=prev.team if prev else "programming",
                role=ts.role, complexity=ts.complexity,
                target_file=ts.target_file, acceptance=list(ts.acceptance),
                depends_on=list(ts.depends_on), status=ts.status,
                errors=list(ts.errors),
                attempts=([TaskAttempt.from_dict(a) for a in ts.attempt_log]
                          if ts.attempt_log else (prev.attempts if prev else [])),
                delivered_by=(ModelRef.from_dict(ts.delivered_by) if ts.delivered_by
                              else (prev.delivered_by if prev else None)),
                requirement_ids=(list(ts.requirement_ids)
                                 if getattr(ts, "requirement_ids", None)
                                 else (list(prev.requirement_ids) if prev else [])),
                legacy_extra=(dict(getattr(ts, "legacy_extra", {}) or {})
                              or (dict(prev.legacy_extra) if prev else {})),
            ))
        self.project.tasks = tasks
        validate_dependencies(self.project.tasks)
        link_traceability(self.project.requirements, self.project.tasks)
        validate_traceability(self.project.requirements, self.project.tasks)
        for task in self.project.tasks:
            raw = getattr(next((ts for ts in fs.tasks if ts.id == task.id), None),
                          "verification", None)
            if raw:
                rec = VerificationRecord.from_dict(raw)
                rec.criterion_ids = list(task.requirement_ids)
                self.project.verifications[task.id] = rec
        self.project.integration = dict(fs.integration)
        self.project.visual = dict(fs.visual)
        self.project.signoff = dict(fs.signoff)
        for entry in fs.history:
            if entry not in self.project.history:
                self.project.history.append(entry)
        if fs.pause_reason and not self.project.pause.reason:
            self.project.pause = PauseState(reason=fs.pause_reason,
                                            phase=self.project.phase, since=utc_now())
        for t in self.project.tasks:
            if t.status in ("verified", "done") and t.id in self._last_ok:
                t.delivered_by = self._last_ok[t.id]

    # -- adapter API -------------------------------------------------------- #

    def prepare(self, goal: str, feedback: str = "") -> dict[str, Any]:
        req = self.factory.prepare(goal, feedback)
        self.sync_to_project()
        return req

    def answer(self, question: str, answer: str) -> None:
        self.factory.answer(question, answer)
        self.sync_to_project()

    def questions(self) -> list[str]:
        return list(self.factory.state.requirements.get("open_questions") or [])

    def next_phase(self, phase: str) -> str:
        try:
            idx = self.order.index(phase)
        except ValueError:
            return "DONE"
        return self.order[idx + 1] if idx + 1 < len(self.order) else "DONE"

    def step(self, phase: str) -> bool:
        self.factory.power.start()
        try:
            if phase == "ARCHITECTURE":
                ok = bool(self.factory.step_architect())
            elif phase == "SCAFFOLD":
                ok = bool(self.factory.step_scaffold())
            elif phase == "BUILD":
                ok = bool(self.factory.step_build())
            elif phase == "VERIFY":
                ok = True
            elif phase == "INTEGRATE":
                ok = bool(self.factory.step_integrate())
            elif phase == "VISUAL":
                ok = bool(self.factory.step_visual())
            elif phase == "SIGNOFF":
                ok = bool(self.factory.step_signoff())
            else:
                ok = True
        finally:
            self.factory.power.stop()
        self.sync_to_project()
        return ok

    def on_last_ok(self, task_id: str, ref: ModelRef) -> None:
        self._last_ok[task_id] = ref

    def _on_attempt_from_pool(self, event: AttemptEvent) -> None:
        # The engine is the canonical sink when present; this keeps the
        # ephemeral Factory view in sync for the dashboard.
        if event.task:
            for ts in self.factory.state.tasks:
                if ts.id == event.task:
                    ts.attempt_log.append(event.attempt.to_dict())
                    if event.attempt.outcome == "ok":
                        ts.delivered_by = event.attempt.model.to_dict()
                    break


# --------------------------------------------------------------------------- #
#  Execution selection
# --------------------------------------------------------------------------- #


def new_unified_state(goal: str, project_root: str, run_id: Optional[str] = None,
                      engine_version: str = ENGINE_VERSION) -> ProjectState:
    return ProjectState(
        run_id=run_id or new_id("run_"),
        project_root=project_root,
        goal=goal,
        engine_kind=ENGINE_UNIFIED,
        engine_version=engine_version,
        phase="SPEC",
    )


def open_execution(store: StateManager, project_root: Optional[str] = None) -> Optional[ExecDecision]:
    target: Optional[ResumeTarget] = store.resolve_resume(project_root)
    if target is None:
        return None
    return ExecDecision(engine_kind=target.engine_kind, path=target.path,
                        state=target.project_state, legacy_raw=target.legacy_raw)


def migrate_state(store: StateManager, project_root: str) -> ProjectState:
    """Format migration only: engine_kind stays whatever the source is (legacy)."""
    return store.migrate_factory_to_crew(project_root)


def _validate_dependencies(tasks: list[Task]) -> None:
    validate_dependencies(tasks)


def validate_unified_state(state: ProjectState) -> None:
    """Refuse an execution that the unified engine cannot resume safely."""
    if state.schema_version < SCHEMA_VERSION:
        raise EngineError("migra primeiro o schema com --migrate-schema")
    if state.engine_kind not in (ENGINE_LEGACY, ENGINE_UNIFIED):
        raise EngineError(f"engine_kind inválido: {state.engine_kind!r}")
    if state.phase not in PHASES:
        raise EngineError(f"fase desconhecida: {state.phase!r}")
    try:
        from state import validate_tasks, validate_traceability
        validate_tasks(state.tasks, project_root=state.project_root or None)
        validate_traceability(state.requirements, state.tasks)
    except StateValidationError as exc:
        raise EngineError(f"tarefas incompatíveis: {exc}") from exc
    _validate_dependencies(state.tasks)
    if not isinstance(state.requirements, RequirementContract):
        raise EngineError("contrato de requisitos inválido")
    if not isinstance(state.architecture, ArchitectureContract):
        raise EngineError("contrato de arquitectura inválido")


def _has_architecture(state: ProjectState) -> bool:
    a = state.architecture
    return bool(a.approach or a.modules or a.interfaces or a.test_plan)


def _normalize_for_unified(state: ProjectState) -> None:
    """Make a legacy state resumable by the corrected workflow order."""
    has_req = bool(state.requirements.objective)
    has_arch = bool(state.architecture.approach or state.architecture.interfaces
                    or state.architecture.modules)
    if not has_req:
        state.phase = "SPEC"
        return
    if state.phase == "SPEC":
        # Requirements exist but there is no spec checkpoint: keep the gate
        # closed instead of silently proceeding.
        state.pause = PauseState(reason="spec_approval", phase="SPEC", since=utc_now())
        return
    if not has_arch and state.phase not in ("SPEC", "ARCHITECTURE"):
        state.phase = "ARCHITECTURE"
        state.pause = PauseState()
        return
    if state.phase != "DONE" and state.architecture and not state.architecture_approved():
        # Architecture exists but was never approved under the new gate.
        state.pause = PauseState(reason="architecture_approval", phase=state.phase,
                                 since=utc_now(), detail=state.architecture_hash())
    if state.phase == "ARCHITECTURE" and state.architecture_approved():
        state.pause = PauseState()


def migrate_engine(store: StateManager, state: ProjectState) -> ProjectState:
    """Transactional, idempotent switch of engine_kind (format must be v2)."""
    validate_unified_state(state)
    if state.engine_kind == ENGINE_UNIFIED:
        return state

    source = state.to_dict()
    source_hash = sha256_json(source)
    snapshot_dir = store.history_dir(state.project_root) if state.project_root else None
    if snapshot_dir is not None:
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        snap = snapshot_dir / f"engine-source-{source_hash[:16]}.json"
        if not snap.exists():
            atomic_write_json(snap, source)

    candidate = ProjectState.from_dict(source)
    _normalize_for_unified(candidate)
    validate_unified_state(candidate)
    candidate.engine_kind = ENGINE_UNIFIED
    candidate.engine_version = ENGINE_VERSION
    candidate.migrations.append({
        "from": "legacy", "to": "unified", "at": utc_now(),
        "engine_changed": True, "source_sha256": source_hash,
    })
    store.save_project(candidate)
    return candidate
