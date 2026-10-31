"""Deterministic scaffold from the approved ArchitectureContract."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from state import normalize_target_file, StateValidationError


@dataclass
class ScaffoldManifest:
    root: str
    created: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _architecture_paths(architecture: Any,
                        tasks: Iterable[Any]) -> list[tuple[str, bool]]:
    paths: list[tuple[str, bool]] = []
    if hasattr(architecture, "modules"):
        modules = architecture.modules
        interfaces = architecture.interfaces
    else:
        data = architecture or {}
        modules = data.get("modules") or []
        interfaces = data.get("interfaces") or []
    for module in modules or []:
        if isinstance(module, dict):
            path = module.get("path") or module.get("file")
            if path:
                raw = str(path)
                is_dir = raw.endswith("/") or not Path(raw).suffix
                paths.append((raw, is_dir))
        elif isinstance(module, str):
            paths.append((module, module.endswith("/") or not Path(module).suffix))
    for interface in interfaces or []:
        if isinstance(interface, dict):
            path = interface.get("file")
            if path:
                paths.append((str(path), False))
    for task in tasks or []:
        path = getattr(task, "target_file", "") if not isinstance(task, dict) else task.get("target_file")
        if path:
            paths.append((str(path), False))
    return paths


def scaffold(workspace_root: str | Path, architecture: Any,
             tasks: Iterable[Any] = (), *, approved: bool = True) -> ScaffoldManifest:
    """Create directories/empty files without touching existing content or an LLM."""
    if not approved:
        raise StateValidationError("scaffold requires an approved architecture")
    root = Path(workspace_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest = ScaffoldManifest(root=str(root))
    raw_paths = _architecture_paths(architecture, tasks)
    normalized: list[tuple[str, bool]] = []
    seen: dict[str, tuple[str, bool]] = {}
    for raw, is_dir in raw_paths:
        try:
            rel = normalize_target_file(raw)
        except StateValidationError as exc:
            manifest.conflicts.append(str(exc))
            continue
        if not rel:
            continue
        key = rel.casefold()
        if key in seen and (seen[key][0] != rel or seen[key][1] != is_dir):
            manifest.conflicts.append(
                f"conflito case-insensitive: {seen[key][0]!r} vs {rel!r}")
            continue
        seen[key] = (rel, is_dir)
        normalized.append((rel, is_dir))
    if manifest.conflicts:
        raise StateValidationError("; ".join(manifest.conflicts))

    for rel, is_dir in normalized:
        dest = (root / rel).resolve()
        try:
            dest.relative_to(root)
        except ValueError:
            manifest.conflicts.append(f"path escapa da raiz: {rel!r}")
            continue
        if dest.is_symlink():
            manifest.ignored.append(rel)
            continue
        if is_dir:
            if dest.exists():
                manifest.reused.append(rel)
            else:
                dest.mkdir(parents=True, exist_ok=True)
                manifest.created.append(rel)
            continue
        if dest.exists():
            manifest.reused.append(rel)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            dest.write_text("", encoding="utf-8")
            manifest.created.append(rel)
        else:
            manifest.reused.append(rel)
    if manifest.conflicts:
        raise StateValidationError("; ".join(manifest.conflicts))
    import time
    manifest.at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return manifest


def snapshot_files(root: str | Path) -> dict[str, str]:
    """Relative path -> sha256 for a deterministic filesystem comparison."""
    import hashlib
    base = Path(root).expanduser().resolve()
    out: dict[str, str] = {}
    if not base.is_dir():
        return out
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = str(path.relative_to(base))
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out
