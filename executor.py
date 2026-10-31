"""Native BUILD/FIX service over canonical ProjectState (no Factory)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import verify as verify_policy
from state import ProjectState, Task, VerificationRecord
from state import normalize_target_file
from registry import ModelRef

DEV_SYSTEM = (
    "You are a senior software engineer. You write production-quality code for exactly one "
    "file. You follow the given interfaces literally. You output the COMPLETE file content "
    "in ONE fenced block, no prose, no placeholders, no elided sections."
)
JUNIOR_SYSTEM = (
    "You are a junior software engineer. You implement exactly what the interface says, "
    "keeping it small and dependency-free. You output the COMPLETE file content in ONE "
    "fenced block, no prose, no placeholders, no elided sections."
)


def _one_block(raw: str, fence: str) -> str:
    from main import extract_code_blocks_typed
    blocks = [(lang, code) for lang, code in extract_code_blocks_typed(raw) if code]
    if not blocks:
        return ""
    if fence:
        for lang, code in blocks:
            if lang == fence or (fence == "javascript" and lang in ("js", "mjs")):
                return code
    return max(blocks, key=lambda b: len(b[1]))[1]


def _safe(root: Path, rel: str) -> Path:
    rel = normalize_target_file(rel)
    if not rel:
        raise ValueError("target_file vazio")
    dest = (root / rel).resolve()
    try:
        dest.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"target fora da raiz: {rel}") from exc
    return dest


def _as_ref(value: Any) -> Optional[ModelRef]:
    if value is None:
        return None
    if isinstance(value, ModelRef):
        return value
    return ModelRef.from_dict(value)


class ExecutorService:
    def __init__(self, settings: Any, pool: Any, verifier: Any, emit: Any,
                 run_id: str = ""):
        self.settings = settings
        self.pool = pool
        self.verifier = verifier
        self.emit = emit
        self.run_id = run_id

    def build(self, project: ProjectState, workspace_root: str | Path) -> bool:
        root = Path(workspace_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        for task in project.tasks:
            if task.kind != "build" or task.status in ("done", "verified"):
                continue
            if not self._build_task(project, root, task):
                return False
        return all(t.status in ("done", "verified") for t in project.tasks if t.kind == "build")

    def _build_task(self, project: ProjectState, root: Path, task: Task) -> bool:
        target = _safe(root, task.target_file)
        lang = self._lang(target)
        fence = "" if lang in ("html", "css", "text", "") else lang
        existing = target.read_text(encoding="utf-8") if target.is_file() else ""
        arch = project.architecture
        contracts = [i for i in arch.interfaces if i.get("file") == task.target_file]
        prompt = f"""PROJECT GOAL: {project.goal}

ARCHITECTURE APPROACH:
{arch.approach}

INTERFACES FOR THIS FILE:
{json.dumps(contracts, ensure_ascii=False, indent=2)}

ALL INTERFACES:
{json.dumps(arch.interfaces, ensure_ascii=False, indent=2)}

TASK {task.id}: {task.title}
{task.description}

ACCEPTANCE CRITERIA:
{chr(10).join(f'- {a}' for a in task.acceptance) or '- works as described'}

