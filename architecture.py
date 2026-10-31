"""Native architecture decomposition over canonical ProjectState."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from router import RouteRequest
from state import ArchitectureContract, ProjectState, Task, link_traceability, validate_traceability, validate_tasks

ARCHITECT_SYSTEM = (
    "You are the chief engineer. You define the attack plan, the module boundaries, the "
    "public interfaces (name, inputs, outputs, errors) and split the work into atomic "
    "tasks. Every task has ONE target file and measurable acceptance criteria. You assign "
    "complexity by how many components the unit touches: 'high' for cross-cutting logic "
    "and UI, 'low' for pure helpers. Output STRICT JSON only."
)


def _json_or_raise(raw: str, what: str) -> dict[str, Any]:
    import re
    m = re.search(r"```(?:json)?\s*(.*?)```", raw or "", re.S | re.I)
    text = m.group(1) if m else (raw or "")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"{what}: JSON inválido")
    try:
        data = json.loads(text[start:end + 1])
    except Exception as exc:
        raise ValueError(f"{what}: JSON inválido") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{what}: esperado objeto JSON")
    return data


def _tree(root: str | Path, limit: int = 200) -> str:
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        return ""
    out = []
    for p in sorted(base.rglob("*")):
        if p.is_file() and not p.is_symlink():
            out.append(str(p.relative_to(base)))
        if len(out) >= limit:
            break
    return "\n".join(out)


def _brief(root: str | Path, limit_files: int = 40, limit_chars: int = 24000) -> str:
    base = Path(root).expanduser().resolve()
    parts: list[str] = []
    total = 0
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        block = f"### FILE: {p.relative_to(base)}\n```\n{text[:8000]}\n```"
        if total + len(block) > limit_chars:
            break
        parts.append(block)
        total += len(block)
        if len(parts) >= limit_files:
            break
    return "\n\n".join(parts)


class ArchitectureService:
    def __init__(self, settings: Any, pool: Any, emit: Any):
        self.settings = settings
        self.pool = pool
        self.emit = emit

    def _ask(self, project: ProjectState, system: str, user: str, tag: str) -> str:
        request = RouteRequest(
            role="architecture", complexity="high", tag=tag,
            required_capabilities=frozenset({"text"}),
            run_id=project.run_id or "",
        )
        routed = self.pool.router.route(request)
        if not routed.candidates:
            raise RuntimeError("sem candidato para architecture")
        from langchain_core.messages import HumanMessage, SystemMessage
        return self.pool.complete(
            routed.chain, [SystemMessage(system), HumanMessage(user)],
            meta={"run": project.run_id or None, "tag": tag} if project.run_id else None)

    def design(self, project: ProjectState, workspace_root: str | Path) -> dict[str, Any]:
        brief = _brief(workspace_root)
        tree = _tree(workspace_root)
        prompt = f"""REQUIREMENTS:
{json.dumps(project.requirements.to_dict(), ensure_ascii=False, indent=2)}

PROJECT CONTENT (authoritative):
{brief}

TREE:
{tree}

Design the solution. STRICT JSON:
{{"approach": str,
  "modules": [{{"path": str, "responsibility": str}}],
  "interfaces": [{{"name": str, "file": str, "inputs": [str], "outputs": [str],
                   "errors": [str], "used_by": [str]}}],
  "tasks": [{{"id": "t1", "title": str, "description": str, "target_file": str,
              "acceptance": [str], "depends_on": [str],
              "requirement_ids": [str],
              "complexity": "low"|"medium"|"high", "role": "junior"|"senior"}}],
  "test_plan": [{{"task": str, "check": str}}],
  "deletions": [{{"target_file": str, "expected_sha256": str,
                  "requirement_id": str, "reason": str}}]}}

Rules:
- One target_file per task; no two pending tasks share the same file.
- target_file must be a path inside the project, existing or new.
- complexity "high" for UI/cross-cutting logic; "low" for pure helpers.
- Interfaces must include every cross-file call (name, inputs, outputs, errors).
- Every task must list requirement_ids referencing acceptance_criteria ids.
- Deletions are only explicit user/architecture operations; never infer them."""
        raw = self._ask(project, ARCHITECT_SYSTEM, prompt, "ARCHITECT")
        data = _json_or_raise(raw, "architecture")
        tasks: list[Task] = []
        seen: set[str] = set()
        for i, t in enumerate(data.get("tasks", []) or [], 1):
            target = str(t.get("target_file", "")).strip()
            if not target or target in seen:
                continue
            seen.add(target)
            complexity = str(t.get("complexity", "medium")).lower()
            role = str(t.get("role") or ("junior" if complexity == "low" else "senior")).lower()
            if role not in ("junior", "senior"):
                role = "senior"
            tasks.append(Task(
                id=str(t.get("id") or f"t{i}"),
                title=str(t.get("title") or Path(target).name),
                description=str(t.get("description") or ""),
                kind="build", target_file=target,
                acceptance=[str(a) for a in (t.get("acceptance") or [])],
                depends_on=[str(d) for d in (t.get("depends_on") or [])],
                requirement_ids=[str(r) for r in (t.get("requirement_ids") or [])],
                complexity=complexity, role=role,
            ))
        if not tasks:
            raise ValueError("architect não devolveu tarefas válidas")
        tasks = validate_tasks(tasks, project_root=project.project_root or None)
        project.architecture = ArchitectureContract.from_dict(data)
        project.tasks = tasks
        link_traceability(project.requirements, project.tasks)
        validate_traceability(project.requirements, project.tasks)
        self.emit("ARCHITECT", f"{len(tasks)} tarefa(s)")
        return data
