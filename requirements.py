"""Native SPEC/requirements service over canonical ProjectState."""

from __future__ import annotations

import json
from typing import Any

from router import RouteRequest
from state import ProjectState, RequirementContract

ANALYST_SYSTEM = (
    "You are a senior requirements engineer. You are terse, factual and decisive. "
    "No small talk, no praise, no restating the user. You ask at most five questions, "
    "only when the answer changes the implementation. You output STRICT JSON only."
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


class RequirementsService:
    def __init__(self, settings: Any, pool: Any, emit: Any):
        self.settings = settings
        self.pool = pool
        self.emit = emit

    def _ask(self, project: ProjectState, system: str, user: str, tag: str,
             task_id: str = "", complexity: str = "medium") -> str:
        request = RouteRequest(
            role="spec", complexity=complexity, tag=tag,
            required_capabilities=frozenset({"text"}),
            task_id=task_id, run_id=project.run_id or "",
        )
        routed = self.pool.router.route(request)
        if not routed.candidates:
            raise RuntimeError("sem candidato para requirements")
        from langchain_core.messages import HumanMessage, SystemMessage
        return self.pool.complete(
            routed.chain, [SystemMessage(system), HumanMessage(user)],
            meta={"run": project.run_id or None, "tag": tag,
                  "task": task_id} if project.run_id else None)

    def prepare(self, project: ProjectState, feedback: str = "") -> dict[str, Any]:
        project.phase = "SPEC"
        project.goal = (project.goal or "").strip()
        ctx = ""
        if project.requirements.objective:
            ctx = ("\nPREVIOUS REQUIREMENTS (refine, do not restart):\n"
                   + json.dumps(project.requirements.to_legacy_dict(),
                                ensure_ascii=False, indent=2))
        if project.requirements.answers:
            ctx += "\nUSER ANSWERS SO FAR:\n" + "\n".join(
                f"- Q: {a.get('question')}\n  A: {a.get('answer')}"
                for a in project.requirements.answers)
        if feedback:
            ctx += f"\nNEW FEEDBACK FROM THE USER:\n{feedback}"
        prompt = f"""GOAL:
{project.goal}
{ctx}

Produce the requirements contract. STRICT JSON:
{{"objective": str,
  "scope_in": [str],
  "scope_out": [str],
  "constraints": [str],
  "acceptance": [str],
  "assumptions": [str],
  "open_questions": [str]}}

Rules: acceptance items must be testable by running something (command, browser,
assert). At most 5 open_questions, each one line, only if it changes the code.
If the goal is clear, return an empty list."""
        raw = self._ask(project, ANALYST_SYSTEM, prompt, "ANALYST")
        req = _json_or_raise(raw, "requirements")
        req.setdefault("objective", project.goal)
        req.setdefault("acceptance", [])
        req.setdefault("open_questions", [])
        project.requirements = RequirementContract.from_dict(req)
        self.emit("ANALYST", f"requisitos: {req.get('objective', '')[:160]}")
        return req

    def answer(self, project: ProjectState, question: str, answer: str) -> None:
        project.requirements.answers.append({"question": question, "answer": answer})
