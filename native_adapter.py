"""Unified runtime adapter composed only of native P7 services.

It intentionally has no dependency on ``Factory``/``FactoryState``. Publishing
and workspace safety are reused unchanged so P6/P6.1 contracts remain intact.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from architecture import ArchitectureService
from executor import ExecutorService
from integration import IntegrationService
from publish import PublishConflict, PublishManager
from requirements import RequirementsService
from scaffold import scaffold
from signoff import SignoffService
from state import ProjectState
from visual import VisualService
from workspace import (IgnoreRules, default_workspace, prepare_workspace,
                       validate_workspace_integrity)

PHASE_ORDER = ("SPEC", "ARCHITECTURE", "SCAFFOLD", "BUILD", "VERIFY",
               "INTEGRATE", "VISUAL", "SIGNOFF", "DONE")


class NativeAdapter:
    """Drive native services over one canonical ``ProjectState``."""

    def __init__(self, settings: Any, pool: Any, emit: Any, project: ProjectState,
                 *, verifier: Any = None, isolate_workspace: bool = True):
        self.settings = settings
        self.pool = pool
        self.emit = emit
        self.project = project
        self.isolated = bool(isolate_workspace)
        self.rules = None
        self.recovery_error = ""
        if self.isolated and project.project_root:
            self.rules = IgnoreRules.from_project(project.project_root)
            ws = str(project.extensions.get("workspace_root") or
                     default_workspace(settings, project.run_id))
            project.extensions["workspace_root"] = ws
            origin = project.extensions.get("workspace_origin")
            if not origin:
                excluded_report: list[tuple[str, str]] = []
                origin = prepare_workspace(project.project_root, ws,
                                           rules=self.rules,
                                           report=excluded_report)
                project.extensions["workspace_origin"] = origin
                project.extensions["workspace_excluded"] = [
                    {"path": p, "reason": r} for p, r in excluded_report]
            self.workspace_root = ws
            try:
                PublishManager(project.project_root, ws,
                               Path(project.project_root) / ".crew",
                               origin, rules=self.rules,
                               deletions=project.architecture.deletions).recover()
            except Exception as exc:
                self.recovery_error = str(exc)
        else:
            self.workspace_root = project.project_root
        self.verifier = verifier
        self.artifact_dir = (Path(self.workspace_root) / ".crew" / "runs" /
                             (project.run_id or "latest"))
        self.requirements = RequirementsService(settings, pool, emit)
        self.architecture = ArchitectureService(settings, pool, emit)
        self.executor = ExecutorService(settings, pool, self.verifier, emit,
                                        project.run_id or "")
        self.integration = IntegrationService(settings, pool, emit,
                                              project.run_id or "",
                                              artifact_dir=self.artifact_dir)
        self.visual = VisualService(settings, pool, emit, project.run_id or "",
                                    artifact_dir=self.artifact_dir)
        self.signoff = SignoffService(settings, pool, emit, project.run_id or "")

    def prepare(self, goal: str, feedback: str = "") -> dict[str, Any]:
        return self.requirements.prepare(self.project, feedback)

    def answer(self, question: str, answer: str) -> None:
        self.requirements.answer(self.project, question, answer)

    def questions(self) -> list[str]:
        return list(self.project.requirements.open_questions)

    def next_phase(self, phase: str) -> str:
        try:
            idx = PHASE_ORDER.index(phase)
        except ValueError:
            return "DONE"
        return PHASE_ORDER[idx + 1] if idx + 1 < len(PHASE_ORDER) else "DONE"

    def step(self, phase: str) -> bool:
        if phase == "ARCHITECTURE":
            return bool(self.architecture.design(self.project, self.workspace_root))
        if phase == "SCAFFOLD":
            manifest = scaffold(self.workspace_root, self.project.architecture,
                                self.project.tasks,
                                approved=self.project.architecture_approved())
            self.project.scaffold = manifest.to_dict()
            return True
        if phase == "BUILD":
            return bool(self.executor.build(self.project, self.workspace_root))
        if phase == "VERIFY":
            return self._verify_checkpoint()
        if phase == "INTEGRATE":
            return bool(self.integration.integrate(self.project, self.workspace_root))
        if phase == "VISUAL":
            return bool(self.visual.step_visual(self.project, self.workspace_root))
        if phase == "SIGNOFF":
            return bool(self.signoff.signoff(self.project, self.workspace_root,
                                             final_root=self.project.project_root))
        return True

    def _verify_checkpoint(self) -> bool:
        if self.project.profile == "review":
            return True
        build_tasks = [t for t in self.project.tasks if t.kind == "build"]
        missing = [t.id for t in build_tasks
                   if t.id not in self.project.verifications]
        bad = [t.id for t in build_tasks
               if self.project.verifications.get(t.id)
               and self.project.verifications[t.id].verdict == "FAIL"]
        unverified = [t.id for t in build_tasks
                      if self.project.verifications.get(t.id)
                      and self.project.verifications[t.id].verdict == "UNVERIFIED"]
        if missing or bad or (unverified and not self.settings.allow_unverified):
            self.emit("VERIFY", f"checkpoint bloqueado: missing={missing} "
                                f"fail={bad} unverified={unverified}")
            return False
        self.emit("VERIFY", f"checkpoint OK ({len(build_tasks)} tarefas)")
        return True

    def publish(self) -> Optional[dict[str, Any]]:
        if not self.isolated or self.project.profile == "review":
            return None
        origin = dict(self.project.extensions.get("workspace_origin") or {})
        mgr = PublishManager(self.project.project_root, self.workspace_root,
                             Path(self.project.project_root) / ".crew", origin,
                             rules=self.rules,
                             deletions=self.project.architecture.deletions)
        returned = mgr.recover()
        if returned and returned.get("status") in ("RECOVERY_REQUIRED",
                                                   "REVALIDATION_REQUIRED"):
            raise PublishConflict("recuperação de publicação pendente")
        manifest = validate_workspace_integrity(
            self.project.project_root, self.workspace_root, origin,
            self.rules, self.project.architecture.deletions)
        self.project.extensions["workspace_manifest"] = manifest.to_dict()
        if not manifest.ok:
            raise PublishConflict("workspace incompleto: "
                                  + ", ".join(manifest.missing[:5]))
        mgr.manifest = manifest
        return mgr.publish()

    def on_last_ok(self, task_id: str, ref: Any) -> None:
        return None

    def sync_to_project(self) -> None:
        return None
