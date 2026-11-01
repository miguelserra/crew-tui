"""Non-destructive adoption of an existing external software repository."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Optional

import engine
import engine_selection
from store import StateManager
from state import utc_now


class AdoptionError(Exception):
    pass


LANG_BY_SUFFIX = {
    ".py": "Python", ".js": "JavaScript", ".mjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".jsx": "JavaScript",
    ".java": "Java", ".go": "Go", ".rs": "Rust", ".cs": "C#",
    ".c": "C", ".h": "C", ".cpp": "C++", ".rb": "Ruby", ".php": "PHP",
    ".html": "HTML", ".css": "CSS", ".sh": "Shell",
}
CONFIG_NAMES = {
    "package.json", "package-lock.json", "pyproject.toml", "setup.py",
    "setup.cfg", "requirements.txt", "Pipfile", "Cargo.toml", "go.mod",
    "pom.xml", "build.gradle", "Makefile", "CMakeLists.txt", "Dockerfile",
    "docker-compose.yml", "tsconfig.json", "vite.config.js",
    "vite.config.ts", "next.config.js",
}
TEST_HINTS = ("test_", "_test.", ".spec.", ".test.")


def _git(root: Path, *args: str) -> tuple[bool, str]:
    try:
        proc = subprocess.run(["git", "-C", str(root), *args],
                              capture_output=True, text=True, timeout=20)
    except Exception as exc:
        return False, str(exc)
    return proc.returncode == 0, (proc.stdout or proc.stderr).strip()


def inspect_repository(root: str | Path,
                       max_files: int = 2000) -> dict[str, Any]:
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise AdoptionError(f"diretório inexistente: {root}")
    files: list[str] = []
    languages: dict[str, int] = {}
    configs: list[str] = []
    tests: list[str] = []
    hashes: dict[str, str] = {}
    truncated = False
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in (".git", ".crew", ".factory", "node_modules",
                        "__pycache__", ".venv", "venv")
               for part in rel.parts):
            continue
        if not path.is_file() or path.is_symlink():
            continue
        if len(files) >= max_files:
            truncated = True
            break
        name = path.name
        rel_s = rel.as_posix()
        files.append(rel_s)
        if name in CONFIG_NAMES:
            configs.append(rel_s)
        if any(h in name.lower() for h in TEST_HINTS) or "test" in rel.parts:
            tests.append(rel_s)
        lang = LANG_BY_SUFFIX.get(path.suffix.lower())
        if lang:
            languages[lang] = languages.get(lang, 0) + 1
        try:
            hashes[rel_s] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            pass
    git_ok, branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    _, head = _git(root, "rev-parse", "HEAD")
    status_ok, status = _git(root, "status", "--porcelain")
    dirty = [line for line in status.splitlines() if line.strip()]
    return {
        "root": str(root),
        "file_count": len(files),
        "files_truncated": truncated,
        "files": files,
        "file_sha256": hashes,
        "languages": dict(sorted(languages.items())),
        "configurations": sorted(configs),
        "tests": sorted(tests),
        "git_available": git_ok,
        "git_branch": branch if git_ok else "",
        "git_head": head if git_ok else "",
        "git_status_available": status_ok,
        "git_dirty": dirty,
    }


def adopt_repository(path: str | Path,
                     store: StateManager,
                     *,
                     goal: Optional[str] = None,
                     run_id: Optional[str] = None) -> dict[str, Any]:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise AdoptionError(f"diretório inexistente: {root}")
    if (root / ".crew" / "state.json").exists():
        raise AdoptionError("repositório já é gerido pelo Crew (.crew/state.json)")
    if (root / ".factory" / "state.json").exists():
        raise AdoptionError("repositório contém estado .factory; não é adoção externa")
    baseline = inspect_repository(root)
    project_goal = goal or f"Maintain the existing repository {root.name}"
    state = engine.new_unified_state(project_goal, str(root), run_id=run_id,
                                     engine_identity=engine_selection.NATIVE)
    state.phase = "SPEC"
    state.extensions[engine_selection.ORIGIN_KEY] = (
        engine_selection.EXTERNAL_EXISTING_REPOSITORY)
    state.extensions["adoption"] = {
        "adopted_at": utc_now(),
        "baseline": baseline,
        "policy": {
            "changes": "inspect_only_until_approved",
            "automatic_restructure": False,
            "automatic_dependency_upgrade": False,
            "automatic_scaffold_over_existing": False,
            "silent_delete": False,
            "preserve_git_history": True,
            "preserve_uncommitted_changes": True,
            "chestertons_fence": True,
        },
        "tests_check": {"status": "not_run",
                        "note": "adoption does not execute repository tests"},
    }
    store.save_project(state)
    after = inspect_repository(root)
    changed = [rel for rel, sha in baseline["file_sha256"].items()
               if after["file_sha256"].get(rel) not in (None, sha)]
    if changed:
        raise AdoptionError(
            "adoção alterou ficheiros existentes: " + ", ".join(changed[:5]))
    state.extensions["adoption"]["verified_unchanged"] = True
    store.save_project(state)
    return {"state": state, "baseline": baseline,
            "identity": engine_selection.NATIVE,
            "origin": engine_selection.EXTERNAL_EXISTING_REPOSITORY}
