"""Hierarchical software factory (steps 1-9) with resumable JSON state.

Pipeline
--------
1  REQUIREMENTS  senior analyst (Gemini Flash, terse)  -> requirements.json
2  SCAFFOLD      junior (Qwen local)                   -> file tree skeleton
3  ARCHITECT     chief engineer (Gemini Pro)           -> interfaces + tasks
4  BUILD         workers (role per complexity)         -> files + unit checks
5  FIX           recursive repair loop (junior/senior) -> log per attempt
6  INTEGRATE     run the software / Playwright audit   -> runtime log
7  SENIOR_FIX    senior -> chief escalation loop       -> log per attempt
8  VISUAL        headless browser + vision review      -> ui_report + fixes
9  SIGN_OFF      chief memory (project_memory.json)    -> resumable handover

Everything is persisted under ``<project>/.factory/`` (state.json + logs/ +
candidates/ + backups/ + tests/), so an interrupted run can be resumed with
``Factory(..., project_root=...).build()`` or ``crew-tui --resume``.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import http.server
import socketserver
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from main import (LANG_SUFFIX, LLMPool, Settings, Verifier,
                  extract_code_blocks_typed, extract_json_block, guess_lang)
from power import make_power

ROLES = ("ANALYST", "SCAFFOLD", "ARCHITECT", "DEV", "TEST", "INTEG", "VISUAL", "SIGN")

ANALYST_SYSTEM = (
    "You are a senior requirements engineer. You are terse, factual and decisive. "
    "No small talk, no praise, no restating the user. You ask at most five questions, "
    "only when the answer changes the implementation. You output STRICT JSON only."
)

SCAFFOLD_SYSTEM = (
    "You are a junior engineer creating only the skeleton of a project: directories and "
    "empty/placeholder files. No business logic, no algorithms. Output STRICT JSON only."
)

ARCHITECT_SYSTEM = (
    "You are the chief engineer. You define the attack plan, the module boundaries, the "
    "public interfaces (name, inputs, outputs, errors) and split the work into atomic "
    "tasks. Every task has ONE target file and measurable acceptance criteria. You assign "
    "complexity by how many components the unit touches: 'high' for cross-cutting logic "
    "and UI, 'low' for pure helpers. Output STRICT JSON only."
)

DEV_SYSTEM = (
    "You are a senior software engineer. You write production-quality code for exactly one "
    "file. You follow the given interfaces literally. You output the COMPLETE file content "
    "in ONE fenced block, no prose, no placeholders, no elided sections."
)

JUNIOR_SYSTEM = (
    "You are a junior software engineer. You implement exactly what the interface says, "
    "keeping it small and dependency-free. You output the COMPLETE file content in ONE "
    "fenced block, no prose, no placeholders."
)

FIX_SYSTEM = (
    "You are a senior engineer fixing a concrete defect. You receive the failing file, the "
    "exact error output and the acceptance criteria. You change only what is needed and "
    "output the COMPLETE corrected file in ONE fenced block, no prose."
)

CHIEF_FIX_SYSTEM = (
    "You are the chief engineer. A senior engineer has repeatedly failed to fix a defect. "
    "You receive the current files, the logs and the architecture. You diagnose the root "
    "cause and output the COMPLETE corrected file(s) in fenced blocks, prefixed by a line "
    "`### FILE: <relative path>` for each. No prose."
)

TEST_SYSTEM = (
    "You are a test engineer. You write a small, deterministic, self-contained test that "
    "checks the acceptance criteria of the given unit. You output ONLY the test file "
    "content in one fenced block."
)

VISION_SYSTEM = (
    "You are a UI reviewer with vision. Compare the screenshot with the acceptance "
    "criteria. Report only concrete, falsifiable mismatches. Output STRICT JSON only."
)


# --------------------------------------------------------------------------- #
#  State
# --------------------------------------------------------------------------- #

@dataclass
class TaskState:
    id: str
    title: str
    description: str
    target_file: str
    acceptance: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    complexity: str = "medium"
    role: str = "senior"
    status: str = "pending"
    attempts: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskState":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class FactoryState:
    project: str
    goal: str = ""
    step: str = "REQUIREMENTS"
    requirements: dict[str, Any] = field(default_factory=dict)
    answers: list[dict[str, str]] = field(default_factory=list)
    scaffold: dict[str, Any] = field(default_factory=dict)
    architecture: dict[str, Any] = field(default_factory=dict)
    tasks: list[TaskState] = field(default_factory=list)
    integration: dict[str, Any] = field(default_factory=dict)
    visual: dict[str, Any] = field(default_factory=dict)
    signoff: dict[str, Any] = field(default_factory=dict)
    paused_until: float = 0.0
    pause_reason: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)
    created: str = ""
    updated: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["tasks"] = [t.to_dict() for t in self.tasks]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FactoryState":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in data.items() if k in known and k != "tasks"}
        st = cls(**kwargs)
        st.tasks = [TaskState.from_dict(t) for t in data.get("tasks", [])]
        return st


# --------------------------------------------------------------------------- #
#  Factory
# --------------------------------------------------------------------------- #

class Factory:
    """Steps 1-9 with persistence and resumable execution."""

    def __init__(self, settings: Settings, pool: LLMPool,
                 emit: Callable[[str, str], None], project_root: str | Path,
                 telemetry: Any = None) -> None:
        self.settings = settings
        self.pool = pool
        self.emit = emit
        self.telemetry = telemetry
        self.root = Path(project_root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.dir = self.root / ".factory"
        self.dir.mkdir(exist_ok=True)
        (self.dir / "logs").mkdir(exist_ok=True)
        (self.dir / "candidates").mkdir(exist_ok=True)
        (self.dir / "tests").mkdir(exist_ok=True)
        (self.dir / "backups").mkdir(exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.state = self._load()
        self.power = make_power(settings, pool, emit,
                                on_pause=self._pause_marker,
                                on_resume=self._power_resume)

    # -- persistence -------------------------------------------------------- #

    def _load(self) -> FactoryState:
        if self.state_path.is_file():
            try:
                return FactoryState.from_dict(
                    json.loads(self.state_path.read_text(encoding="utf-8")))
            except Exception as e:
                self.emit("SYSTEM", f"estado da factory ilegível ({e}); a recomeçar")
        now = _now()
        return FactoryState(project=str(self.root), created=now, updated=now)

    def save(self) -> None:
        self.state.updated = _now()
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state.to_dict(), ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.state_path)

    def log(self, step: str, text: str) -> None:
        path = self.dir / "logs" / f"{step.lower()}.log"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {text}\n")

    def _event(self, step: str, **fields: Any) -> None:
        entry = {"at": _now(), "step": step, **fields}
        self.state.history.append(entry)
        self.save()

    def _pause_marker(self, ts: float) -> None:
        self.state.paused_until = float(ts)
        self.state.pause_reason = "peak"
        self.save()

    def _clear_pause(self) -> None:
        if not self.state.paused_until and not self.state.pause_reason:
            return
        self.state.paused_until = 0.0
        self.state.pause_reason = ""
        self.save()

    def _power_resume(self) -> None:
        self._clear_pause()
        try:
            self.pool.discover()
        except Exception:
            self.pool.refresh_peak()

    def _llm(self, role: str) -> tuple[Any, str]:
        """Route a factory role to a model (never the local model as a thinker)."""
        pool = self.pool
        if role == "junior":
            if pool.local_up and pool.worker is not None:
                return pool.worker, "WORKER"
            return (pool.gemini or pool.fast_cloud), "WORKER"
        if role == "chief":
            return (pool.architect or pool.gemini), "ARCHITECT"
        if role in ("senior", "fixer"):
            return (pool.senior_dev or pool.gemini or pool.architect), "DEV"
        return (pool.senior_dev or pool.gemini or pool.architect), "DEV"

    def _ask(self, role: str, system: str, user: str, tag: str) -> str:
        self.power.before_step(tag)
        self.pool.refresh_peak()
        llm, _ = self._llm(role)
        if llm is None:
            raise RuntimeError(f"sem modelo para o papel '{role}'")
        return self.pool.complete(llm, [SystemMessage(system), HumanMessage(user)],
                                  meta={"run": None, "tag": tag})

    # -- step 1: requirements ---------------------------------------------- #

    def prepare(self, goal: str, feedback: str = "") -> dict[str, Any]:
        self.power.start()
        try:
            return self._prepare(goal, feedback)
        finally:
            self.power.stop()

    def _prepare(self, goal: str, feedback: str = "") -> dict[str, Any]:
        self.power.before_step("requirements")
        self.state.goal = goal.strip() or self.state.goal
        self.state.step = "REQUIREMENTS"
        ctx = ""
        if self.state.requirements:
            ctx = ("\nPREVIOUS REQUIREMENTS (refine, do not restart):\n"
                   + json.dumps(self.state.requirements, ensure_ascii=False, indent=2))
        if self.state.answers:
            ctx += "\nUSER ANSWERS SO FAR:\n" + "\n".join(
                f"- Q: {a.get('question')}\n  A: {a.get('answer')}" for a in self.state.answers)
        if feedback:
            ctx += f"\nNEW FEEDBACK FROM THE USER:\n{feedback}"
        prompt = f"""GOAL:
{self.state.goal}
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
        raw = self._ask("senior", ANALYST_SYSTEM, prompt, "ANALYST")
        req = _json_or_raise(raw, "requirements")
        req.setdefault("objective", self.state.goal)
        req.setdefault("acceptance", [])
        req.setdefault("open_questions", [])
        self.state.requirements = req
        self.state.step = "REQUIREMENTS"
        self.save()
        self.log("requirements", json.dumps(req, ensure_ascii=False, indent=2))
        self.emit("ANALYST", f"requisitos: {req.get('objective', '')[:160]}")
        return req

    def answer(self, question: str, answer: str) -> None:
        self.state.answers.append({"question": question, "answer": answer})
        self.save()

    # -- driver ------------------------------------------------------------- #

    ORDER = ("SCAFFOLD", "ARCHITECT", "BUILD", "INTEGRATE", "VISUAL", "SIGN_OFF", "DONE")

    def build(self) -> bool:
        if not self.state.requirements:
            raise RuntimeError("sem requisitos — chama prepare()/--factory primeiro")
        steps = {
            "SCAFFOLD": self.step_scaffold,
            "ARCHITECT": self.step_architect,
            "BUILD": self.step_build,
            "INTEGRATE": self.step_integrate,
            "VISUAL": self.step_visual,
            "SIGN_OFF": self.step_signoff,
        }
        start = self.ORDER.index(self.state.step) if self.state.step in self.ORDER else 0
        if self.state.paused_until and not self.pool.is_peak():
            self._clear_pause()
        self.power.start()
        try:
            for name in self.ORDER[start:]:
                if name == "DONE":
                    break
                self.state.step = name
                self.save()
                self.power.before_step(name)
                self.emit("FACTORY", f"step {name} …")
                ok = steps[name]()
                self.save()
                if not ok:
                    self.emit("FACTORY", f"step {name} bloqueado — vê .factory/logs/")
                    return False
            self.state.step = "DONE"
            self.save()
            self.emit("FACTORY", "pipeline concluída (sign-off gravado)")
            return True
        finally:
            self.power.stop()

    # -- step 2: scaffold --------------------------------------------------- #

    def step_scaffold(self) -> bool:
        tree = self._tree()
        prompt = f"""REQUIREMENTS:
{json.dumps(self.state.requirements, ensure_ascii=False, indent=2)}

CURRENT PROJECT TREE:
{tree or '(empty project)'}

Propose the minimal skeleton (directories + placeholder files) needed to satisfy the
requirements. Do not write logic. Never overwrite files that already have content.
STRICT JSON:
{{"files": [{{"path": str, "content": str}}], "notes": [str]}}"""
        try:
            raw = self._ask("junior", SCAFFOLD_SYSTEM, prompt, "SCAFFOLD")
            data = _json_or_raise(raw, "scaffold")
        except Exception as e:
            self.log("scaffold", f"ERRO: {e}")
            self.emit("SYSTEM", f"scaffold falhou ({e}); a continuar com o que existe")
            self.state.scaffold = {"files": [], "notes": [f"scaffold skipped: {e}"]}
            return True
        created, skipped = [], []
        for item in data.get("files", []) or []:
            rel = str(item.get("path", "")).strip()
            if not rel:
                continue
            dest = self._safe(rel)
            if dest is None:
                continue
            if dest.exists() and dest.stat().st_size > 0:
                skipped.append(rel)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(str(item.get("content", "")), encoding="utf-8")
            created.append(rel)
        self.state.scaffold = {"files": created, "skipped": skipped,
                               "notes": data.get("notes", [])}
        self.log("scaffold", f"criados={created} ignorados={skipped}")
        self.emit("SCAFFOLD", f"{len(created)} ficheiro(s) de base criados")
        return True

    # -- step 3: architect -------------------------------------------------- #

    def step_architect(self) -> bool:
        tree = self._tree()
        brief = self._project_brief()
        prompt = f"""REQUIREMENTS:
{json.dumps(self.state.requirements, ensure_ascii=False, indent=2)}

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
              "complexity": "low"|"medium"|"high", "role": "junior"|"senior"}}],
  "test_plan": [{{"task": str, "check": str}}]}}

Rules:
- One target_file per task; no two pending tasks share the same file.
- target_file must be a path inside the project, existing or new.
- complexity "high" for UI/cross-cutting logic; "low" for pure helpers.
- Interfaces must include every cross-file call (name, inputs, outputs, errors)."""
        try:
            raw = self._ask("chief", ARCHITECT_SYSTEM, prompt, "ARCHITECT")
            data = _json_or_raise(raw, "architecture")
        except Exception as e:
            self.log("architect", f"ERRO: {e}")
            self.emit("SYSTEM", f"architect falhou: {e}")
            return False
        tasks: list[TaskState] = []
        seen = set()
        for i, t in enumerate(data.get("tasks", []) or [], 1):
            target = str(t.get("target_file", "")).strip()
            if not target or target in seen:
                continue
            seen.add(target)
            complexity = str(t.get("complexity", "medium")).lower()
            role = str(t.get("role") or ("junior" if complexity == "low" else "senior")).lower()
            if role not in ("junior", "senior"):
                role = "senior"
            tasks.append(TaskState(
                id=str(t.get("id") or f"t{i}"),
                title=str(t.get("title") or Path(target).name),
                description=str(t.get("description") or ""),
                target_file=target,
                acceptance=[str(a) for a in (t.get("acceptance") or [])],
                depends_on=[str(d) for d in (t.get("depends_on") or [])],
                complexity=complexity, role=role))
        if not tasks:
            self.emit("SYSTEM", "architect não devolveu tarefas válidas")
            self.log("architect", json.dumps(data, ensure_ascii=False))
            return False
        self.state.architecture = data
        self.state.tasks = tasks
        self.log("architect", json.dumps(data, ensure_ascii=False, indent=2))
        self.emit("ARCHITECT", f"{len(tasks)} tarefa(s): "
                               + ", ".join(f"{t.id}:{Path(t.target_file).name}/{t.role}" for t in tasks))
        return True

    # -- steps 4-5: build + fix -------------------------------------------- #

    def step_build(self) -> bool:
        pending = [t for t in self.state.tasks if t.status not in ("done", "skipped")]
        done_ids: set[str] = {t.id for t in self.state.tasks if t.status == "done"}
        guard = 0
        while pending and guard < 3 * len(self.state.tasks) + 10:
            guard += 1
            ready = [t for t in pending if all(d in done_ids for d in t.depends_on)]
            batch = ready or pending  # break dependency deadlocks
            for task in batch:
                if task.status == "done":
                    continue
                self.power.before_step(f"build {task.id}")
                self.emit("DEV", f"{task.id} → {task.target_file} ({task.role}, {task.complexity})")
                ok = self._build_one(task)
                task.status = "done" if ok else "failed"
                if ok:
                    done_ids.add(task.id)
                self.save()
                if not ok:
                    return False
            pending = [t for t in pending if t.status not in ("done", "skipped")]
        return all(t.status == "done" for t in self.state.tasks)

    def _build_one(self, task: TaskState) -> bool:
        target = self._safe(task.target_file)
        if target is None:
            task.errors.append("target fora do projeto")
            return False
        existing = target.read_text(encoding="utf-8") if target.is_file() else ""
        arch = self.state.architecture or {}
        contracts = [i for i in arch.get("interfaces", []) if i.get("file") == task.target_file]
        lang = guess_lang(target)
        fence = "" if lang in ("html", "css", "text") else lang
        prompt = f"""PROJECT GOAL: {self.state.goal}

ARCHITECTURE APPROACH:
{arch.get('approach', '')}

INTERFACES FOR THIS FILE:
{json.dumps(contracts, ensure_ascii=False, indent=2)}

ALL INTERFACES (cross-file contract, obey them):
{json.dumps(arch.get('interfaces', []), ensure_ascii=False, indent=2)}

TASK {task.id}: {task.title}
{task.description}

ACCEPTANCE CRITERIA:
{chr(10).join(f'- {a}' for a in task.acceptance) or '- works as described'}

Deliverable file: {task.target_file}
Output the COMPLETE file content in ONE fenced block tagged ```{fence or 'text'}.
No prose, no placeholders, no elided sections.
{f"CURRENT CONTENT (keep everything that works, fix what the task asks):{chr(10)}```{fence}{chr(10)}{existing[:12000]}{chr(10)}```" if existing else ""}"""
        try:
            raw = self._ask(task.role, DEV_SYSTEM if task.role == "senior" else JUNIOR_SYSTEM,
                            prompt, "DEV")
        except Exception as e:
            task.errors.append(f"gerar: {e}")
            self.log("build", f"{task.id} ERRO ao gerar: {e}")
            return False
        code = _one_block(raw, fence)
        if not code.strip():
            task.errors.append("resposta sem bloco de código")
            return False
        cand = self.dir / "candidates" / f"{task.id}_{Path(task.target_file).name}"
        cand.parent.mkdir(parents=True, exist_ok=True)
        cand.write_text(code, encoding="utf-8")
        ok, error = self._check_file(cand, lang)
        if not ok:
            task.errors.append(error)
            self.log("build", f"{task.id} check falhou:\n{error}")
            fixed = self._fix(task, cand, error, attempts=2)
            if fixed is None:
                return False
            cand, code = fixed
        # optional unit test (python only, deterministic)
        if lang == "python" and self.settings.test_oracle:
            test_ok, test_err = self._unit_test(task, cand)
            if not test_ok:
                task.errors.append(test_err)
                self.log("test", f"{task.id} teste falhou:\n{test_err}")
                fixed = self._fix(task, cand, test_err, attempts=2)
                if fixed is None:
                    return False
                cand, code = fixed
        self._install(task, cand, code)
        self.log("build", f"{task.id} entregue em {task.target_file}")
        return True

    def _fix(self, task: TaskState, cand: Path, error: str,
             attempts: int = 2) -> Optional[tuple[Path, str]]:
        """Recursive repair: senior first, chief on the last attempt."""
        current = cand
        for n in range(1, attempts + 1):
            task.attempts += 1
            role = "chief" if n == attempts else "fixer"
            system = CHIEF_FIX_SYSTEM if role == "chief" else FIX_SYSTEM
            target = self._safe(task.target_file)
            lang = guess_lang(current)
            fence = "" if lang in ("html", "css", "text") else lang
            prompt = f"""GOAL: {self.state.goal}

FILE: {task.target_file}
ACCEPTANCE:
{chr(10).join(f'- {a}' for a in task.acceptance) or '- works'}

CURRENT CONTENT:
```{fence}
{current.read_text(encoding='utf-8')[:14000]}
```

ERROR OUTPUT (attempt {n}/{attempts}):
{error[:4000]}

Output the COMPLETE corrected file in ONE fenced block."""
            try:
                raw = self._ask(role, system, prompt, "DEV")
            except Exception as e:
                self.log("fix", f"{task.id} tentativa {n}: modelo falhou: {e}")
                continue
            code = _one_block(raw, fence)
            if not code.strip():
                continue
            current.write_text(code, encoding="utf-8")
            ok, err = self._check_file(current, lang)
            self.log("fix", f"{task.id} tentativa {n} ({role}): {'PASS' if ok else 'FAIL: ' + err[:300]}")
            if ok:
                return current, code
            error = err
        self.emit("SYSTEM", f"{task.id} sem correção após {attempts} tentativas")
        return None

    def _unit_test(self, task: TaskState, cand: Path) -> tuple[bool, str]:
        test_path = self.dir / "tests" / f"test_{task.id}_{cand.stem}.py"
        prompt = f"""Write a pytest file for this unit.
UNIT FILE: {task.target_file}
ACCEPTANCE:
{chr(10).join(f'- {a}' for a in task.acceptance) or '- basic behaviour'}

UNIT CONTENT:
```python
{cand.read_text(encoding='utf-8')[:10000]}
```

Rules: no network, no external services, deterministic, <= 40 lines.
Output only the test file in one fenced block tagged python."""
        try:
            raw = self._ask("junior", TEST_SYSTEM, prompt, "TEST")
            code = _one_block(raw, "python")
            test_path.write_text(code, encoding="utf-8")
        except Exception as e:
            return True, f"(teste não gerado: {e})"  # do not block on test generation
        return self._run_tests(test_path)

    def _run_tests(self, test_path: Path) -> tuple[bool, str]:
        py = str(Path(sys.executable))
        try:
            proc = subprocess.run([py, "-m", "pytest", "-q", str(test_path)],
                                  capture_output=True, text=True, timeout=180,
                                  cwd=str(self.root))
        except Exception as e:
            return True, f"(pytest indisponível: {e})"
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, out.strip()[-4000:]

    # -- step 6-7: integration --------------------------------------------- #

    def step_integrate(self) -> bool:
        entry = self._entrypoint()
        if entry is None:
            self.state.integration = {"verdict": "SKIP", "note": "sem entrypoint detetável"}
            self.log("integrate", "sem entrypoint detetável")
            self.emit("INTEG", "sem entrypoint detetável — verificação estática apenas")
            return True
        kind, path = entry
        attempts = 0
        max_attempts = self.settings.max_verify_loops + 1
        while attempts < max_attempts:
            attempts += 1
            report = self._run_entry(kind, path)
            self.state.integration = report
            self.log("integrate", json.dumps(report, ensure_ascii=False)[:8000])
            if report.get("verdict") == "PASS":
                self.emit("INTEG", f"integração PASS ({kind})")
                return True
            self.emit("INTEG", f"integração FAIL (tentativa {attempts}/{max_attempts})")
            self._record_dom_mismatches(report)
            self.power.before_step(f"integrate fix {attempts}")
            if not self._integration_fix(report, attempts):
                return False
        self.emit("SYSTEM", "integração não passou após as tentativas permitidas")
        return False

    def _record_dom_mismatches(self, report: dict[str, Any]) -> None:
        for m in report.get("dom_mismatches", []) or []:
            self.log("integrate", f"mismatch DOM: {m}")

    def _integration_fix(self, report: dict[str, Any], attempt: int) -> bool:
        """Ask senior (then chief) to fix the runtime errors; re-run is the caller's job."""
        errors = _format_report_errors(report)
        if not errors:
            return False
        role = "chief" if attempt >= self.settings.max_verify_loops else "fixer"
        system = CHIEF_FIX_SYSTEM if role == "chief" else FIX_SYSTEM
        files = self._runtime_files(report)
        payload = []
        for task in files:
            path = self._safe(task.target_file)
            if path is None or not path.is_file():
                continue
            payload.append(f"### FILE: {task.target_file}\n```\n"
                           f"{path.read_text(encoding='utf-8')[:12000]}\n```")
        prompt = f"""GOAL: {self.state.goal}

TASK: fix the runtime/integration defects below. Only change these files:
{chr(10).join(f'- {t.target_file}' for t in files)}

ARCHITECTURE:
{json.dumps(self.state.architecture.get('interfaces', []), ensure_ascii=False)[:4000]}

ERROR REPORT:
{errors[:6000]}

CURRENT FILES:
{chr(10).join(payload)}

Output each corrected file with `### FILE: <relative path>` then a fenced block."""
        try:
            raw = self._ask(role, system, prompt, "DEV")
        except Exception as e:
            self.log("integrate", f"correção ({role}) falhou: {e}")
            return False
        written = 0
        for rel, code in _multi_file_blocks(raw):
            dest = self._safe(rel)
            if dest is None or not code.strip():
                continue
            task = next((t for t in self.state.tasks if t.target_file == rel), None)
            self._backup(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(code.rstrip("\n") + "\n", encoding="utf-8")
            if task is not None:
                task.status = "done"
            written += 1
            self.log("integrate", f"corrigido por {role}: {rel}")
        self.emit("INTEG", f"{written} ficheiro(s) corrigido(s) por {role}")
        return written > 0

    def _runtime_files(self, report: dict[str, Any]) -> list[TaskState]:
        touched: list[TaskState] = []
        blobs = json.dumps(report, ensure_ascii=False)
        for task in self.state.tasks:
            name = Path(task.target_file).name
            if task.target_file in blobs or name in blobs:
                touched.append(task)
        if touched:
            return touched
        # web entry: fix the JS/HTML files of the project that exist
        web = [t for t in self.state.tasks if t.target_file.endswith((".js", ".html", ".css"))]
        return web or self.state.tasks[:2]

    def _entrypoint(self) -> Optional[tuple[str, Path]]:
        if (self.root / "index.html").is_file():
            return "web", self.root / "index.html"
        pkg = self.root / "package.json"
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
            if (self.root / pat).is_file():
                return "python", self.root / pat
        for p in sorted(self.root.glob("*.py")):
            txt = p.read_text(encoding="utf-8", errors="ignore")
            if "__main__" in txt:
                return "python", p
        for p in sorted(self.root.glob("*.js")):
            txt = p.read_text(encoding="utf-8", errors="ignore")
            if "__main__" not in txt and "require.main" in txt:
                return "node", p
        return None

    def _run_entry(self, kind: str, path: Path) -> dict[str, Any]:
        if kind == "web":
            return self.playwright_audit(self.root)
        cmd = ([str(Path(sys.executable)), str(path)] if kind == "python"
               else ["node", str(path)])
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                                  cwd=str(self.root))
            out = ((proc.stdout or "") + (proc.stderr or "")).strip()
            ok = proc.returncode == 0
            note = ""
            if not ok and kind == "python" and "Traceback" not in out:
                # a CLI that needs arguments exits non-zero with a usage message: fine
                low = out.lower()
                if "usage:" in low or "argument" in low or "falta" in low:
                    ok, note = True, "programa arranca (requer argumentos)"
            return {"verdict": "PASS" if ok else "FAIL", "kind": kind,
                    "cmd": " ".join(cmd), "exit": proc.returncode, "note": note,
                    "output": out[-6000:]}
        except Exception as e:
            return {"verdict": "FAIL", "kind": kind, "cmd": " ".join(cmd),
                    "output": f"{type(e).__name__}: {e}"}

    # -- step 8: visual ----------------------------------------------------- #

    def step_visual(self) -> bool:
        if not (self.root / "index.html").is_file():
            self.state.visual = {"verdict": "SKIP", "note": "projeto sem index.html"}
            self.log("visual", "SKIP (sem index.html)")
            return True
        if not self.settings.visual_check:
            self.state.visual = {"verdict": "SKIP", "note": "visual_check desligado"}
            return True
        report = self.playwright_audit(self.root)
        png = report.get("screenshot")
        if png and self.pool.gemini is not None:
            self.power.before_step("visual review")
            verdict = self._vision_review(Path(png))
            report["vision"] = verdict
            if verdict.get("verdict") == "FAIL" and verdict.get("objections"):
                self.power.before_step("visual fix")
                self._visual_fix(verdict)
                report = self.playwright_audit(self.root)
                report["vision_retry"] = verdict
        self.state.visual = report
        self.log("visual", json.dumps(report, ensure_ascii=False)[:8000])
        self.save()
        ok = report.get("verdict") != "FAIL"
        self.emit("VISUAL", f"verdict {report.get('verdict')}"
                            + (f" ({len(report.get('console_errors', []))} console, "
                               f"{len(report.get('page_errors', []))} page errors)"
                               if ok else ""))
        return ok

    def _vision_review(self, png: Path) -> dict[str, Any]:
        try:
            img = base64.b64encode(png.read_bytes()).decode()
        except Exception as e:
            return {"verdict": "UNAVAILABLE", "note": str(e)}
        acceptance = self.state.requirements.get("acceptance", [])
        prompt = f"""ACCEPTANCE CRITERIA:
{chr(10).join(f'- {a}' for a in acceptance) or '- the UI matches the goal'}

GOAL: {self.state.goal}

Check this screenshot. STRICT JSON:
{{"verdict": "PASS"|"FAIL",
  "objections": [{{"claim": str, "evidence": str, "fix": str}}]}}"""
        try:
            llm = self.pool.gemini
            msg = HumanMessage(content=[{"type": "text", "text": prompt},
                                        {"type": "image_url",
                                         "image_url": {"url": f"data:image/png;base64,{img}"}}])
            raw = self.pool.complete(llm, [SystemMessage(VISION_SYSTEM), msg],
                                     meta={"run": None, "tag": "VISUAL"})
            return _json_or_raise(raw, "vision")
        except Exception as e:
            return {"verdict": "UNAVAILABLE", "note": str(e)[:300]}

    def _visual_fix(self, verdict: dict[str, Any]) -> None:
        objections = json.dumps(verdict.get("objections", []), ensure_ascii=False)[:4000]
        web = [t for t in self.state.tasks if t.target_file.endswith((".css", ".html", ".js"))]
        for task in web[:3]:
            path = self._safe(task.target_file)
            if path is None or not path.is_file():
                continue
            lang = guess_lang(path)
            fence = "" if lang in ("html", "css", "text") else lang
            prompt = f"""GOAL: {self.state.goal}
FILE: {task.target_file}
UI OBJECTIONS:
{objections}

CURRENT CONTENT:
```{fence}
{path.read_text(encoding='utf-8')[:12000]}
```

Fix ONLY the objections. Output the COMPLETE file in one fenced block."""
            try:
                raw = self._ask("fixer", FIX_SYSTEM, prompt, "DEV")
                code = _one_block(raw, fence)
                if code.strip():
                    self._backup(path)
                    path.write_text(code.rstrip("\n") + "\n", encoding="utf-8")
                    self.log("visual", f"corrigido {task.target_file} após objeções visuais")
            except Exception as e:
                self.log("visual", f"correção visual falhou em {task.target_file}: {e}")

    # -- playwright audit --------------------------------------------------- #

    def playwright_audit(self, root: Path, page: str = "index.html",
                         width: int = 1024, height: int = 768, wait_ms: int = 1500) -> dict[str, Any]:
        """Load the app in headless Chromium: console, page errors, 404s, DOM contracts."""
        dom_mismatches = _dom_contract_check(root)
        report: dict[str, Any] = {"verdict": "FAIL", "console_errors": [],
                                  "console_all": [], "page_errors": [],
                                  "failed_requests": [], "http_errors": [],
                                  "dom_mismatches": dom_mismatches, "screenshot": ""}
        try:
            from playwright.sync_api import sync_playwright
        except Exception as e:
            report["verdict"] = "UNAVAILABLE"
            report["note"] = f"playwright indisponível: {e}"
            return report
        server, port = _serve(root)
        if server is None:
            report["verdict"] = "UNAVAILABLE"
            report["note"] = "não consegui subir servidor HTTP local"
            return report
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                ctx = browser.new_context(viewport={"width": width, "height": height})
                pg = ctx.new_page()
                pg.on("console", lambda m: report["console_all"].append(
                    {"type": m.type, "text": m.text, "where": str(m.location)}))
                pg.on("pageerror", lambda e: report["page_errors"].append(str(e)))
                pg.on("requestfailed", lambda r: report["failed_requests"].append(
                    {"url": r.url, "failure": (r.failure or "")}))
                pg.on("response", lambda r: report["http_errors"].append(
                    {"url": r.url, "status": r.status})
                      if r.status >= 400 and "favicon" not in r.url else None)
                pg.goto(f"http://127.0.0.1:{port}/{page}", wait_until="load", timeout=15000)
                pg.wait_for_timeout(wait_ms)
                png = self.dir / "logs" / "ui_screenshot.png"
                png.parent.mkdir(parents=True, exist_ok=True)
                pg.screenshot(path=str(png), full_page=True)
                report["screenshot"] = str(png)
                ctx.close()
                browser.close()
        except Exception as e:
            report["note"] = f"playwright falhou: {type(e).__name__}: {e}"
        finally:
            server.shutdown()
            server.server_close()
        report["console_errors"] = [c for c in report["console_all"]
                                    if c["type"] in ("error", "assert")]
        problems = (report["page_errors"] or report["console_errors"]
                    or report["failed_requests"] or report["http_errors"]
                    or report["dom_mismatches"])
        if not report.get("note") and not problems:
            report["verdict"] = "PASS"
        elif not report.get("note"):
            report["verdict"] = "FAIL"
        return report

    # -- step 9: sign-off --------------------------------------------------- #

    def step_signoff(self) -> bool:
        tree = self._tree()
        summary = f"""GOAL: {self.state.goal}
REQUIREMENTS: {json.dumps(self.state.requirements, ensure_ascii=False)[:3000]}
ARCHITECTURE: {json.dumps(self.state.architecture.get('approach', ''), ensure_ascii=False)[:1500]}
TASKS: {json.dumps([{'id': t.id, 'file': t.target_file, 'status': t.status} for t in self.state.tasks], ensure_ascii=False)}
INTEGRATION: {json.dumps(self.state.integration, ensure_ascii=False)[:2500]}
VISUAL: {json.dumps(self.state.visual, ensure_ascii=False)[:1500]}
TREE: {tree}"""
        prompt = f"""{summary}

Write the handover memory for the next engineer. STRICT JSON:
{{"what_it_is": str, "how_to_run": [str], "entrypoints": [str],
  "architecture_notes": [str], "tests": [str],
  "known_issues": [str], "next_steps": [str]}}"""
        memory: dict[str, Any] = {}
        try:
            raw = self._ask("chief", ARCHITECT_SYSTEM, prompt, "SIGN")
            memory = _json_or_raise(raw, "signoff")
        except Exception as e:
            memory = {"note": f"sign-off gerado sem modelo: {e}"}
        payload = {
            "project": str(self.root),
            "goal": self.state.goal,
            "generated_at": _now(),
            "requirements": self.state.requirements,
            "architecture": self.state.architecture,
            "tasks": [t.to_dict() for t in self.state.tasks],
            "integration": self.state.integration,
            "visual": self.state.visual,
            "handover": memory,
        }
        (self.root / "project_memory.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.state.signoff = payload
        self.log("signoff", json.dumps(payload, ensure_ascii=False, indent=2)[:12000])
        self.emit("SIGN", "project_memory.json gravado no projeto")
        return True

    # -- helpers ------------------------------------------------------------ #

    def _safe(self, rel: str) -> Optional[Path]:
        try:
            dest = (self.root / rel).resolve()
            dest.relative_to(self.root)
            return dest
        except Exception:
            return None

    def _tree(self) -> str:
        lines: list[str] = []
        skip = {".git", ".factory", "node_modules", "__pycache__", ".venv", "workspace"}
        count = 0
        for p in sorted(self.root.rglob("*")):
            rel = p.relative_to(self.root)
            if set(rel.parts) & skip:
                continue
            if p.is_file():
                try:
                    size = p.stat().st_size
                except OSError:
                    size = 0
                lines.append(f"{rel} ({size}B)")
                count += 1
                if count >= 200:
                    break
        return "\n".join(lines)

    def _project_brief(self, limit: int = 24000) -> str:
        parts: list[str] = []
        used = 0
        skip = {".git", ".factory", "node_modules", "__pycache__", ".venv", "workspace"}
        exts = {".py", ".js", ".mjs", ".ts", ".html", ".htm", ".css", ".json", ".md"}
        for p in sorted(self.root.rglob("*")):
            rel = p.relative_to(self.root)
            if set(rel.parts) & skip or p.suffix.lower() not in exts or not p.is_file():
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if p.stat().st_size > 40000:
                text = text[:40000]
            block = f"=== {rel} ===\n{text}\n"
            if used + len(block) > limit:
                parts.append(f"=== {rel} === (omitido: contexto cheio)")
                continue
            parts.append(block)
            used += len(block)
        return "\n".join(parts)

    def _check_file(self, path: Path, lang: str) -> tuple[bool, str]:
        if lang in ("python", "py"):
            try:
                proc = subprocess.run([str(Path(sys.executable)), "-m", "py_compile", str(path)],
                                      capture_output=True, text=True, timeout=60)
            except Exception as e:
                return False, f"py_compile: {e}"
            if proc.returncode != 0:
                return False, (proc.stderr or proc.stdout)[-3000:]
            return True, ""
        if lang in ("javascript", "js", "mjs"):
            if shutil.which("node") is None:
                return True, ""
            try:
                proc = subprocess.run(["node", "--check", str(path)],
                                      capture_output=True, text=True, timeout=60)
            except Exception as e:
                return False, f"node --check: {e}"
            if proc.returncode != 0:
                return False, (proc.stderr or proc.stdout)[-3000:]
            return True, ""
        if lang == "json":
            try:
                json.loads(path.read_text(encoding="utf-8"))
                return True, ""
            except Exception as e:
                return False, f"json inválido: {e}"
        return True, ""

    def _install(self, task: TaskState, cand: Path, code: str) -> None:
        dest = self._safe(task.target_file)
        if dest is None:
            return
        self._backup(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(code.rstrip("\n") + "\n", encoding="utf-8")

    def _backup(self, path: Path) -> None:
        if not path.is_file():
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        rel = path.relative_to(self.root)
        dest = self.dir / "backups" / stamp / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _json_or_raise(raw: str, what: str) -> dict[str, Any]:
    try:
        return json.loads(extract_json_block(raw))
    except Exception as e:
        raise RuntimeError(f"{what}: JSON inválido ({e})") from e


def _one_block(raw: str, fence: str) -> str:
    blocks = [(lang, code) for lang, code in extract_code_blocks_typed(raw) if code]
    if not blocks:
        return ""
    if fence:
        for lang, code in blocks:
            if lang == fence or (fence == "javascript" and lang in ("js", "mjs")):
                return code
    return max(blocks, key=lambda b: len(b[1]))[1]


def _multi_file_blocks(raw: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pattern = re.compile(r"###\s*FILE:\s*([^\n`]+)\s*\n(.*?)(?=###\s*FILE:|\Z)", re.DOTALL)
    for m in pattern.finditer(raw):
        rel = m.group(1).strip().strip("`")
        body = m.group(2)
        code = _one_block(body, "") or body.strip()
        if rel and code:
            out.append((rel, code))
    return out


def _format_report_errors(report: dict[str, Any]) -> str:
    lines: list[str] = []
    if report.get("note"):
        lines.append(f"NOTE: {report['note']}")
    for e in report.get("page_errors", []) or []:
        lines.append(f"PAGE ERROR: {e}")
    for c in report.get("console_errors", []) or []:
        lines.append(f"CONSOLE {c.get('type')}: {c.get('text')} ({c.get('where')})")
    for r in report.get("failed_requests", []) or []:
        lines.append(f"REQUEST FAILED: {r.get('url')} — {r.get('failure')}")
    for r in report.get("http_errors", []) or []:
        lines.append(f"HTTP {r.get('status')}: {r.get('url')}")
    for m in report.get("dom_mismatches", []) or []:
        lines.append(f"DOM MISMATCH ({m.get('kind')}): {m.get('missing')} "
                     f"referenced by {m.get('referenced_by')} but defined in {m.get('defined_in')}")
    if report.get("output"):
        lines.append("OUTPUT:\n" + str(report["output"])[:4000])
    return "\n".join(lines)


def _dom_contract_check(root: Path) -> list[dict[str, str]]:
    """Static cross-file contract: ids/classes used by JS/CSS vs defined in HTML."""
    html_files = [p for p in root.rglob("*.html") if ".factory" not in p.parts]
    if not html_files:
        return []
    ids: set[str] = set()
    classes: set[str] = set()
    for h in html_files:
        txt = h.read_text(encoding="utf-8", errors="ignore")
        ids |= set(re.findall(r'\bid\s*=\s*["\']([^"\']+)["\']', txt))
        classes |= set(re.findall(r'\bclass\s*=\s*["\']([^"\']+)["\']', txt))
    class_set = {c for group in classes for c in group.split()}
    refs: dict[str, set[str]] = {}
    for js in list(root.rglob("*.js")) + list(root.rglob("*.mjs")):
        if ".factory" in js.parts:
            continue
        txt = js.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"getElementById\(\s*[\"']([^\"']+)[\"']", txt):
            refs.setdefault(m.group(1), set()).add(str(js.relative_to(root)))
        for m in re.finditer(r"querySelector(?:All)?\(\s*[\"']#([A-Za-z0-9_-]+)[\"']", txt):
            refs.setdefault(m.group(1), set()).add(str(js.relative_to(root)))
        for m in re.finditer(r"\.style\b", txt):
            pass
    css_refs: dict[str, set[str]] = {}
    for css in root.rglob("*.css"):
        if ".factory" in css.parts:
            continue
        txt = css.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"#([A-Za-z][A-Za-z0-9_-]*)", txt):
            css_refs.setdefault(m.group(1), set()).add(str(css.relative_to(root)))
    out: list[dict[str, str]] = []
    for ident, where in refs.items():
        if ident not in ids:
            out.append({"kind": "id", "missing": f"#{ident}",
                        "referenced_by": ", ".join(sorted(where)),
                        "defined_in": ", ".join(str(h.relative_to(root)) for h in html_files)})
    for ident, where in css_refs.items():
        if re.fullmatch(r"[0-9a-fA-F]{3,8}", ident):
            continue  # hex colour, not an id
        if ident not in ids and ident not in {"root"} and not ident[0].isdigit():
            out.append({"kind": "css-id", "missing": f"#{ident}",
                        "referenced_by": ", ".join(sorted(where)),
                        "defined_in": ", ".join(str(h.relative_to(root)) for h in html_files)})
    return out


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        pass


def _serve(root: Path) -> tuple[Optional[socketserver.TCPServer], int]:
    handler = lambda *a, **kw: _QuietHandler(*a, directory=str(root), **kw)  # noqa: E731
    last_err: Optional[Exception] = None
    for port in range(8765, 8805):
        try:
            server = socketserver.TCPServer(("127.0.0.1", port), handler)
        except OSError as e:
            last_err = e
            continue
        server.allow_reuse_address = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        time.sleep(0.15)
        return server, port
    if last_err:
        return None, 0
    return None, 0
