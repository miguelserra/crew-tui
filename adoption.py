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
                       max_files: int = 2000,
                       max_total_bytes: int = 64 * 1024 * 1024,
                       max_file_bytes: int = 8 * 1024 * 1024) -> dict[str, Any]:
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise AdoptionError(f"diretório inexistente: {root}")
    excluded_dirs = {".git", ".crew", ".factory", "node_modules",
                     "__pycache__", ".venv", "venv"}
    files: list[str] = []
    hashes: dict[str, str] = {}
    languages: dict[str, int] = {}
    configs: list[str] = []
    tests: list[str] = []
    symlinks: list[dict[str, str]] = []
    special_files: list[str] = []
    unreadable: list[str] = []
    hash_skipped: list[dict[str, str]] = []
    lfs_pointers: list[str] = []
    regular_total = 0
    total_bytes = 0
    files_truncated = False
    submodules: list[str] = []
    gitmodules = root / ".gitmodules"
    if gitmodules.is_file():
        try:
            for line in gitmodules.read_text(encoding="utf-8",
                                             errors="ignore").splitlines():
                if line.strip().startswith("path") and "=" in line:
                    submodules.append(line.split("=", 1)[1].strip())
        except OSError:
            pass

    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        kept_dirs = []
        for name in sorted(dirnames):
            path = current_path / name
            rel = path.relative_to(root).as_posix()
            if name in excluded_dirs:
                continue
            if path.is_symlink():
                try:
                    target = os.readlink(path)
                    resolved = path.resolve(strict=False)
                    inside = True
                    try:
                        resolved.relative_to(root)
                    except ValueError:
                        inside = False
                    symlinks.append({"path": rel, "target": target,
                                     "target_inside_root": inside})
                except OSError:
                    symlinks.append({"path": rel, "target": "<unreadable>",
                                     "target_inside_root": False})
                continue
            kept_dirs.append(name)
        dirnames[:] = kept_dirs
        for name in sorted(filenames):
            path = current_path / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                try:
                    target = os.readlink(path)
                    resolved = path.resolve(strict=False)
                    inside = True
                    try:
                        resolved.relative_to(root)
                    except ValueError:
                        inside = False
                    symlinks.append({"path": rel, "target": target,
                                     "target_inside_root": inside})
                except OSError:
                    symlinks.append({"path": rel, "target": "<unreadable>",
                                     "target_inside_root": False})
                continue
            try:
                if not path.is_file():
                    special_files.append(rel)
                    continue
            except OSError:
                unreadable.append(rel)
                continue
            regular_total += 1
            if len(files) < max_files:
                files.append(rel)
            else:
                files_truncated = True
            if len(hashes) >= max_files:
                hash_skipped.append({"path": rel, "reason": "file_limit"})
                files_truncated = True
                continue
            try:
                size = path.stat().st_size
            except OSError:
                unreadable.append(rel)
                continue
            if size > max_file_bytes:
                hash_skipped.append({"path": rel, "reason": "file_size_limit"})
                continue
            if total_bytes + size > max_total_bytes:
                hash_skipped.append({"path": rel, "reason": "total_bytes_limit"})
                continue
            try:
                data = path.read_bytes()
            except OSError:
                unreadable.append(rel)
                continue
            total_bytes += len(data)
            hashes[rel] = hashlib.sha256(data).hexdigest()
            if data.startswith(b"version https://git-lfs.github.com/spec/v1"):
                lfs_pointers.append(rel)
            if name in CONFIG_NAMES:
                configs.append(rel)
            if any(h in name.lower() for h in TEST_HINTS) or "test" in rel.split("/"):
                tests.append(rel)
            lang = LANG_BY_SUFFIX.get(path.suffix.lower())
            if lang:
                languages[lang] = languages.get(lang, 0) + 1
    git_ok, branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    _, head = _git(root, "rev-parse", "HEAD")
    status_ok, status = _git(root, "status", "--porcelain")
    dirty = [line for line in status.splitlines() if line.strip()]
    hashed_count = len(hashes)
    hash_coverage = (hashed_count / regular_total) if regular_total else 1.0
    hash_complete = (hashed_count == regular_total
                     and not unreadable and not hash_skipped)
    return {
        "root": str(root),
        "inventory_total": regular_total + len(symlinks) + len(special_files),
        "files_total_regular": regular_total,
        "inventory_complete": not files_truncated and not unreadable,
        "files_truncated": files_truncated,
        "files": files,
        "hashed_count": hashed_count,
        "hash_coverage": hash_coverage,
        "hash_complete": hash_complete,
        "hash_limits": {
            "max_files": max_files,
            "max_total_bytes": max_total_bytes,
            "max_file_bytes": max_file_bytes,
            "hashed_bytes": total_bytes,
        },
        "file_sha256": hashes,
        "hash_skipped": hash_skipped,
        "unreadable": unreadable,
        "symlinks": symlinks,
        "special_files": special_files,
        "excluded_directories": sorted(excluded_dirs),
        "lfs_pointers": lfs_pointers,
        "submodules": submodules,
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
                     run_id: Optional[str] = None,
                     max_files: int = 2000,
                     max_total_bytes: int = 64 * 1024 * 1024,
                     max_file_bytes: int = 8 * 1024 * 1024) -> dict[str, Any]:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise AdoptionError(f"diretório inexistente: {root}")
    if (root / ".crew" / "state.json").exists():
        raise AdoptionError("repositório já é gerido pelo Crew (.crew/state.json)")
    if (root / ".factory" / "state.json").exists():
        raise AdoptionError("repositório contém estado .factory; não é adoção externa")
    baseline = inspect_repository(root, max_files=max_files,
                                  max_total_bytes=max_total_bytes,
                                  max_file_bytes=max_file_bytes)
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
    after = inspect_repository(root, max_files=max_files,
                               max_total_bytes=max_total_bytes,
                               max_file_bytes=max_file_bytes)
    changed = []
    unverified = []
    for rel, sha in baseline["file_sha256"].items():
        new_sha = after["file_sha256"].get(rel)
        if new_sha is None:
            unverified.append(rel)
        elif new_sha != sha:
            changed.append(rel)
    if changed:
        raise AdoptionError(
            "adoção alterou ficheiros existentes: " + ", ".join(changed[:5]))
    complete = (baseline.get("hash_complete", False)
                and after.get("hash_complete", False)
                and not unverified)
    state.extensions["adoption"]["integrity"] = {
        "verified_unchanged": complete,
        "baseline_hash_complete": bool(baseline.get("hash_complete")),
        "after_hash_complete": bool(after.get("hash_complete")),
        "hashed_count": baseline.get("hashed_count", 0),
        "hash_coverage": baseline.get("hash_coverage", 0.0),
        "unverified_hashes": unverified[:50],
        "unverified_count": len(unverified),
        "hash_skipped_count": len(baseline.get("hash_skipped") or []),
        "unreadable_count": len(baseline.get("unreadable") or []),
        "changed_count": len(changed),
    }
    state.extensions["adoption"]["verified_unchanged"] = complete
    store.save_project(state)
    return {"state": state, "baseline": baseline,
            "identity": engine_selection.NATIVE,
            "origin": engine_selection.EXTERNAL_EXISTING_REPOSITORY}
