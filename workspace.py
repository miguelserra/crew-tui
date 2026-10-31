"""Isolated workspace preparation and publish planning (stdlib only)."""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

EXCLUDE_DIRS = {
    ".crew", ".factory", ".git", ".venv", "venv", "node_modules",
    "__pycache__", ".pytest_cache", ".mypy_cache", "logs", "workspace",
}
EXCLUDE_FILES = {".env", ".DS_Store", "Thumbs.db"}
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".p6tmp")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def excluded(rel: str) -> bool:
    parts = rel.replace("\\", "/").split("/")
    if any(p in EXCLUDE_DIRS for p in parts[:-1]):
        return True
    if parts and parts[-1] in EXCLUDE_DIRS:
        return True
    if parts and parts[-1] in EXCLUDE_FILES:
        return True
    return rel.endswith(EXCLUDE_SUFFIXES)


def default_workspace(settings: Any, run_id: str) -> Path:
    return (Path(settings.workspace_dir).expanduser() / run_id / "project").resolve()


def prepare_workspace(project_root: str | Path,
                      workspace_root: str | Path) -> dict[str, str]:
    """Seed the workspace from the user's project; return original hashes."""
    project = Path(project_root).expanduser().resolve()
    workspace = Path(workspace_root).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    origin: dict[str, str] = {}
    if project.is_dir():
        for src in sorted(project.rglob("*")):
            if not src.is_file() or src.is_symlink():
                continue
            rel = str(src.relative_to(project))
            if excluded(rel):
                continue
            dest = workspace / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                shutil.copy2(src, dest)
            origin[rel] = sha256_file(src)
    return origin


@dataclass
class PublishEntry:
    rel: str
    action: str          # create|replace|skip
    old_sha: str = ""
    new_sha: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class PublishPlan:
    project_root: str
    entries: list[PublishEntry] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"project_root": self.project_root,
                "entries": [e.to_dict() for e in self.entries],
                "conflicts": list(self.conflicts)}


def plan_publish(project_root: str | Path, workspace_root: str | Path,
                 origin_hashes: dict[str, str]) -> PublishPlan:
    project = Path(project_root).expanduser().resolve()
    workspace = Path(workspace_root).expanduser().resolve()
    plan = PublishPlan(project_root=str(project))
    if not workspace.is_dir():
        return plan
    for src in sorted(workspace.rglob("*")):
        if not src.is_file() or src.is_symlink():
            continue
        rel = str(src.relative_to(workspace))
        if excluded(rel):
            continue
        new_sha = sha256_file(src)
        dest = project / rel
        current_sha = sha256_file(dest) if dest.is_file() else ""
        old_sha = origin_hashes.get(rel, "")
        if old_sha and new_sha == old_sha:
            # Crew did not change this file; a human edit is preserved.
            plan.entries.append(PublishEntry(rel, "skip", old_sha=current_sha,
                                             new_sha=new_sha))
            continue
        if old_sha and current_sha and current_sha != old_sha:
            plan.conflicts.append(f"alteração humana detectada: {rel}")
            continue
        if old_sha and not current_sha:
            plan.conflicts.append(f"ficheiro removido pelo utilizador: {rel}")
            continue
        if not old_sha and current_sha:
            plan.conflicts.append(f"ficheiro existente não preparado: {rel}")
            continue
        if not current_sha:
            action = "create"
        elif current_sha == new_sha:
            action = "skip"
        else:
            action = "replace"
        plan.entries.append(PublishEntry(rel, action, old_sha=current_sha,
                                         new_sha=new_sha))
    return plan
