"""Native INTEGRATION service: entrypoint execution and workspace fixes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import native_util
import verify as verify_policy
from router import RouteRequest
from state import ProjectState, Task, TaskAttempt, VerificationRecord, new_id
from registry import unknown_ref
from visual import playwright_audit

_LANG_SUFFIX = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript",
    ".html": "html", ".css": "css", ".txt": "text", ".md": "text",
}

FIX_SYSTEM = (
    "You are a senior software engineer fixing runtime defects. Output each corrected "
    "file with `### FILE: <relative path>` followed by a fenced code block. No prose."
)
CHIEF_FIX_SYSTEM = (
    "You are the chief engineer. Fix the runtime defects with minimal, correct changes. "
    "Output each corrected file with `### FILE: <relative path>` followed by a fenced block."
)


class IntegrationService:
    def __init__(self, settings: Any, pool: Any, emit: Any, run_id: str = "",
                 artifact_dir: str | Path | None = None, verifier: Any = None):
        self.settings = settings
        self.pool = pool
        self.emit = emit
        self.run_id = run_id
        self.verifier = verifier
        self.artifact_dir = Path(artifact_dir).expanduser() if artifact_dir else None

    def _artifacts(self, root: Path) -> Path:
        return (self.artifact_dir or (root / ".crew" / "runs" / (self.run_id or "latest")))

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
            fixes = list(project.integration.get("fix_records") or [])
            report = self._run_entry(kind, path, root)
            report["fix_records"] = fixes
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
            return playwright_audit(root, self._artifacts(root) / "logs" / "integration")
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
            ], meta={"run": self.run_id or None, "tag": "DEV",
                     "task": "__integration_fix__"})
        except Exception:
            return False
        model = self.pool.last_attempt_model.get("__integration_fix__") or unknown_ref()
        call_id = new_id("intfix_")
        written = 0
        touched: set[str] = set()
        touched_files: list[str] = []
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
            touched_files.append(rel)
            for task in project.tasks:
                if task.target_file == rel:
                    touched.add(task.id)
                    task.attempts.append(TaskAttempt(
                        attempt_id=call_id, model=model,
                        started_at=native_util.now(), completed_at=native_util.now(),
                        outcome="ok"))
            written += 1
        revalidations = []
        for task in project.tasks:
            if task.id in touched:
                revalidations.append(self._revalidate(project, root, task, model))
        if written:
            project.integration.setdefault("fix_records", []).append({
                "call_id": call_id, "model": model.to_dict(),
                "task_ids": sorted(touched), "files": sorted(set(touched_files)),
                "outcome": "ok", "revalidations": revalidations,
                "at": native_util.now(),
            })
        self.emit("INTEG", f"{written} ficheiro(s) corrigido(s) por {role_tag}")
        if revalidations and not all(r.get("delivered") for r in revalidations):
            self.emit("INTEG", "revalidação do fixer falhou — integração bloqueada")
            return False
        return written > 0

    def _revalidate(self, project: ProjectState, root: Path, task: Task,
                    model: Any) -> dict[str, Any]:
        """Re-run oracle + cross-family critic on a file changed by the fixer.

        The fix call is a single generation attempt; verification is recorded
        separately and invalidates any prior evidence for the affected task.
        """
        if self.verifier is None:
            task.status = "failed"
            return {"task_id": task.id, "verdict": verify_policy.UNVERIFIED,
                    "delivered": False, "note": "verificador indisponível"}
        target = (root / task.target_file).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError:
            task.status = "failed"
            return {"task_id": task.id, "verdict": verify_policy.FAIL,
                    "delivered": False, "note": "alvo fora da raiz"}
        if not target.is_file():
            task.status = "failed"
            return {"task_id": task.id, "verdict": verify_policy.FAIL,
                    "delivered": False, "note": "ficheiro ausente"}
        code = target.read_text(encoding="utf-8")
        lang = _LANG_SUFFIX.get(target.suffix.lower(), "")
        fence = "" if lang in ("html", "css", "text", "") else lang
        artifact = f"```{fence or 'text'}\n{code}\n```"
        oracle = self.verifier.run_oracle(task.id, artifact, self.run_id,
                                          suffix=target.suffix)
        critic = self.verifier.run_critic(model, task, code, self.run_id)
        tests: dict[str, Any] = {"verdict": "SKIP", "advisory": True}
        if target.suffix == ".py" and self.settings.test_oracle:
            tests = self.verifier.run_test_oracle(task, artifact, self.run_id)
            tests["advisory"] = True
        decision = verify_policy.decide_build(
            oracle.passed, critic, visual={"verdict": verify_policy.NA},
            tests=tests, allow_unverified=self.settings.allow_unverified)
        record = VerificationRecord(
            verdict=decision.verdict, criterion_ids=list(task.requirement_ids),
            oracle={"passed": oracle.passed, "exit_code": oracle.exit_code},
            critic=critic if isinstance(critic, dict) else {},
            visual={"verdict": verify_policy.NA}, tests=tests,
            reason="; ".join(decision.reasons),
            authorized_unverified=(decision.verdict == verify_policy.UNVERIFIED
                                   and decision.deliver),
            at=native_util.now())
        project.verifications[task.id] = record
        task.verification = record.to_dict()
        if decision.deliver:
            task.status = "done"
            task.delivered_by = model
        else:
            task.status = "failed"
        return {"task_id": task.id, "verdict": decision.verdict,
                "delivered": decision.deliver}

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