Deliverable file: {task.target_file}
Output the COMPLETE file content in ONE fenced block tagged ```{fence or 'text'}.
No prose, no placeholders, no elided sections.
{f"CURRENT CONTENT (keep what works):{chr(10)}```{fence}{chr(10)}{existing[:12000]}{chr(10)}```" if existing else ""}"""
        try:
            llm, tag = self.pool.team_llm(task, run_id=self.run_id or "")
            if llm is None:
                task.errors.append("sem modelo")
                return False
            raw = self.pool.complete(llm, [
                self._message("SystemMessage", JUNIOR_SYSTEM if task.role == "junior" else DEV_SYSTEM),
                self._message("HumanMessage", prompt),
            ], meta={"run": self.run_id or None, "tag": tag, "task": task.id})
        except Exception as exc:
            task.errors.append(str(exc)[:300])
            return False
        code = _one_block(raw, fence)
        if not code.strip():
            task.errors.append("resposta sem bloco de código")
            return False
        ok, _feedback, record = self._verify(project, task, code, root, target, lang)
        if ok:
            self._install(task, target, code)
            project.verifications[task.id] = record
            task.verification = record.to_dict()
            task.status = "done"
            self.emit("DEV", f"{task.id} entregue em {task.target_file}")
            return True
        if record.verdict == verify_policy.UNVERIFIED:
            task.status = "failed"
            project.verifications[task.id] = record
            task.verification = record.to_dict()
            self.emit("SYSTEM", f"{task.id}: UNVERIFIED — não entregue")
            return False
        # Quality FAIL: one native fix attempt, then re-verify.
        error = record.reason or "critic FAIL"
        try:
            fixed = self._fix(project, task, code, error, root)
        except Exception as exc:
            task.errors.append(str(exc)[:300])
            return False
        if fixed is None:
            task.status = "failed"
            return False
        new_code, record = fixed
        self._install(task, target, new_code)
        project.verifications[task.id] = record
        task.verification = record.to_dict()
        task.status = "done"
        return True

    def _fix(self, project: ProjectState, task: Task, code: str, error: str,
             root: Path) -> Optional[tuple[str, VerificationRecord]]:
        from router import RouteRequest
        request = RouteRequest(role="fixer", complexity="high", escalation=True,
                               required_capabilities=frozenset({"text"}),
                               task_id=task.id, run_id=self.run_id or "",
                               producer=_as_ref(task.delivered_by), tag="DEV")
        routed = self.pool.router.route(request)
        if not routed.candidates:
            return None
        target = _safe(root, task.target_file)
        lang = self._lang(target)
        fence = "" if lang in ("html", "css", "text", "") else lang
        prompt = f"""GOAL: {project.goal}
FILE: {task.target_file}
ACCEPTANCE:
{chr(10).join(f'- {a}' for a in task.acceptance) or '- works'}
CURRENT CONTENT:
```{fence}
{code[:14000]}
```
VERIFICATION ERROR:
{error[:4000]}
Output the COMPLETE corrected file in ONE fenced block."""
        try:
            raw = self.pool.complete(routed.chain, [
                self._message("SystemMessage", DEV_SYSTEM),
                self._message("HumanMessage", prompt),
            ], meta={"run": self.run_id or None, "tag": "DEV", "task": task.id})
        except Exception:
            return None
        new_code = _one_block(raw, fence)
        if not new_code.strip():
            return None
        ok, _fb, record = self._verify(project, task, new_code, root, target, lang)
        if not ok:
            return None
        return new_code, record

    def _verify(self, project: ProjectState, task: Task, code: str, root: Path,
                target: Path, lang: str) -> tuple[bool, str, VerificationRecord]:
        suffix = target.suffix
        artifact = f"```{lang or 'text'}\n{code}\n```"
        oracle = self.verifier.run_oracle(task.id, artifact, self.run_id, suffix=suffix)
        producer = self.pool.last_attempt_model.get(task.id)
        if producer is None:
            producer = _as_ref(task.delivered_by)
        critic = self.verifier.run_critic(producer, task, code, self.run_id)
        tests: dict[str, Any] = {"verdict": "SKIP", "advisory": True}
        if suffix == ".py" and self.settings.test_oracle:
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
            at=self._now())
        return decision.deliver, "; ".join(decision.reasons), record

    def _install(self, task: Task, target: Path, code: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code.rstrip("\n") + "\n", encoding="utf-8")
        producer = self.pool.last_attempt_model.get(task.id)
        if producer is not None:
            task.delivered_by = producer

    @staticmethod
    def _message(kind: str, text: str) -> Any:
        from langchain_core.messages import HumanMessage, SystemMessage
        return SystemMessage(text) if kind == "SystemMessage" else HumanMessage(text)

    @staticmethod
    def _lang(path: Path) -> str:
        from main import guess_lang
        return guess_lang(path) or ""

    @staticmethod
    def _now() -> str:
        import time
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
