"""Bounded, ignore-aware project context reading (stdlib only)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from workspace import IgnoreRules


@dataclass
class ContextFile:
    path: str
    chars: int
    truncated: bool = False
    note: str = ""


@dataclass
class ProjectContext:
    tree: list[str] = field(default_factory=list)
    files: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    truncated: bool = False

    def text(self) -> str:
        return "\n\n".join(
            f"### FILE: {rel}\n```\n{body}\n```" for rel, body in self.files)


def is_binary(data: bytes) -> bool:
    if not data:
        return False
    if b"\x00" in data:
        return True
    sample = data[:4096]
    controls = sum(1 for b in sample if b < 9 or (13 < b < 32))
    return controls / max(1, len(sample)) > 0.30


def read_project_context(root: str | Path, *, rules: Optional[IgnoreRules] = None,
                         max_files: int = 40, max_file_chars: int = 8000,
                         max_total_chars: int = 24000,
                         max_tree: int = 200) -> ProjectContext:
    base = Path(root).expanduser().resolve()
    rules = rules or IgnoreRules.from_project(base)
    ctx = ProjectContext()
    if not base.is_dir():
        return ctx
    total = 0
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = str(path.relative_to(base))
        if rules.is_excluded(rel):
            ctx.skipped.append((rel, "excluded"))
            continue
        if len(ctx.tree) >= max_tree:
            ctx.truncated = True
            break
        ctx.tree.append(rel)
        if len(ctx.files) >= max_files or total >= max_total_chars:
            ctx.truncated = True
            continue
        try:
            with path.open("rb") as fh:
                raw = fh.read(max_file_chars * 4 + 1)
        except Exception as exc:
            ctx.skipped.append((rel, f"read: {exc}"))
            continue
        if is_binary(raw):
            ctx.skipped.append((rel, "binary"))
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            ctx.skipped.append((rel, "not-utf8"))
            continue
        truncated = len(text) > max_file_chars or len(raw) > max_file_chars * 4
        text = text[:max_file_chars]
        if total + len(text) > max_total_chars:
            text = text[:max(0, max_total_chars - total)]
            truncated = True
        if not text:
            ctx.skipped.append((rel, "empty/truncated"))
            continue
        ctx.files.append((rel, text))
        total += len(text)
        if truncated:
            ctx.truncated = True
    return ctx
