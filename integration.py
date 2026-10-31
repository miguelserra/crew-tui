"""Native INTEGRATION service: entrypoint execution and workspace fixes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import native_util
from router import RouteRequest
from state import ProjectState, Task
from visual import playwright_audit

FIX_SYSTEM = (
    "You are a senior software engineer fixing runtime defects. Output each corrected "
    "file with `### FILE: <relative path>` followed by a fenced code block. No prose."
)
CHIEF_FIX_SYSTEM = (
    "You are the chief engineer. Fix the runtime defects with minimal, correct changes. "
    "Output each corrected file with `### FILE: <relative path>` followed by a fenced block."
)


class IntegrationService:
    def __init__(self, settings: Any, pool: Any, emit: Any, run_id: str = ""):
        self.settings = settings
        self.pool = pool
        self.emit = emit
        self.run_id = run_id

    def integrate(self, project: ProjectState, workspace_root: str | Path) -> bool:
        root = Path(workspace_root).expanduser().resolve()
        entry = self._entrypoint(root)
        if entry is None:
            project.integration = {"verdict": "SKIP", "note": "sem entrypoint detetável"}
            self.emit("INTEG", "sem entrypoint detetável — verificação estática apenas")
            return True
        kind, path = entry
        max_attempts = self.settings.max_verify_loops + 1
        for attempt in range(1, max_attempts + 1):
            report = self._run_entry(kind, path, root)
            project.integration = report
            if report.get("verdict") == "PASS":
                self.emit("INTEG", f"integração PASS ({kind})")
                return True
            self.emit("INTEG", f"integração FAIL ({attempt}/{max_attempts})")
            if not self._fix(project, root, report, attempt):
                return False
        self.emit("SYSTEM", "integração não passou após as tentativas permitidas")
        return False

    @staticmethod
    def _entrypoint(root: Path) -> Optional[tuple[str, Path]]:
        if (root / "index.html").is_file():
            return "web", root / "index.html"
        pkg = root / "package.json"
        if pkg.is_file():
            try:
                data = json.loads(pkg.read_text(encoding="utf-8"))
                main = data.get("main") or (data.get("scripts") or {}).get("start")
                if main:
                    return "node", pkg
            except Exception:
                pass
            return "node", pkg
        for pat in ("main.py", "app.py", "run.py"):
            if (root / pat).is_file():
                return "python", root / pat
        for p in sorted(root.glob("*.py")):
            txt = p.read_text(encoding="utf-8", errors="ignore")
            if "__main__" in txt:
                return "python", p
        for p in sorted(root.glob("*.js")):
            txt = p.read_text(encoding="utf-8", errors="ignore")
            if "__main__" not in txt and "require.main" in txt:
                return "node", p
        return None

    def _run_entry(self, kind: str, path: Path, root: Path) -> dict[str, Any]:
        if kind == "web":
            return playwright_audit(root, root / ".factory" / "logs")
        cmd = ([str(Path(sys.executable)), str(path)] if kind == "python"
               else ["node", str(path)])
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                                  cwd=str(root))
            out = ((proc.stdout or "") + (proc.stderr or "")).strip()
            ok = proc.returncode == 0
            note = ""
            if not ok and kind == "python" and "Traceback" not in out:
                low = out.lower()
                if "usage:" in low or "argument" in low or "falta" in low:
                    ok, note = True, "programa arranca (requer argumentos)"
            return {"verdict": "PASS" if ok else "FAIL", "kind": kind,
                    "cmd": " ".join(cmd), "exit": proc.returncode, "note": note,
                    "output": out[-6000:]}
        except Exception as exc:
            return {"verdict": "FAIL", "kind": kind, "cmd": " ".join(cmd),
                    "output": f"{type(exc).__name__}: {exc}"}

    def _fix(self, project: ProjectState, root: Path, report: dict[str, Any],
             attempt: int) -> bool:
        errors = native_util.format_report_errors(report)
        if not errors:
            return False
        role_tag = "chief" if attempt >= self.settings.max_verify_loops else "fixer"
        request = RouteRequest(role=role_tag, complexity="high",
                               run_id=self.run_id or "", tag="DEV")
        routed = self.pool.router.route(request)
        if not routed.candidates:
            return False
        files = self._runtime_files(project, report)
        payload = []
        for task in files:
            path = (root / task.target_file).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                continue
            if path.is_file():
                payload.append(f"### FILE: {task.target_file}\n```\n"
                               f"{path.read_text(encoding='utf-8')[:12000]}\n```")
        prompt = f"""GOAL: {project.goal}

TASK: fix the runtime/integration defects below. Only change these files:
{chr(10).join(f'- {t.target_file}' for t in files)}

ARCHITECTURE:
{json.dumps(project.architecture.interfaces, ensure_ascii=False)[:4000]}

ERROR REPORT:
{errors[:6000]}

CURRENT FILES:
{chr(10).join(payload)}

Output each corrected file with `### FILE: <relative path>` then a fenced block."""
        try:
            raw = self.pool.complete(routed.chain, [
                self._msg("SystemMessage", CHIEF_FIX_SYSTEM if role_tag == "chief" else FIX_SYSTEM),
                self._msg("HumanMessage", prompt),
            ], meta={"run": self.run_id or None, "tag": "DEV"})
        except Exception:
            return False
        written = 0
        for rel, code in native_util.multi_file_blocks(raw):
            if not code.strip():
                continue
            dest = (root / rel).resolve()
            try:
                dest.relative_to(root)
            except ValueError:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(code.rstrip("\n") + "\n", encoding="utf-8")
            for task in project.tasks:
                if task.target_file == rel:
                    task.status = "done"
            written += 1
        self.emit("INTEG", f"{written} ficheiro(s) corrigido(s) por {role_tag}")
        return written > 0

    @staticmethod
    def _runtime_files(project: ProjectState, report: dict[str, Any]) -> list[Task]:
        blob = json.dumps(report, ensure_ascii=False)
        touched = [t for t in project.tasks
                   if t.target_file in blob or Path(t.target_file).name in blob]
        if touched:
            return touched
        web = [t for t in project.tasks if t.target_file.endswith((".js", ".html", ".css"))]
        return web or project.tasks[:2]

    @staticmethod
    def _msg(kind: str, text: str) -> Any:
        from langchain_core.messages import HumanMessage, SystemMessage
        return SystemMessage(text) if kind == "SystemMessage" else HumanMessage(text)
