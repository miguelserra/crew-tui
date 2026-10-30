"""Canonical state storage and safe promotion (stdlib only).

Rules enforced here:

* one canonical state file per execution; never a permanent dual-write;
* corrupt JSON raises and the original file is preserved untouched;
* staging -> project promotion writes a temp file in the *destination*
  filesystem, fsyncs, validates, then ``os.replace`` (atomic within a volume);
* a consumed staging file cannot become a second active execution.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from state import (
    ENGINE_KINDS,
    ENGINE_LEGACY,
    SCHEMA_VERSION,
    LEGACY_SCHEMA_VERSION,
    ProjectState,
    StateCorruptError,
    from_legacy_factory,
    sha256_json,
    utc_now,
)

# Re-export for callers that import from store.
__all__ = [
    "StateManager",
    "ResumeTarget",
    "atomic_write_json",
    "read_json",
]

CONSUMED_SUFFIX = ".consumed.json"


class ResumeTarget:
    """A resumed execution: which engine and which persisted format."""

    def __init__(self, engine_kind: str, path: Path, schema_version: int,
                 project_state: Optional[ProjectState] = None,
                 legacy_raw: Optional[dict[str, Any]] = None):
        self.engine_kind = engine_kind
        self.path = path
        self.schema_version = schema_version
        self.project_state = project_state
        self.legacy_raw = legacy_raw


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except Exception as e:
        raise StateCorruptError(str(path), str(e)) from e


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_DIRECTORY)
    except Exception:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_json(path: Path, data: Any,
                      validate: Optional[Callable[[Any], None]] = None) -> None:
    """Write JSON atomically in the destination filesystem."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                    dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        if validate is not None:
            validate(read_json(tmp))
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass
        raise


