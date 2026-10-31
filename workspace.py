"""Workspace integrity, ignore rules and publish planning (stdlib only)."""

from __future__ import annotations

import fnmatch
import hashlib
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

SECURITY_DIRS = {".ssh", ".aws"}
SECURITY_NAMES = {"id_rsa", "id_rsa.pub", "id_ed25519", "id_ed25519.pub"}
SECURITY_SUFFIXES = (".pem", ".key")
ENV_TEMPLATES = {".env.example", ".env.sample", ".env.template"}

TECHNICAL_DIRS = {
    ".git", ".crew", ".factory", ".venv", "venv", "env", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", "dist",
    ".next", "coverage", ".idea", ".vscode", ".vs", "bin", "obj",
    "TestResults", "logs", "workspace",
}
TECHNICAL_FILES = {".DS_Store", "Thumbs.db"}
TECHNICAL_SUFFIXES = (".pyc", ".pyo", ".suo", ".user", ".p6tmp")


@dataclass
class IgnoreRules:
    crew: list[str] = field(default_factory=list)
    git: list[str] = field(default_factory=list)

    @classmethod
    def from_project(cls, project_root: str | Path) -> "IgnoreRules":
        root = Path(project_root)
        return cls(crew=_read_patterns(root / ".crewignore"),
                   git=_read_patterns(root / ".gitignore"))

    def _project_excluded(self, rel: str) -> Optional[str]:
        for source, patterns in ((".crewignore", self.crew), (".gitignore", self.git)):
            for pattern in patterns:
                if pattern.startswith("!"):
                    continue
                if _match(pattern, rel):
                    return source
        return None

    def _included(self, rel: str) -> bool:
        for patterns in (self.crew, self.git):
            for pattern in patterns:
                if pattern.startswith("!") and _match(pattern[1:], rel):
                    return True
        return False

    def reason(self, rel: str) -> Optional[str]:
        rel = rel.replace("\\", "/")
        if rel.startswith("./"):
            rel = rel[2:]
        parts = rel.split("/")
        name = parts[-1] if parts else rel
        # Mandatory security exclusions cannot be re-included.
        if name not in ENV_TEMPLATES:
            if name == ".env" or name.startswith(".env."):
                return "security"
            if name.lower().endswith(SECURITY_SUFFIXES):
                return "security"
            if name.startswith(("id_rsa", "id_ed25519")):
                return "security"
            if name in SECURITY_NAMES:
                return "security"
            if any(part in SECURITY_DIRS for part in parts):
                return "security"
        if self._included(rel):
            return None
        if any(part in TECHNICAL_DIRS for part in parts):
            return "technical"
        if name in TECHNICAL_FILES or name.endswith(TECHNICAL_SUFFIXES):
            return "technical"
        project = self._project_excluded(rel)
        if project:
            return project
        return None

    def is_excluded(self, rel: str) -> bool:
        return self.reason(rel) is not None


def _read_patterns(path: Path) -> list[str]:
    if not path.is_file():
        return []
    out: list[str] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line)
    except Exception:
        return []
    return out


def _match(pattern: str, rel: str) -> bool:
    pattern = pattern.strip().replace("\\", "/").lstrip("/")
    rel = rel.replace("\\", "/")
    if rel.startswith("./"):
        rel = rel[2:]
    if not pattern:
        return False
    if pattern.endswith("/"):
        dir_pat = pattern.rstrip("/")
        return any(fnmatch.fnmatchcase(part, dir_pat) for part in rel.split("/")[:-1]) \
            or rel.startswith(dir_pat + "/")
    if "/" in pattern:
        return fnmatch.fnmatchcase(rel, pattern) or rel.startswith(pattern)
    name = rel.rsplit("/", 1)[-1]
    return (fnmatch.fnmatchcase(name, pattern)
            or any(fnmatch.fnmatchcase(part, pattern) for part in rel.split("/")))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def excluded(rel: str, rules: Optional[IgnoreRules] = None) -> bool:
    return (rules or IgnoreRules()).is_excluded(rel)


@dataclass
class WorkspaceFile:
    path: str
    original_sha256: str = ""
    workspace_sha256: str = ""
    classification: str = "preserved"  # preserved|modified|missing|created|excluded|approved_delete

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class WorkspaceManifest:
    files: list[WorkspaceFile] = field(default_factory=list)
    excluded: list[tuple[str, str]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": [f.to_dict() for f in self.files],
            "excluded": [{"path": p, "reason": r} for p, r in self.excluded],
            "missing": list(self.missing),
            "conflicts": list(self.conflicts),
            "ok": self.ok,
        }


def default_workspace(settings: Any, run_id: str) -> Path:
    return (Path(settings.workspace_dir).expanduser() / run_id / "project").resolve()


def prepare_workspace(project_root: str | Path, workspace_root: str | Path,
                      rules: Optional[IgnoreRules] = None,
                      report: Optional[list[tuple[str, str]]] = None) -> dict[str, str]:
    """Seed the workspace; return original hashes for included files."""
    project = Path(project_root).expanduser().resolve()
    workspace = Path(workspace_root).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    rules = rules or IgnoreRules.from_project(project)
    origin: dict[str, str] = {}
    if project.is_dir():
        for src in sorted(project.rglob("*")):
            if not src.is_file() or src.is_symlink():
                continue
            rel = str(src.relative_to(project))
            why = rules.reason(rel)
            if why:
                if report is not None:
                    report.append((rel, why))
                continue
            dest = workspace / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                shutil.copy2(src, dest)
            origin[rel] = sha256_file(src)
    return origin


