"""Native SIGN-OFF service: aggregate evidence and write project_memory.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import context_reader
import native_util
from router import RouteRequest
from state import ProjectState


class SignoffService:
    def __init__(self, settings: Any, pool: Any, emit: Any, run_id: str = ""):
        self.settings = settings
        self.pool = pool
        self.emit = emit
        self.run_id = run_id

    def signoff(self, project: ProjectState, workspace_root: str | Path,
                final_root: str | Path | None = None) -> bool:
        root = Path(workspace_root).expanduser().resolve()
        ctx = context_reader.read_project_context(root, max_files=20,
                                                  max_total_chars=12000)
        summary = f"""GOAL: {project.goal}
REQUIREMENTS: {json.dumps(project.requirements.to_dict(), ensure_ascii=False)[:3000]}
ARCHITECTURE: {json.dumps(project.architecture.to_dict(), ensure_ascii=False)[:2500]}
TASKS: {json.dumps([{'id': t.id, 'file': t.target_file, 'status': t.status} for t in project.tasks], ensure_ascii=False)}
INTEGRATION: {json.dumps(project.integration, ensure_ascii=False)[:2000]}
VISUAL: {json.dumps(project.visual, ensure_ascii=False)[:1000]}
TREE: {chr(10).join(ctx.tree)}"""
        prompt = f"""{summary}

Write the handover memory for the next engineer. STRICT JSON:
{{"what_it_is": str, "how_to_run": [str], "entrypoints": [str],
  "architecture_notes": [str], "tests": [str],
  "known_issues": [str], "next_steps": [str]}}"""
        memory: dict[str, Any] = {}
        request = RouteRequest(role="architecture", complexity="high",
                               run_id=self.run_id or "", tag="SIGN")
        routed = self.pool.router.route(request)
        if routed.candidates:
            try:
                raw = self.pool.complete(routed.chain, [
                    self._msg("SystemMessage", "You output valid JSON only."),
                    self._msg("HumanMessage", prompt),
                ], meta={"run": self.run_id or None, "tag": "SIGN"})
                memory = native_util.json_or_raise(raw, "signoff")
            except Exception as exc:
                memory = {"note": f"sign-off gerado sem modelo: {exc}"}
        payload = {
            "project": str(Path(final_root).expanduser().resolve() if final_root else root),
            "goal": project.goal,
            "generated_at": native_util.now(),
            "requirements": project.requirements.to_dict(),
            "architecture": project.architecture.to_dict(),
            "tasks": [t.to_dict() for t in project.tasks],
            "integration": project.integration,
            "visual": project.visual,
            "handover": memory,
        }
        (root / "project_memory.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        project.signoff = payload
        self.emit("SIGN", "project_memory.json gravado no projeto")
        return True

    @staticmethod
    def _msg(kind: str, text: str) -> Any:
        from langchain_core.messages import HumanMessage, SystemMessage
        return SystemMessage(text) if kind == "SystemMessage" else HumanMessage(text)