class StateManager:
    """Owns the canonical paths and the promotion/migration rules."""

    def __init__(self, workspace_dir: str | Path = "workspace",
                 log_dir: str | Path = "logs"):
        self.workspace_dir = Path(workspace_dir).expanduser()
        self.log_dir = Path(log_dir).expanduser()

    # -- paths -------------------------------------------------------------- #

    @staticmethod
    def project_state_path(project_root: str | Path) -> Path:
        return Path(project_root).expanduser().resolve() / ".crew" / "state.json"

    @staticmethod
    def history_dir(project_root: str | Path) -> Path:
        return Path(project_root).expanduser().resolve() / ".crew" / "history"

    def staging_path(self, run_id: str) -> Path:
        return self.workspace_dir / ".crew" / run_id / "state.json"

    @staticmethod
    def legacy_factory_path(project_root: str | Path) -> Path:
        return Path(project_root).expanduser().resolve() / ".factory" / "state.json"

    def legacy_session_path(self) -> Path:
        return self.log_dir / "session.json"

    # -- canonical load/save ------------------------------------------------ #

    def save_project(self, state: ProjectState) -> Path:
        path = (self.project_state_path(state.project_root) if state.project_root
                else self.staging_path(state.run_id))
        atomic_write_json(path, state.to_dict(),
                          validate=lambda data: ProjectState.from_dict(data))
        return path

    def load_project(self, path: Path) -> ProjectState:
        data = read_json(Path(path))
        return ProjectState.from_dict(data)

    def save_staging(self, state: ProjectState) -> Path:
        path = self.staging_path(state.run_id)
        atomic_write_json(path, state.to_dict(),
                          validate=lambda data: ProjectState.from_dict(data))
        return path

    # -- legacy ------------------------------------------------------------- #

    def load_legacy_factory(self, project_root: str | Path) -> dict[str, Any]:
        return read_json(self.legacy_factory_path(project_root))

    # -- promotion ---------------------------------------------------------- #

    def _consume_staging(self, staging: Path) -> None:
        try:
            target = staging.with_name(staging.stem + CONSUMED_SUFFIX)
            if target.exists():
                target = staging.with_name(
                    f"{staging.stem}.{utc_now().replace(':', '')}{CONSUMED_SUFFIX}")
            os.replace(staging, target)
        except Exception:
            # A marker is enough: the canonical file already exists.
            try:
                staging.with_suffix(".consumed").write_text(utc_now(), encoding="utf-8")
            except Exception:
                pass

    def archive_previous(self, project_root: str | Path) -> Optional[Path]:
        final = self.project_state_path(project_root)
        if not final.is_file():
            return None
        try:
            data = read_json(final)
            run_id = str(data.get("run_id") or "unknown")
        except Exception:
            run_id = f"corrupt-{utc_now().replace(':', '')}"
            data = {"_corrupt_snapshot": True, "path": str(final)}
        hist = self.history_dir(project_root)
        hist.mkdir(parents=True, exist_ok=True)
        dest = hist / f"{run_id}.json"
        if not dest.exists():
            atomic_write_json(dest, data)
        return dest

    def promote_staging(self, staging: Path, project_root: str | Path,
                        state: ProjectState) -> Path:
        """Promote a staged run into the project, safely across volumes."""
        staging = Path(staging)
        final = self.project_state_path(project_root)
        final.parent.mkdir(parents=True, exist_ok=True)

        if final.is_file():
            try:
                existing = read_json(final)
            except StateCorruptError:
                raise
            if str(existing.get("run_id") or "") == state.run_id:
                # Already promoted (e.g. crash after replace): just consume.
                self._consume_staging(staging)
                return final
            self.archive_previous(project_root)

        atomic_write_json(final, state.to_dict(),
                          validate=lambda data: ProjectState.from_dict(data))
        self._consume_staging(staging)
        return final

    # -- resume resolution -------------------------------------------------- #

    def resolve_resume(self, project_root: Optional[str | Path] = None,
                       ) -> Optional[ResumeTarget]:
        """Pick the persisted format/engine. Never migrates or switches silently."""
        if project_root:
            crew = self.project_state_path(project_root)
            if crew.is_file():
                state = self.load_project(crew)
                return ResumeTarget(state.engine_kind, crew, SCHEMA_VERSION, state)
            factory = self.legacy_factory_path(project_root)
            if factory.is_file():
                raw = read_json(factory)
                kind = str(raw.get("engine_kind") or ENGINE_LEGACY)
                if kind not in ENGINE_KINDS:
                    raise StateSchemaError(
                        f"engine_kind desconhecido em {factory}: {kind!r}")
                schema = int(raw.get("schema_version") or LEGACY_SCHEMA_VERSION)
                return ResumeTarget(kind, factory, schema, legacy_raw=raw)
        session = self.legacy_session_path()
        if session.is_file():
            raw = read_json(session)
            return ResumeTarget(ENGINE_LEGACY, session, LEGACY_SCHEMA_VERSION,
                                legacy_raw=raw)
        return None

    # -- format migration (does NOT change the engine) ---------------------- #

    def migrate_factory_to_crew(self, project_root: str | Path) -> ProjectState:
        """Convert ``.factory/state.json`` to ``.crew/state.json``.

        ``engine_kind`` stays ``legacy``: the execution continues on the
        LegacyFactoryAdapter, now with the canonical new persistence.
        """
        project_root = Path(project_root).expanduser().resolve()
        factory = self.legacy_factory_path(project_root)
        raw = read_json(factory)
        source_hash = sha256_json(raw)
        state = from_legacy_factory(raw, engine_kind=ENGINE_LEGACY,
                                    engine_version=str(raw.get("engine_version") or "legacy"))
        state.project_root = str(project_root)
        state.schema_version = SCHEMA_VERSION
        state.migrations.append({
            "from": "factory", "to": "project", "at": utc_now(),
            "source_sha256": source_hash, "engine_changed": False,
        })
        final = self.project_state_path(project_root)
        if final.is_file():
            existing = read_json(final)
            if str(existing.get("run_id") or "") != state.run_id:
                self.archive_previous(project_root)
        atomic_write_json(final, state.to_dict(),
                          validate=lambda data: ProjectState.from_dict(data))
        # Preserve an immutable snapshot of the source for rollback/audit.
        snap = self.history_dir(project_root) / f"factory-source-{source_hash[:16]}.json"
        if not snap.exists():
            atomic_write_json(snap, raw)
        return state
