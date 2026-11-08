"""Native BUILD/FIX service over canonical ProjectState (no Factory)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import verify as verify_policy
from state import ProjectState, Task, VerificationRecord, artifact_hash
from state import normalize_target_file, validate_dependencies
from registry import ModelRef
from calls import CallOutcomeUnknown
from workspace import apply_generated_text
from workflow_context import ContextRecoveryRequired

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
        self.context_engine = None

    def build(self, project: ProjectState, workspace_root: str | Path) -> bool:
        validate_dependencies(project.tasks)
        root = Path(workspace_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        pending = [t for t in project.tasks
                   if t.kind == "build" and t.status not in ("done", "verified",
                                                             "skipped")]
        done_ids = {t.id for t in project.tasks
                    if t.status in ("done", "verified")}
        guard = 0
        while pending and guard < 3 * len(project.tasks) + 10:
            guard += 1
            ready = [t for t in pending
                     if all(d in done_ids for d in t.depends_on)]
            if not ready:
                for task in pending:
                    missing = [d for d in task.depends_on
                               if d not in done_ids]
                    task.errors.append("dependências não satisfeitas: "
                                       + ", ".join(missing))
                return False
            for task in ready:
                if not self._build_task(project, root, task):
                    task.status = "failed"
                    return False
                if task.status in ("done", "verified"):
                    done_ids.add(task.id)
            pending = [t for t in pending
                       if t.status not in ("done", "verified")]
        return all(t.status in ("done", "verified")
                   for t in project.tasks if t.kind == "build")

    def _core(self):
        engine = getattr(self, "context_engine", None)
        state = getattr(engine, "state", None)
        if engine is None or state is None or not getattr(state, "project_root", ""):
            return None
        return engine.context_core()

    @staticmethod
    def _build_query(project: ProjectState, task: Task) -> str:
        parts = [task.title, task.description, task.target_file, *task.acceptance]
        parts += [str(i.get("name") or "") for i in project.architecture.interfaces
                  if i.get("file") == task.target_file]
        return " ".join(p.strip() for p in parts if p and p.strip()) or task.target_file

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
        messages = [
            self._message("SystemMessage",
                          JUNIOR_SYSTEM if task.role == "junior" else DEV_SYSTEM),
            self._message("HumanMessage", prompt),
        ]
        handle = None
        try:
            llm, tag = self.pool.team_llm(task, run_id=self.run_id or "")
            if llm is None:
                task.errors.append("sem modelo")
                return False
            route = getattr(llm, "result", None)
            core = self._core()
            if core is not None and route is not None:
                logical_id = f"build-{self.run_id or project.run_id}-{task.id}"
                snapshot = core.prepare(
                    logical_id, "BUILD", route, messages,
                    target_file=task.target_file,
                    query=self._build_query(project, task),
                    sections=("structure", "dependencies", "components"))
                if snapshot is not None:
                    messages = snapshot.render_messages(self._render_message)
                    try:
                        result = core.invoke(self.pool, route, logical_id, messages)
                    except CallOutcomeUnknown as exc:
                        setattr(exc, "managed_context", True)
                        raise
                    raw = result.text
                    handle = (core, logical_id, result)
                else:
                    raw = self.pool.complete(llm, messages, meta={
                        "run": self.run_id or None, "tag": tag, "task": task.id})
            else:
                raw = self.pool.complete(llm, messages, meta={
                    "run": self.run_id or None, "tag": tag, "task": task.id})
        except ContextRecoveryRequired:
            raise
        except CallOutcomeUnknown as exc:
            if getattr(exc, "managed_context", False):
                raise
            task.errors.append(str(exc)[:300])
            return False
        except Exception as exc:
            task.errors.append(str(exc)[:300])
            return False
        code = _one_block(raw, fence)
        if not code.strip():
            if handle is not None:
                raise ContextRecoveryRequired(
                    f"{task.id}: managed BUILD output has no code block; result retained")
            task.errors.append("resposta sem bloco de código")
            return False
        ok, feedback, record = self._verify(project, task, code, root, target, lang)

        def managed_apply(final_code: str, final_record: VerificationRecord):
            producer = self.pool.last_attempt_model.get(task.id)

            def apply(state: ProjectState, managed) -> None:
                try:
                    apply_generated_text(root, task.target_file, final_code,
                                         state.extensions.get("workspace_origin") or {})
                except ValueError as exc:
                    raise ContextRecoveryRequired(str(exc)) from exc
                state.verifications[task.id] = final_record
                task.verification = final_record.to_dict()
                task.status = "done"
                task.delivered_by = producer or ModelRef.from_dict(managed.served)
            return apply

        if handle is not None and ok:
            core, logical_id, _result = handle
            core.commit(logical_id, managed_apply(code, record))
            self.emit("DEV", f"{task.id} entregue em {task.target_file}")
            return True
        if handle is None and ok:
            self._install(task, target, code)
            project.verifications[task.id] = record
            task.verification = record.to_dict()
            task.status = "done"
            self.emit("DEV", f"{task.id} entregue em {task.target_file}")
            return True

        # Definitive verification failure (managed or legacy): record evidence and
        # run the existing automatic FIX. A CONFIRMED managed BUILD result is
        # recovered locally on resume; it is never re-submitted to a provider.
        project.verifications[task.id] = record
        task.verification = record.to_dict()
        if record.verdict == verify_policy.UNVERIFIED:
            task.status = "failed"
            self.emit("SYSTEM", f"{task.id}: UNVERIFIED — não entregue")
            return False
        oracle_failed = not record.oracle.get("passed", True)
        if oracle_failed:
            error = record.reason or "oracle falhou"
            task.errors.append(f"oracle FAIL: {error}")
        else:
            objections = (record.critic.get("objections")
                          if isinstance(record.critic, dict) else [])
            detail = "; ".join(
                str(o.get("claim") or o.get("why_wrong") or o)
                if isinstance(o, dict) else str(o)
                for o in objections)
            error = record.reason or "critic FAIL"
            task.errors.append(f"critic FAIL: {detail or error}")
        try:
            fixed = self._fix(project, task, code, error, root)
        except CallOutcomeUnknown:
            raise
        except Exception as exc:
            task.errors.append(str(exc)[:300])
            return False
        if fixed is None:
            task.status = "failed"
            return False
        new_code, record = fixed
        if handle is not None:
            core, logical_id, _result = handle
            core.commit(logical_id, managed_apply(new_code, record))
        else:
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
        except CallOutcomeUnknown:
            raise
        except Exception:
            return None
        new_code = _one_block(raw, fence)
        if not new_code.strip():
            return None
        ok, _fb, record = self._verify(project, task, new_code, root, target, lang)
        if not ok:
            project.verifications[task.id] = record
            task.verification = record.to_dict()
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
        if not oracle.passed:
            critic: dict[str, Any] = {"verdict": verify_policy.NA,
                                      "note": "oracle falhou — crítico não executado"}
        else:
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
            oracle={"passed": oracle.passed, "exit_code": oracle.exit_code,
                    "artifact_sha256": artifact_hash(code)},
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
    def _render_message(role: str, text: str) -> Any:
        from langchain_core.messages import HumanMessage, SystemMessage
        return SystemMessage(text) if role == "system" else HumanMessage(text)

    @staticmethod
    def _lang(path: Path) -> str:
        from main import guess_lang
        return guess_lang(path) or ""

    @staticmethod
    def _now() -> str:
        import time
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