def validate_workspace_integrity(project_root: str | Path,
                                 workspace_root: str | Path,
                                 origin_hashes: dict[str, str],
                                 rules: Optional[IgnoreRules] = None,
                                 approved_deletions: Iterable[Any] = ()) -> WorkspaceManifest:
    project = Path(project_root).expanduser().resolve()
    workspace = Path(workspace_root).expanduser().resolve()
    rules = rules or IgnoreRules.from_project(project)
    deletions = {getattr(d, "target_file", "") or (d.get("target_file") if isinstance(d, dict) else "")
                 for d in approved_deletions}
    expected = {getattr(d, "target_file", "") or (d.get("target_file") if isinstance(d, dict) else ""):
                getattr(d, "expected_sha256", "") or (d.get("expected_sha256") if isinstance(d, dict) else "")
                for d in approved_deletions}
    manifest = WorkspaceManifest()
    for rel, old_sha in sorted(origin_hashes.items()):
        if rules.is_excluded(rel):
            manifest.files.append(WorkspaceFile(rel, old_sha, "", "excluded"))
            continue
        dest = workspace / rel
        if not dest.is_file() or dest.is_symlink():
            if rel in deletions and expected.get(rel, old_sha) == old_sha:
                manifest.files.append(WorkspaceFile(rel, old_sha, "", "approved_delete"))
            else:
                manifest.files.append(WorkspaceFile(rel, old_sha, "", "missing"))
                manifest.missing.append(rel)
                manifest.ok = False
            continue
        new_sha = sha256_file(dest)
        classification = "preserved" if new_sha == old_sha else "modified"
        manifest.files.append(WorkspaceFile(rel, old_sha, new_sha, classification))
    for src in sorted(workspace.rglob("*")):
        if not src.is_file() or src.is_symlink():
            continue
        rel = str(src.relative_to(workspace))
        if rel in origin_hashes or rules.is_excluded(rel):
            continue
        manifest.files.append(WorkspaceFile(rel, "", sha256_file(src), "created"))
    return manifest


@dataclass
class PublishEntry:
    rel: str
    action: str          # create|replace|delete|skip
    old_sha: str = ""
    new_sha: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class PublishPlan:
    project_root: str
    entries: list[PublishEntry] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    manifest: Optional[WorkspaceManifest] = None

    def to_dict(self) -> dict[str, Any]:
        return {"project_root": self.project_root,
                "entries": [e.to_dict() for e in self.entries],
                "conflicts": list(self.conflicts),
                "manifest": self.manifest.to_dict() if self.manifest else None}


def plan_publish(project_root: str | Path, workspace_root: str | Path,
                 origin_hashes: dict[str, str], *,
                 rules: Optional[IgnoreRules] = None,
                 approved_deletions: Iterable[Any] = (),
                 manifest: Optional[WorkspaceManifest] = None) -> PublishPlan:
    project = Path(project_root).expanduser().resolve()
    workspace = Path(workspace_root).expanduser().resolve()
    rules = rules or IgnoreRules.from_project(project)
    if manifest is None:
        manifest = validate_workspace_integrity(
            project, workspace, origin_hashes, rules, approved_deletions)
    plan = PublishPlan(project_root=str(project), manifest=manifest)
    if not manifest.ok:
        plan.conflicts.extend(f"workspace sem ficheiro original: {rel}"
                              for rel in manifest.missing)
        return plan

    deletion_map = {getattr(d, "target_file", "") or (d.get("target_file") if isinstance(d, dict) else ""):
                    (getattr(d, "expected_sha256", "") or (d.get("expected_sha256") if isinstance(d, dict) else ""),
                     getattr(d, "requirement_id", "") or (d.get("requirement_id") if isinstance(d, dict) else ""))
                    for d in approved_deletions}

    for item in manifest.files:
        rel = item.path
        if rel in deletion_map:
            continue
        if item.classification in ("excluded", "missing", "approved_delete"):
            continue
        dest = project / rel
        current = sha256_file(dest) if dest.is_file() and not dest.is_symlink() else ""
        if item.classification == "created":
            if current:
                plan.conflicts.append(f"ficheiro existente não preparado: {rel}")
                continue
            plan.entries.append(PublishEntry(rel, "create", "", item.workspace_sha256))
            continue
        old = item.original_sha256
        new = item.workspace_sha256
        if new == old:
            plan.entries.append(PublishEntry(rel, "skip", current, new))
            continue
        if not current or current != old:
            plan.conflicts.append(f"alteração/eliminação humana detectada: {rel}")
            continue
        plan.entries.append(PublishEntry(rel, "replace", current, new))

    for rel, (expected, _req) in deletion_map.items():
        if not rel:
            continue
        if rules.is_excluded(rel):
            plan.conflicts.append(f"eliminação de ficheiro protegido: {rel}")
            continue
        dest = project / rel
        current = sha256_file(dest) if dest.is_file() and not dest.is_symlink() else ""
        if not current:
            plan.entries.append(PublishEntry(rel, "skip", "", ""))
            continue
        if expected and current != expected:
            plan.conflicts.append(f"eliminação aprovada mas ficheiro mudou: {rel}")
            continue
        plan.entries.append(PublishEntry(rel, "delete", current, ""))
    return plan
