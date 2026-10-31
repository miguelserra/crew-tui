#!/usr/bin/env python3
"""
Dynamic Team-Based Multi-Agent Pool System (native async orchestrator).

An agent-pool framework wrapped in a Textual TUI. It replaces CrewAI's
hierarchical "role-play" engine with a deterministic state machine:

    SPEC  ->  (human gate)  ->  BUILD  ->  VERIFY  ->  DONE

Design principles (from "Stop Prompting Claude. Use Karpathy's Method Instead"):

* SPEC     - a Pydantic "SpecContract" is frozen before any code is generated.
             The manager co-designs it with the user (goal, non-negotiables,
             measurable acceptance criteria, small compartmentalized tasks).
* VERIFY   - two independent levers:
               1. an external *oracle* (py_compile / ruff / pytest exit codes),
               2. a cross-model *critic* (a different model family that must
                  produce falsifiable objections, never praise).
             A feedback loop (build -> verify -> fix) is mandatory.
* ENVIRONMENT - persistent config via .env / env vars, a reusable LLMPool with
             deterministic context guards, and a thread-safe TUI callback layer.

This script is PORTABLE: everything is driven by environment variables, the
local llama.cpp worker is auto-detected (with automatic cloud fallback), and
all keys are optional (roles degrade gracefully when a key/endpoint is absent).

Run:  python main.py
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
from decimal import Decimal, InvalidOperation
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage, SystemMessage

from rich.table import Table
from rich.text import Text

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, RichLog, Select, Static

try:
    from langchain_openai import ChatOpenAI
    from openai import RateLimitError
except Exception:  # pragma: no cover - env without the cloud stack
    ChatOpenAI = None  # type: ignore
    RateLimitError = Exception

try:
    from langchain_google_genai import ChatGoogleGenerativeAI
except Exception:  # pragma: no cover - env without the google stack
    ChatGoogleGenerativeAI = None  # type: ignore

from power import make_power
from budget import BudgetSuspended, bills_per_token
from attempts import AttemptCollector, AttemptEvent
from registry import (
    BILLING_PAYG, BILLING_UNKNOWN as MB_UNKNOWN, CostEstimate, ModelIdentity, ModelRef,
    ModelRegistry, RegistryError, normalize_billing, ref_from_handle,
)
from publish import PublishError, PublishManager, RecoveryRequired
from calls import (CallLedger, CallLedgerError, clear_call_context,
                   last_call_id, last_served_ref)
from workspace import IgnoreRules, default_workspace
from engine import (
    ENGINE_LEGACY, ENGINE_UNIFIED, ENGINE_VERSION, EngineError, EngineFacade,
    FactoryAdapter, UnknownBilling, WorkflowEngine, iter_model_refs, migrate_engine,
    migrate_state, new_unified_state, open_execution, resolve_engine,
)
from store import StateManager
from state import (ProjectState, StateCorruptError, StateError, VerificationRecord,
                   from_legacy_factory, new_id)
import verify as verify_policy
from verify import UNVERIFIED, FAIL, NA, PASS as VERIFY_PASS
from router import (
    CircuitBreaker, GrantStore, PaidGrant, RouteRequest, RouteResult, RouteChain,
    Router, RouterExhausted, UnknownModelError, classify_error, identity_key,
)


# --------------------------------------------------------------------------- #
#  Config / environment
# --------------------------------------------------------------------------- #

def _app_config_dir() -> Path:
    """Cross-platform per-user config dir (Linux/macOS: ~/.config, Windows: %APPDATA%)."""
    if platform.system() == "Windows":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "crew-tui"


def _env_bool(env: dict, key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _read_env_file(path: str | os.PathLike) -> dict[str, str]:
    """Parse a KEY=VALUE .env file (ignores comments/blank lines). No side effects."""
    p = Path(path)
    if not p.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _merge_env_files() -> None:
    """Merge secrets/config into os.environ (cross-platform, portable).

    Precedence (highest wins):
      1. variables already set in the real environment;
      2. project `.env` (current working directory);
      3. per-user `~/.config/crew-tui/.env`  (Windows: `%APPDATA%\\crew-tui\\.env`);
      4. built-in defaults (applied by Settings itself).

    Keys are read from user-level and project files only; never written to disk.
    """
    preset = set(os.environ.keys())  # real environment always wins
    merged = _read_env_file(_app_config_dir() / ".env")
    merged.update(_read_env_file(".env"))  # project overrides user-level
    for key, value in merged.items():
        if key not in preset:
            os.environ[key] = value


@dataclass
class Settings:
    """Central config. Every value is overridable by env var / .env file."""

    # OpenCode cloud API (OpenAI-compatible).
    opencode_api_key: str = ""
    opencode_base_url: str = "https://opencode.ai/zen/go/v1"
    # "last" = subscription models only as a last resort; "primary" = old
    # behaviour; "off" = never call them (disables the key entirely).
    opencode_mode: str = "last"

    # OpenRouter cloud API (OpenAI-compatible, free models available).
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "nvidia/nemotron-3-ultra"  # free tier model
    openrouter_free_model: str = "nvidia/nemotron-3-ultra"  # free tier fallback

    # Gemini (native).
    gemini_api_key: str = ""

    # Direct DeepSeek API (pay-as-you-go, NOT the OpenCode Go subscription).
    # Empty key disables the whole peak/off-peak policy (behaviour unchanged).
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-pro"
    # Budget breaker: pay-as-you-go models (direct DeepSeek, non-free
    # OpenRouter) are never called unless explicitly authorized here or via
    # the interactive override modal.
    allow_paid: bool = False
    allow_unknown: bool = False  # authorize models whose billing cannot be proved
    allow_unverified: bool = False  # explicit delivery exception for UNVERIFIED
    workflow_profile: str = "auto"  # auto|web|library|script|review
    payg_models: str = ""      # extra comma-separated ids that bill per token
    # Cost policy: during a peak UTC window, skip pay-as-you-go DeepSeek and
    # fall back to the already-paid Gemini Plus subscription.
    peak_policy: bool = True
    peak_windows: str = "01:00-04:00,06:00-10:00"   # comma-separated HH:MM-HH:MM (UTC)
    peak_weekdays_only: bool = True                 # Sat/Sun never peak

    # Power policy: hold an idle inhibitor while agents work, and pause the run
    # at peak (RTC alarm + suspend, resume off-peak). Needs scripts/crew-sleep.
    keep_awake: bool = True
    sleep_offpeak: bool = False                     # off until the helper is installed
    offpeak_idle_min: int = 5                       # suspend only after this much idle
    sleep_if_active: bool = False                   # else stay awake, stay paused
    sleep_helper: str = "/usr/local/sbin/crew-sleep"
    local_service: str = "qwen36"                   # stopped/started around suspend
    qwen_restart_on_wake: bool = True
    unattended: bool = False                        # runtime flag (--unattended)

    # Senior dev / fixer: free "stealth" model first, then paid fallbacks.
    # The free zen endpoint currently answers 429 for API clients, so the pool
    # probes once at startup and only uses it when it is actually reachable.
    senior_model: str = "big-pickle"
    senior_base_url: str = "https://opencode.ai/zen/v1"
    senior_probe: bool = True
    senior_force: bool = False      # include the model even if the probe fails

    # Local llama.cpp worker (auto-detected; optional).
    local_llm_url: str = "http://127.0.0.1:8081/v1"
    local_model_name: str = "qwen3.6-35b-a3b"
    # Optional command to start the local server on demand (empty = disabled).
    local_start_cmd: str = ""

    # Cloud model used when the local worker is DOWN (mobility fallback).
    beam_free_model: str = "beam-free"

    # Cloud model mapping (per role).
    fieldmarshal_model: str = "deepseek-v4-pro"
    fallback_model: str = "kimi-k3.1-pro"      # 429 fallback for the manager
    architect_model: str = "kimi-k3.1-pro"
    fast_cloud_model: str = "deepseek-v4.1-flash"
    gemini_model: str = "gemini-3.5-flash"
    gemini_pro_model: str = "gemini-3.1-pro-preview"
    gemini_max_output_tokens: int = 8192
    review_model: str = "gemini-4-argon"

    # UI streaming: "progress" (chars/s, default) | "full" (whole code) | "off".
    stream_mode: str = "progress"

    # Visual verification: headless screenshot + optional vision-model judgement.
    visual_check: bool = True

    # Local test oracle: Qwen writes a self-contained test, we run it locally.
    test_oracle: bool = True

    # Reasoning / thinking control (per role).
    reasoning_effort: str = "medium"           # low|medium|high (roles with thinking ON)
    think_fieldmarshal: bool = True
    think_architect: bool = True
    think_fast_cloud: bool = False
    think_worker: bool = False
    think_critic: bool = False
    think_gemini: bool = True

    # Gemini rate-limit fallback ("too busy" -> secondary model).
    gemini_fallback_model: str = "gemini-3.8-flash"

    # Context protection (OOM safeguard).
    local_safe_tokens: int = 18_000            # prompt budget; leaves room to generate
    large_context_tokens: int = 32_000         # "large context" delegation bar
    local_task_tokens: int = 1_500             # max task size the weak local model may generate
    local_summarize_chars: int = 20_000       # > this project size -> Qwen summarises locally
    hard_task_tokens: int = 800              # >= this complexity -> DeepSeek Pro directly

    # Verification.
    workspace_dir: str = "workspace"
    max_verify_loops: int = 3

    # Telemetry / portability (all relative to the project dir).
    log_dir: str = "logs"
    log_max_bytes: int = 10_000_000
    spec_dir: str = "specs"

    # Project context ("trabalha na pasta X").
    project_dir: str = "~/projetos_opencode"
    scan_max_files: int = 150
    scan_max_chars_per_file: int = 12_000
    scan_total_chars: int = 80_000

    @classmethod
    def from_env(cls) -> "Settings":
        _merge_env_files()
        env = os.environ
        return cls(
            opencode_api_key=env.get("OPENCODE_API_KEY", ""),
            opencode_base_url=env.get("OPENCODE_BASE_URL", "https://opencode.ai/zen/go/v1"),
            opencode_mode=env.get("OPENCODE_MODE", "last").strip().lower(),
            openrouter_api_key=env.get("OPENROUTER_API_KEY", ""),
            openrouter_base_url=env.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            openrouter_model=env.get("OPENROUTER_MODEL", "nvidia/nemotron-3-ultra"),
            openrouter_free_model=env.get("OPENROUTER_FREE_MODEL", "nvidia/nemotron-3-ultra"),
            gemini_api_key=env.get("GEMINI_API_KEY", ""),
            deepseek_api_key=env.get("DEEPSEEK_API_KEY", ""),
            deepseek_base_url=env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            deepseek_model=env.get("DEEPSEEK_MODEL", "deepseek-v4-pro"),
            allow_paid=_env_bool(env, "ALLOW_PAID", False),
            allow_unknown=_env_bool(env, "ALLOW_UNKNOWN", False),
            allow_unverified=_env_bool(env, "ALLOW_UNVERIFIED", False),
            workflow_profile=env.get("WORKFLOW_PROFILE", "auto"),
            payg_models=env.get("PAYG_MODELS", ""),
            peak_policy=_env_bool(env, "PEAK_POLICY", True),
            peak_windows=env.get("PEAK_WINDOWS_UTC", "01:00-04:00,06:00-10:00"),
            peak_weekdays_only=_env_bool(env, "PEAK_WEEKDAYS_ONLY", True),
            keep_awake=_env_bool(env, "KEEP_AWAKE", True),
            sleep_offpeak=_env_bool(env, "SLEEP_OFFPEAK", False),
            offpeak_idle_min=int(env.get("OFFPEAK_IDLE_MIN", "5")),
            sleep_if_active=_env_bool(env, "SLEEP_IF_ACTIVE", False),
            sleep_helper=env.get("SLEEP_HELPER", "/usr/local/sbin/crew-sleep"),
            local_service=env.get("LOCAL_SERVICE", "qwen36"),
            qwen_restart_on_wake=_env_bool(env, "QWEN_RESTART_ON_WAKE", True),
            senior_model=env.get("SENIOR_MODEL", "big-pickle"),
            senior_base_url=env.get("SENIOR_BASE_URL", "https://opencode.ai/zen/v1"),
            senior_probe=_env_bool(env, "SENIOR_PROBE", True),
            senior_force=_env_bool(env, "SENIOR_FORCE", False),
            local_llm_url=env.get("LOCAL_LLM_URL", "http://127.0.0.1:8081/v1"),
            local_model_name=env.get("LOCAL_MODEL_NAME", "qwen3.6-35b-a3b"),
            local_start_cmd=env.get("LOCAL_START_CMD", ""),
            beam_free_model=env.get("BEAM_FREE_MODEL", "beam-free"),
            fieldmarshal_model=env.get("FIELD_MARSHAL_MODEL", "deepseek-v4-pro"),
            fallback_model=env.get("FALLBACK_MODEL", "kimi-k3.1-pro"),
            architect_model=env.get("ARCHITECT_MODEL", "kimi-k3.1-pro"),
            fast_cloud_model=env.get("FAST_CLOUD_MODEL", "deepseek-v4.1-flash"),
            gemini_model=env.get("GEMINI_MODEL", "gemini-3.5-flash"),
            gemini_pro_model=env.get("GEMINI_PRO_MODEL", "gemini-3.1-pro-preview"),
            gemini_max_output_tokens=int(env.get("GEMINI_MAX_OUTPUT_TOKENS", "8192")),
            review_model=env.get("REVIEW_MODEL", "gemini-4-argon"),
            reasoning_effort=env.get("REASONING_EFFORT", "medium"),
            stream_mode=env.get("STREAM_MODE", "progress"),
            visual_check=_env_bool(env, "VISUAL_CHECK", True),
            test_oracle=_env_bool(env, "TEST_ORACLE", True),
            think_fieldmarshal=_env_bool(env, "THINK_FIELDMARSHAL", True),
            think_architect=_env_bool(env, "THINK_ARCHITECT", True),
            think_fast_cloud=_env_bool(env, "THINK_FAST_CLOUD", False),
            think_worker=_env_bool(env, "THINK_WORKER", False),
            think_critic=_env_bool(env, "THINK_CRITIC", False),
            think_gemini=_env_bool(env, "THINK_GEMINI", True),
            gemini_fallback_model=env.get("GEMINI_FALLBACK_MODEL", "gemini-3.8-flash"),
            project_dir=env.get("PROJECT_DIR", "~/projetos_opencode"),
            local_safe_tokens=int(env.get("LOCAL_SAFE_TOKENS", "18000")),
            large_context_tokens=int(env.get("LARGE_CONTEXT_TOKENS", "32000")),
            local_task_tokens=int(env.get("LOCAL_TASK_TOKENS", "1500")),
            local_summarize_chars=int(env.get("LOCAL_SUMMARIZE_CHARS", "20000")),
            hard_task_tokens=int(env.get("HARD_TASK_TOKENS", "800")),
            workspace_dir=env.get("WORKSPACE_DIR", "workspace"),
            max_verify_loops=int(env.get("MAX_VERIFY_LOOPS", "3")),
            log_dir=env.get("LOG_DIR", "logs"),
            log_max_bytes=int(env.get("LOG_MAX_BYTES", "10000000")),
            spec_dir=env.get("SPEC_DIR", "specs"),
        )


# --------------------------------------------------------------------------- #
#  Shared helpers
# --------------------------------------------------------------------------- #

COLORS: dict[str, str] = {
    "FIELDMARSHAL": "red",
    "ARCHITECT": "blue",
    "PLAN": "bright_blue",
    "GEMINI": "purple",
    "WORKER": "green",
    "ESCALATE": "bright_red",
    "CRITIC": "yellow",
    "REVIEW": "bright_magenta",
    "ORACLE": "cyan",
    "CHAT": "bright_cyan",
    "RESUMO": "bright_white",
    "SYSTEM": "grey70",
    "ANALYST": "bright_blue",
    "SCAFFOLD": "green3",
    "FACTORY": "magenta",
    "CREW": "magenta",
    "DEV": "green",
    "TEST": "cyan",
    "INTEG": "orange3",
    "VISUAL": "bright_yellow",
    "SIGN": "bright_white",
    "CHIEF": "red3",
}

GO_KEYWORDS = {"go", "vai", "avança", "avanca", "executa", "começa", "comeca",
               "start", "build", "corre", "run", "força", "forca"}
APPROVE_KEYWORDS = GO_KEYWORDS | {"y", "yes", "ok", "sim", "s", "aprovar", "approve", "aceitar"}

# Read-only review intent ("revê o projeto", "audita", "review", "analisa").
REVIEW_RE = re.compile(r"(?i)\b(rev[eê]|rever|revis[ãa]o|revis[ae]r?|analis[ae]r?|audit[ae]r?|"
                       r"review|audit)\b")

# Billing classes are the only dashboard tier dimension. Costs/prices come
# from the Model Registry, never from the model display name.
TIER_LABEL = {
    "local": "local",
    "free_quota": "free quota",
    "subscription": "subscrição",
    "payg": "payg",
    "unknown": "desconhecido",
}
TIER_STYLE = {
    "local": "green",
    "free_quota": "cyan",
    "subscription": "magenta",
    "payg": "yellow",
    "unknown": "red",
}
LEGACY_TIER = {"gemini": "subscription", "free": "free_quota", "paid": "payg"}


def _event_billing(event: dict[str, Any]) -> str:
    raw = event.get("billing") or event.get("tier") or "unknown"
    raw = LEGACY_TIER.get(str(raw), str(raw))
    return normalize_billing(raw)


def _error_kind(error: Exception) -> str:
    """Short, dashboard-friendly classification of an LLM failure."""
    text = f"{type(error).__name__}: {error}".lower()
    if "429" in text or "rate limit" in text or "quota" in text or "usage limit" in text:
        return "429/quota"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "connection" in text or "unreachable" in text or "refused" in text:
        return "offline"
    if "401" in text or "403" in text or "api key" in text or "unauthorized" in text:
        return "auth"
    if "400" in text:
        return "bad-request"
    if "503" in text or "500" in text or "unavailable" in text:
        return "provider-5xx"
    return type(error).__name__


def tagged(tag: str, text: str, *, color: Optional[str] = None, bold: bool = False) -> Text:
    """Build a Rich `Text` renderable with a colour-coded, bracket-safe prefix."""
    style = color or COLORS.get(tag, "white")
    if bold:
        style = f"{style} bold"
    out = Text()
    out.append(f"[{tag}]", style=style)
    out.append(f" {text}")
    return out


def estimate_tokens(text: str) -> int:
    """Deliberately pessimistic token estimate (code/HTML tokenise ~3 chars/token)."""
    if not text:
        return 0
    return max(len(text) // 3, len(text.split()))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:16]


def content_digest(text: str) -> dict[str, Any]:
    """Compact metadata for a payload (never stores the payload itself)."""
    return {"sha": sha256_hex(text), "chars": len(text), "tokens": estimate_tokens(text)}


def extract_json_block(text: str) -> str:
    """Best-effort extraction of a JSON object/array from LLM output."""
    fence = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start == -1:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
    raise ValueError("no JSON block found")


def extract_code_blocks_typed(text: str) -> list[tuple[str, str]]:
    """Pull fenced `(lang, code)` blocks; tolerate an unclosed final fence."""
    out: list[tuple[str, str]] = []
    for m in re.finditer(r"```([a-zA-Z0-9_+-]*)[ \t]*\r?\n(.*?)(?:```|\Z)", text, re.DOTALL):
        lang = m.group(1).strip().lower()
        body = m.group(2)
        if body.strip():
            out.append((lang, body.strip("\n")))
    if out:
        return out
    return [("", text.strip("\n"))]


def extract_code_blocks(text: str) -> list[str]:
    """Pull fenced code blocks; fall back to the whole text if none."""
    return [code for _lang, code in extract_code_blocks_typed(text) if code]


LANG_SUFFIX = {
    "python": ".py", "py": ".py", "python3": ".py",
    "js": ".js", "javascript": ".js", "node": ".js", "mjs": ".mjs",
    "ts": ".ts", "typescript": ".ts", "jsx": ".jsx", "tsx": ".tsx",
    "html": ".html", "css": ".css", "json": ".json", "md": ".md",
    "markdown": ".md", "sh": ".sh", "bash": ".sh", "sql": ".sql",
}


def guess_lang(path: Path | str) -> str:
    suffix = Path(path).suffix.lower().lstrip(".")
    return {"py": "python", "js": "javascript", "mjs": "javascript",
            "md": "markdown", "htm": "html"}.get(suffix, suffix)


# --------------------------------------------------------------------------- #
#  Callback layer (worker threads -> Textual event loop)
# --------------------------------------------------------------------------- #

class TUIStreamHandler(BaseCallbackHandler):
    """LangChain callback that streams agent output into the Textual RichLog.

    Tokens are buffered and flushed line-by-line through `sink`, which is
    expected to be `app.call_from_thread(...)` bound by the App. This keeps the
    async event loop untouched (no print() calls).
    """

    def __init__(self, tag: str, sink: Callable[[Text], None]):
        super().__init__()
        self.tag = tag
        self._sink = sink
        self._buf = ""

    def _emit(self, text: str, *, bold: bool = False) -> None:
        if not text:
            return
        try:
            self._sink(tagged(self.tag, text, bold=bold))
        except Exception:
            pass

    def _flush(self, final: bool = False) -> None:
        while "\n" in self._buf:
            head, self._buf = self._buf.split("\n", 1)
            self._emit(head)
        if final and self._buf:
            self._emit(self._buf)
            self._buf = ""

    def on_llm_start(self, serialized, prompts, **kwargs) -> None:
        name = serialized.get("name") if isinstance(serialized, dict) else None
        self._emit(f"▸ {name or self.tag} started", bold=True)

    def on_llm_new_token(self, token, **kwargs) -> None:
        if isinstance(token, list):
            parts = []
            for t in token:
                if isinstance(t, str):
                    parts.append(t)
                elif isinstance(t, dict):
                    parts.append(t.get("text") or "")
            text = "".join(parts)
        else:
            text = token or ""
        if not text:
            return
        self._buf += text
        self._flush(final=False)

    def on_llm_end(self, response, **kwargs) -> None:
        self._flush(final=True)

    def on_llm_error(self, error, **kwargs) -> None:
        self._flush(final=True)
        self._emit(f"⚠ {error}", bold=True)


_ATTEMPT_CTX: contextvars.ContextVar[Optional[AttemptCollector]] = contextvars.ContextVar(
    "crew_attempt", default=None)


def _served_model_id(response: Any) -> str:
    out = getattr(response, "llm_output", None) or {}
    if isinstance(out, dict):
        for key in ("model_name", "model"):
            if out.get(key):
                return str(out[key])
    gens = getattr(response, "generations", None) or []
    try:
        meta = gens[0][0].message.response_metadata or {}
        return str(meta.get("model_name") or meta.get("model") or "")
    except Exception:
        return ""


class AttemptHandler(BaseCallbackHandler):
    """Records every candidate call inside a fallback chain."""

    def __init__(self, ref: ModelRef, registry: Optional[ModelRegistry] = None):
        super().__init__()
        self.ref = ref
        self.registry = registry
        self._ids: dict[Any, str] = {}

    def on_llm_start(self, serialized, prompts, run_id=None, **kwargs) -> None:
        col = _ATTEMPT_CTX.get()
        if col is None or run_id is None:
            return
        self._ids[run_id] = col.start(self.ref)

    def on_llm_end(self, response, run_id=None, **kwargs) -> None:
        col = _ATTEMPT_CTX.get()
        if col is None or run_id is None:
            return
        attempt_id = self._ids.pop(run_id, None)
        if attempt_id:
            served = _served_model_id(response)
            served_ref = (self.registry.ref_served(self.ref, served)
                          if (self.registry is not None and served) else None)
            col.finish(attempt_id, "ok", served_id=served, served_ref=served_ref)

    def on_llm_error(self, error, run_id=None, **kwargs) -> None:
        col = _ATTEMPT_CTX.get()
        if col is None or run_id is None:
            return
        attempt_id = self._ids.pop(run_id, None)
        if attempt_id:
            col.finish(attempt_id, _error_kind(error), error=str(error))


def _attach_identity(llm: Any, ref: ModelRef) -> Any:
    """Attach provider/endpoint/profile metadata without changing routing."""
    if llm is not None:
        for key, value in (
            ("_model_ref", ref), ("_provider", ref.provider),
            ("_endpoint", ref.endpoint), ("_credential_profile", ref.credential_profile),
            ("_billing", ref.billing),
        ):
            try:
                object.__setattr__(llm, key, value)
            except Exception:
                pass
    return llm


class ProgressHandler(BaseCallbackHandler):
    """Cheap live progress instead of streaming every token of code.

    Reports `(tag, chars)` at most every `interval` seconds, so the UI stays
    responsive even when a local model dribbles ~5000 tokens/4 minutes.
    """

    def __init__(self, tag: str, sink: Callable[[str, int], None], interval: float = 0.5):
        super().__init__()
        self.tag = tag
        self._sink = sink
        self._interval = interval
        self._chars = 0
        self._last = 0.0
        self._t0 = time.monotonic()

    def _report(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last) < self._interval:
            return
        self._last = now
        try:
            self._sink(self.tag, self._chars)
        except Exception:
            pass

    def on_llm_start(self, *args, **kwargs) -> None:
        self._chars = 0
        self._t0 = time.monotonic()
        self._report(force=True)

    def on_llm_new_token(self, token, **kwargs) -> None:
        if isinstance(token, str):
            self._chars += len(token)
        elif isinstance(token, list):
            self._chars += sum(len(t) if isinstance(t, str) else len(str(t)) for t in token)
        else:
            self._chars += 1
        self._report()

    def on_llm_end(self, *args, **kwargs) -> None:
        self._report(force=True)

    def on_llm_error(self, error, **kwargs) -> None:
        self._report(force=True)


# --------------------------------------------------------------------------- #
#  LLMPool  (dual API routing + discovery + fallbacks + guards)
# --------------------------------------------------------------------------- #

class LLMPool:
    """Centralized model pool. Constructs LangChain models, discovers active
    endpoints on startup and exposes role-specific handles."""

    def __init__(self, settings: Settings, sink_factory: Callable[[str], Callable[[Text], None]],
                 telemetry: Optional["Telemetry"] = None,
                 progress_factory: Optional[Callable[[str], Callable[[str, int], None]]] = None):
        self.settings = settings
        self._sink_factory = sink_factory
        self._progress_factory = progress_factory
        self.telemetry = telemetry
        # Budget breaker state: pay-as-you-go models require explicit consent.
        self.allow_paid = bool(settings.allow_paid)
        self.on_budget: Optional[Callable[[str, list[str]], Optional[str]]] = None
        self.budget_events: list[dict[str, Any]] = []
        self.dispatch_guard: Optional[Callable[[list[ModelRef]], None]] = None
        self.attempt_sink: Optional[Callable[[AttemptEvent], None]] = None
        self.allow_unknown = bool(settings.allow_unknown)
        try:
            self.registry = ModelRegistry.load_default()
            self.registry_error = ""
        except Exception as exc:
            self.registry = ModelRegistry()
            self.registry_error = str(exc)
        self.openrouter_pricing: dict[str, dict[str, Any]] = {}
        self.probes: dict[tuple[str, str], bool] = {}
        self._handle_cache: dict[tuple[Any, ...], Any] = {}
        self.last_attempt_model: dict[str, ModelRef] = {}
        self.router = Router(
            self.registry, self._handle_for_identity,
            breaker=CircuitBreaker(Path(settings.log_dir) / "router_state.json"),
            grants=GrantStore(allow_paid=bool(settings.allow_paid)),
            availability=self._identity_available,
            request_grant=self._request_paid_grant,
            allow_unknown=self.allow_unknown,
        )

        self.cloud_enabled = bool(settings.opencode_api_key) and settings.opencode_mode != "off"
        self.openrouter_enabled = bool(settings.openrouter_api_key)
        self.gemini_enabled = bool(settings.gemini_api_key)
        self.deepseek_enabled = bool(settings.deepseek_api_key)
        self.local_up = False
        self.cloud_models: set[str] = set()
        self._session_id = f"ses_{uuid4().hex[:24]}"
        self._peak_at_build = False

        # single-slot llama.cpp: serialize all local-backed calls.
        self._local_lock = threading.Lock()

        # role handles (None = disabled)
        self.manager: Any = None       # fieldmarshal (with 429 fallback)
        self.architect: Any = None
        self.fast_cloud: Any = None
        self.gemini: Any = None
        self.worker: Any = None        # local worker OR beam-free fallback
        self.deepseek: Any = None      # direct DeepSeek API (pay-as-you-go)
        self.deepseek_offpeak: Any = None  # DeepSeek handle, only outside peak UTC
        self.senior_dev: Any = None    # big-pickle -> Gemini -> DeepSeek (off-peak)
        self.big_pickle_up: Optional[bool] = None
        self.critic_local: Any = None
        self.critic_cloud: Any = None
        self.reviewer: Any = None
        self.critic_argon: Any = None
        self.critic_openrouter: Any = None

        self._build()

    # -- construction ------------------------------------------------------- #

    def _sink(self, tag: str) -> Callable[[Text], None]:
        return self._sink_factory(tag)

    def _handlers(self, tag: str, ref: Optional[ModelRef] = None) -> list[Any]:
        """Full-token streaming, compact progress, and per-candidate attempts."""
        mode = (self.settings.stream_mode or "progress").lower()
        handlers: list[Any] = []
        if mode == "full":
            handlers.append(TUIStreamHandler(tag, self._sink(tag)))
        elif mode == "progress" and self._progress_factory is not None:
            handlers.append(ProgressHandler(tag, self._progress_factory(tag)))
        if ref is not None:
            handlers.append(AttemptHandler(ref, self.registry))
        return handlers

    def _handle_for_identity(self, identity: ModelIdentity, tag: str,
                             max_tokens: int = 8192, thinking: bool = False) -> Any:
        key = (identity.key, tag, max_tokens, thinking)
        if key in self._handle_cache:
            return self._handle_cache[key]
        profile_name = identity.credential_profile
        if profile_name == "gemini-plus":
            llm = self._gemini(identity.model_id, thinking=thinking, tag=tag)
        elif profile_name == "opencode-go":
            llm = self._cloud(identity.model_id, tag, thinking=thinking,
                              max_tokens=max_tokens)
        elif profile_name == "opencode-free":
            llm = self._free_cloud(identity.model_id, tag, max_tokens=max_tokens)
        elif profile_name == "openrouter":
            llm = self._openrouter(identity.model_id, tag, thinking=thinking,
                                   max_tokens=max_tokens)
        elif profile_name == "deepseek-api":
            llm = self._deepseek(identity.model_id, tag, thinking=thinking,
                                 max_tokens=max_tokens)
        elif profile_name == "local":
            llm = self._local_or_beam(tag) if self.local_up else None
        else:
            llm = None
        self._handle_cache[key] = llm
        return llm

    def _identity_available(self, identity: ModelIdentity) -> Optional[bool]:
        if identity.credential_profile == "opencode-free":
            return self.probes.get((identity.credential_profile, identity.model_id))
        return None

    def _request_paid_grant(self, request: RouteRequest,
                            candidate: Any) -> Optional[str]:
        options = self.payg_options()
        if candidate.identity.model_id not in options:
            options = [candidate.identity.model_id] + options
        reason = (f"PAYG {candidate.identity.model_id} para "
                  f"{request.task_id or request.role}")
        if self.on_budget is None:
            return None
        return self.on_budget(reason, options)

    def _cloud(self, model: str, tag: str, temperature: Optional[float] = None,
               thinking: bool = False, max_tokens: int = 8192) -> Any:
        if not self.cloud_enabled or ChatOpenAI is None:
            return None
        identity = self.registry.identity_for("opencode-go", model)
        ref = self.registry.ref_for_identity(identity)
        # NOTE: temperature is omitted on purpose: several OpenCode Go models
        # (kimi-k3.x, reasoning models) reject anything but 1.
        kwargs: dict[str, Any] = dict(
            model=model,
            base_url=self.settings.opencode_base_url,
            api_key=self.settings.opencode_api_key,
            streaming=True,
            max_tokens=max_tokens,
            max_retries=2,
            request_timeout=300,
            default_headers={
                "x-opencode-session": self._session_id,
                "x-opencode-request": f"req_{uuid4().hex[:16]}",
            },
            callbacks=self._handlers(tag, ref),
        )
        if temperature is not None:
            kwargs["temperature"] = temperature
        if thinking:
            kwargs["reasoning_effort"] = self.settings.reasoning_effort
        return _attach_identity(ChatOpenAI(**kwargs), ref)

    def _free_cloud(self, model: str, tag: str, max_tokens: int = 8192,
                    max_retries: int = 2) -> Any:
        """Same key, different (free zen) endpoint — used for the stealth model."""
        if not self.cloud_enabled or ChatOpenAI is None:
            return None
        identity = self.registry.identity_for("opencode-free", model)
        available = self.probes.get(("opencode-free", model))
        ref = self.registry.ref_for_identity(identity, available=available)
        return _attach_identity(ChatOpenAI(
            model=model,
            base_url=self.settings.senior_base_url,
            api_key=self.settings.opencode_api_key,
            streaming=True,
            max_tokens=max_tokens,
            max_retries=max_retries,
            request_timeout=120,
            default_headers={
                "x-opencode-session": self._session_id,
                "x-opencode-request": f"req_{uuid4().hex[:16]}",
            },
            callbacks=self._handlers(tag, ref),
        ), ref)

    def _probe_senior(self) -> bool:
        """One cheap call to check whether the free stealth model is usable.

        The free zen endpoint lists `big-pickle` but answers 429 to API clients,
        so we only put it in the chain when it actually responds.
        """
        if not (self.settings.senior_probe and self.cloud_enabled):
            return False
        identity = self.registry.identity_for("opencode-free",
                                              self.settings.senior_model)
        if identity.billing == "unknown" and not self.allow_unknown:
            return False
        llm = self._free_cloud(self.settings.senior_model, "DEV", max_tokens=16, max_retries=0)
        if llm is None:
            return False
        try:
            resp = llm.invoke([HumanMessage(content="Reply with the single word OK.")])
            return bool((getattr(resp, "content", "") or "").strip())
        except Exception:
            return False

    def _openrouter(self, model: str, tag: str, temperature: Optional[float] = None,
                    thinking: bool = False, max_tokens: int = 8192) -> Any:
        if not self.openrouter_enabled or ChatOpenAI is None:
            return None
        identity = self.registry.openrouter_identity(
            model, self.openrouter_pricing.get(model))
        ref = self.registry.ref_for_identity(identity)
        kwargs: dict[str, Any] = dict(
            model=model,
            base_url=self.settings.openrouter_base_url,
            api_key=self.settings.openrouter_api_key,
            streaming=True,
            max_tokens=max_tokens,
            max_retries=2,
            request_timeout=300,
            default_headers={
                "HTTP-Referer": "https://crew-tui.local",
                "X-Title": "Crew TUI",
            },
            callbacks=self._handlers(tag, ref),
        )
        if temperature is not None:
            kwargs["temperature"] = temperature
        if thinking:
            kwargs["reasoning_effort"] = self.settings.reasoning_effort
        return _attach_identity(ChatOpenAI(**kwargs), ref)

    def _deepseek(self, model: Optional[str] = None, tag: str = "DEEPSEEK",
                  thinking: bool = False, max_tokens: int = 16384) -> Any:
        """Direct DeepSeek API (pay-as-you-go). Disabled without a key."""
        if not self.deepseek_enabled or ChatOpenAI is None:
            return None
        mid = model or self.settings.deepseek_model
        identity = self.registry.identity_for("deepseek-api", mid)
        ref = self.registry.ref_for_identity(identity)
        kwargs: dict[str, Any] = dict(
            model=mid,
            base_url=self.settings.deepseek_base_url,
            api_key=self.settings.deepseek_api_key,
            streaming=True,
            max_tokens=max_tokens,
            max_retries=2,
            request_timeout=300,
            callbacks=self._handlers(tag, ref),
        )
        if thinking:
            kwargs["reasoning_effort"] = self.settings.reasoning_effort
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        return _attach_identity(ChatOpenAI(**kwargs), ref)

    def is_peak(self, now: Optional[datetime] = None) -> bool:
        """True when `now` (UTC) falls inside a protected pay-as-you-go window.

        Windows are configurable and may wrap midnight. Weekends are excluded
        when `peak_weekdays_only` is set. With no policy/key this is a no-op.
        """
        s = self.settings
        if not s.peak_policy or not self.deepseek_enabled:
            return False
        now = now or datetime.now(timezone.utc)
        if s.peak_weekdays_only and now.weekday() >= 5:
            return False
        minutes = now.hour * 60 + now.minute
        for part in (s.peak_windows or "").split(","):
            part = part.strip()
            if "-" not in part:
                continue
            start_s, end_s = (x.strip() for x in part.split("-", 1))
            try:
                sh, sm = (int(v) for v in start_s.split(":"))
                eh, em = (int(v) for v in end_s.split(":"))
            except ValueError:
                continue
            start, end = sh * 60 + sm, eh * 60 + em
            if start <= end:
                if start <= minutes < end:
                    return True
            elif minutes >= start or minutes < end:  # window wraps midnight
                return True
        return False

    def refresh_peak(self) -> bool:
        """Rebuild handles if the clock crossed a peak boundary. Returns True if rebuilt."""
        peak = self.is_peak()
        if peak == self._peak_at_build:
            return False
        self._build()
        return True

    def _gemini(self, model: Optional[str] = None, temperature: float = 0.3,
                thinking: bool = False, tag: str = "GEMINI") -> Any:
        if not self.gemini_enabled or ChatGoogleGenerativeAI is None:
            return None
        mid = model or self.settings.gemini_model
        identity = self.registry.identity_for("gemini-plus", mid)
        ref = self.registry.ref_for_identity(identity)
        return _attach_identity(ChatGoogleGenerativeAI(
            model=model or self.settings.gemini_model,
            google_api_key=self.settings.gemini_api_key,
            temperature=temperature,
            streaming=True,
            max_retries=1,
            timeout=120,
            max_output_tokens=self.settings.gemini_max_output_tokens,
            thinking_budget=4096 if thinking else 0,
            callbacks=self._handlers(tag, ref),
        ), ref)

    def _local_or_beam(self, tag: str) -> Any:
        if self.local_up and ChatOpenAI is not None:
            thinking = self.settings.think_worker if tag == "WORKER" else self.settings.think_critic
            identity = self.registry.identity_for("local",
                                                  self.settings.local_model_name)
            ref = self.registry.ref_for_identity(identity)
            kwargs: dict[str, Any] = dict(
                model=self.settings.local_model_name,
                base_url=self.settings.local_llm_url,
                api_key="local",  # llama.cpp ignores this
                temperature=0.3,
                streaming=True,
                request_timeout=600,
                callbacks=self._handlers(tag, ref),
            )
            if not thinking:
                kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
            return _attach_identity(ChatOpenAI(**kwargs), ref)
        # Local down: Gemini (sunk cost) first, subscription model only last.
        return (self._gemini(self.settings.gemini_model, thinking=False, tag=tag)
                or self._cloud(self.settings.beam_free_model, tag, thinking=self.settings.think_worker))

    def _allowed(self, llms: list[Any]) -> list[Any]:
        """Drop pay-as-you-go handles unless the user explicitly authorized them."""
        return [llm for llm in llms
                if llm is not None and (self.allow_paid or not bills_per_token(llm))]

    def _build(self) -> None:
        s = self.settings
        self._handle_cache.clear()
        self._peak_at_build = self.is_peak()
        # Manager: strong reasoning for the (short) spec step. Gemini Pro is the
        # sunk-cost primary; Go subscription models are last resort unless
        # OPENCODE_MODE=primary.
        go_primary = s.opencode_mode == "primary"
        fieldmarshal = self._cloud(s.fieldmarshal_model, "FIELDMARSHAL", thinking=s.think_fieldmarshal)
        fieldmarshal_fb = self._cloud(s.fallback_model, "FIELDMARSHAL", thinking=s.think_fieldmarshal)
        manager_gemini = self._gemini(s.gemini_pro_model, thinking=True, tag="FIELDMARSHAL")
        manager_openrouter = self._openrouter(s.openrouter_model, "FIELDMARSHAL")
        manager_chain = ([fieldmarshal, fieldmarshal_fb, manager_gemini, manager_openrouter]
                         if go_primary else
                         [manager_gemini, manager_openrouter, fieldmarshal, fieldmarshal_fb])
        manager_chain = self._allowed(manager_chain)
        self.manager_primary = manager_chain[0] if manager_chain else None
        manager_fallbacks = manager_chain[1:]
        if self.manager_primary is not None and manager_fallbacks:
            self.manager = self.manager_primary.with_fallbacks(manager_fallbacks)
        else:
            self.manager = self.manager_primary

        # Architect: off-peak -> direct DeepSeek (cheap pay-as-you-go, only when
        # keyed); peak -> Gemini Pro. Never leave direct DeepSeek in the peak
        # fallback chain, otherwise a Gemini failure could incur peak pricing.
        self.gemini_pro = self._gemini(s.gemini_pro_model, thinking=True, tag="ARCHITECT")
        arch_cloud = self._cloud(s.architect_model, "ARCHITECT", thinking=s.think_architect)
        arch_argon = self._cloud(s.review_model, "ARCHITECT", max_tokens=16384)
        arch_openrouter = self._openrouter(s.openrouter_model, "ARCHITECT")
        self.deepseek = self._deepseek(s.deepseek_model, "ARCHITECT", thinking=True, max_tokens=24576)
        self.deepseek_offpeak = (None if (self.deepseek is None or self.is_peak()
                                        or not self.allow_paid) else self.deepseek)
        if self.deepseek_offpeak is not None:
            arch_primary = self.deepseek_offpeak
            arch_rest = [self.gemini_pro, arch_openrouter, arch_argon, arch_cloud]
        else:
            arch_primary = self.gemini_pro
            arch_rest = ([arch_argon, arch_openrouter, arch_cloud]
                         if go_primary else
                         [arch_openrouter, arch_argon, arch_cloud])
        arch_fallbacks = self._allowed(arch_rest)
        if arch_primary is not None and arch_fallbacks:
            self.architect = arch_primary.with_fallbacks(arch_fallbacks)
        else:
            self.architect = arch_primary or (arch_fallbacks[0] if arch_fallbacks else None)

        self.fast_cloud = self._cloud(s.fast_cloud_model, "WORKER", thinking=s.think_fast_cloud)
        # Escalation: direct DeepSeek off-peak -> Gemini Pro -> Go argon/deepseek.
        escalate_chain = ([self._cloud(s.review_model, "ESCALATE", max_tokens=16384),
                           self._cloud(s.fieldmarshal_model, "ESCALATE", thinking=True)]
                          if go_primary else
                          [self.deepseek_offpeak, self.gemini_pro,
                           self._cloud(s.review_model, "ESCALATE", max_tokens=16384),
                           self._cloud(s.fieldmarshal_model, "ESCALATE", thinking=True)])
        escalate_chain = self._allowed(escalate_chain)
        if len(escalate_chain) > 1:
            self.escalate = escalate_chain[0].with_fallbacks(escalate_chain[1:])
        else:
            self.escalate = escalate_chain[0] if escalate_chain else None

        self.worker = self._local_or_beam("WORKER")
        self.critic_local = None  # local Qwen is never a critic (too weak to gate quality)
        self.critic_cloud = self._cloud(s.architect_model, "CRITIC", thinking=s.think_critic)
        # free strong critic: Gemini Pro reviews Gemini Flash (pro/flash quality gap)
        self.critic_gemini = self._gemini(s.gemini_pro_model, thinking=False, tag="CRITIC")
        self.critic_argon = self._cloud(s.review_model, "CRITIC")
        self.critic_openrouter = self._openrouter(s.openrouter_model, "CRITIC")

        # Reviewer (read-only reports): Gemini API first, then OpenRouter free,
        # and subscription models only as a last resort (unless go_primary).
        review_candidates: list[Any] = []
        review_pool = ([self._cloud(s.review_model, "REVIEW", max_tokens=16384),
                        self._openrouter(s.openrouter_model, "REVIEW", max_tokens=16384),
                        self._gemini(s.gemini_model, thinking=False, tag="REVIEW"),
                        self._gemini(s.gemini_fallback_model, thinking=False, tag="REVIEW")]
                       if go_primary else
                       [self._gemini(s.gemini_model, thinking=False, tag="REVIEW"),
                        self._gemini(s.gemini_fallback_model, thinking=False, tag="REVIEW"),
                        self._openrouter(s.openrouter_model, "REVIEW", max_tokens=16384),
                        self._cloud(s.review_model, "REVIEW", max_tokens=16384)])
        review_candidates = self._allowed(review_pool)
        if review_candidates:
            self.reviewer = (review_candidates[0].with_fallbacks(review_candidates[1:])
                             if len(review_candidates) > 1 else review_candidates[0])

        # Gemini workhorse: flash -> 3.8-flash -> Pro -> OpenRouter free -> Go last.
        gemini_primary = self._gemini(s.gemini_model, thinking=s.think_gemini)
        fallbacks: list[Any] = []
        if gemini_primary is not None:
            if s.gemini_fallback_model:
                fallbacks.append(self._gemini(s.gemini_fallback_model, thinking=False))
            if s.gemini_pro_model != s.gemini_model:
                fallbacks.append(self._gemini(s.gemini_pro_model, thinking=True, tag="GEMINI"))
            # OpenRouter free models (Nemotron Ultra, etc.) before paid Go.
            openrouter_free = self._openrouter(s.openrouter_free_model, "GEMINI", max_tokens=16384)
            if openrouter_free is not None:
                fallbacks.append(openrouter_free)
            argon = self._cloud(s.review_model, "GEMINI", max_tokens=16384)
            if argon is not None:
                fallbacks.append(argon)
        go_fallback = self._cloud(s.fast_cloud_model, "GEMINI", thinking=False)
        if go_fallback is not None:
            fallbacks.append(go_fallback)

        fallbacks = self._allowed(fallbacks)
        if gemini_primary is not None and fallbacks:
            self.gemini = gemini_primary.with_fallbacks(fallbacks)
        elif gemini_primary is not None:
            self.gemini = gemini_primary
        else:
            self.gemini = go_fallback

        # Senior dev / fixer: free big-pickle first (only when the probe passed),
        # then Gemini Pro, the Gemini workhorse chain, and off-peak DeepSeek.
        senior_primary = (self._free_cloud(s.senior_model, "DEV", max_tokens=16384)
                          if (self.settings.senior_force or self.big_pickle_up) else None)
        senior_fallbacks = self._allowed([self.gemini_pro, self.gemini, self.deepseek_offpeak])
        if senior_primary is not None and senior_fallbacks:
            self.senior_dev = senior_primary.with_fallbacks(senior_fallbacks)
        else:
            self.senior_dev = senior_primary or (senior_fallbacks[0] if senior_fallbacks else None)

    # -- discovery ---------------------------------------------------------- #

    def discover(self) -> dict[str, Any]:
        """Probe endpoints (cloud + local) and rebuild handles once local_up is known."""
        info: dict[str, Any] = {"local_up": False, "cloud_models": []}

        # optional auto-start of the local server (configurable, off by default)
        if not self._ping(self.settings.local_llm_url):
            if self.settings.local_start_cmd:
                self._try_start_local()
                for _ in range(10):
                    time.sleep(2)
                    if self._ping(self.settings.local_llm_url):
                        break
        info["local_up"] = self.local_up = self._ping(self.settings.local_llm_url)

        if self.cloud_enabled:
            info["cloud_models"] = self._list_cloud_models()
            self.big_pickle_up = self._probe_senior()
            self.probes[("opencode-free", self.settings.senior_model)] = bool(
                self.big_pickle_up)
            info["senior_up"] = self.big_pickle_up
        if self.openrouter_enabled:
            info["openrouter_models"] = self._list_openrouter_models()

        # Rebuild handles so the worker points to the discovered local server.
        self._build()
        return info

    def _ping(self, base_url: str) -> bool:
        url = base_url.rstrip("/") + "/models"
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _try_start_local(self) -> None:
        try:
            subprocess.run(self.settings.local_start_cmd, shell=True, timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def _list_cloud_models(self) -> list[str]:
        url = self.settings.opencode_base_url.rstrip("/") + "/models"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.settings.opencode_api_key}",
                "User-Agent": "crew-tui/1.0",
                "x-opencode-session": self._session_id,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return []
        models = data.get("data", []) if isinstance(data, dict) else data
        self.cloud_models = {m.get("id", "") for m in models if isinstance(m, dict)}
        return sorted(self.cloud_models)

    def _list_openrouter_models(self) -> list[str]:
        url = self.settings.openrouter_base_url.rstrip("/") + "/models"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.settings.openrouter_api_key}",
                "User-Agent": "crew-tui/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return []
        models = data.get("data", []) if isinstance(data, dict) else data
        self.openrouter_models = {m.get("id", "") for m in models if isinstance(m, dict)}
        self.openrouter_pricing = {
            str(m.get("id")): dict(m.get("pricing") or {})
            for m in models if isinstance(m, dict) and m.get("id")
        }
        return sorted(self.openrouter_models)

    # -- invocation helpers ------------------------------------------------- #

    @staticmethod
    def _content_str(response: Any) -> str:
        content = getattr(response, "content", response)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                b.get("text", "") if isinstance(b, dict) else str(b) for b in content
            )
        return str(content)

    def complete(self, llm: Any, messages: list[Any], meta: Optional[dict[str, Any]] = None) -> str:
        """Run `llm.invoke` and return text. Streaming callbacks fire live.

        Every candidate attempted inside a fallback chain is recorded as its
        own ``TaskAttempt``. The spend gate and the unknown-billing guard run
        before any candidate is dispatched.
        """
        if llm is None:
            raise RuntimeError("model unavailable (missing key/endpoint)")
        meta = meta or {}
        tag = meta.get("tag") or "OVERRIDE"
        clear_call_context()
        is_route = bool(getattr(llm, "is_route_chain", False))
        refs = iter_model_refs(llm)
        dispatch_refs = refs or [ref_from_handle(llm)]
        if not is_route:
            if self.dispatch_guard is not None:
                # A handle without identity is treated as unknown, never as free.
                self.dispatch_guard(dispatch_refs)
            elif not getattr(self, "allow_unknown", False):
                for ref in dispatch_refs:
                    if ref.billing == MB_UNKNOWN:
                        raise UnknownBilling(
                            f"billing desconhecido para {ref.provider}/{ref.endpoint}/"
                            f"{ref.model_id}; autoriza com ALLOW_UNKNOWN=1/--allow-unknown")
            self._ensure_paid(llm, meta, tag)
        collector = AttemptCollector(run_id=str(meta.get("run") or ""),
                                     task=str(meta.get("task") or ""), tag=tag)
        token = _ATTEMPT_CTX.set(collector)
        try:
            result = self._invoke(llm, messages, meta, tag, allow_breaker=True)
            if not collector.events():
                served = last_served_ref() or (refs[0] if refs else ref_from_handle(llm))
                collector.synthesize(served, "ok")
            return result
        except Exception as e:
            if not collector.events():
                collector.synthesize(refs[0] if refs else ref_from_handle(llm),
                                     _error_kind(e), error=str(e))
            raise
        finally:
            ok_event = collector.last_ok(str(meta.get("task") or "") or None)
            call_id = last_call_id()
            if ok_event is not None and call_id:
                ok_event.attempt.call_id = call_id
            verifier_tags = {"CRITIC", "VISUAL", "READER", "ORACLE", "TEST"}
            if ok_event is not None and tag not in verifier_tags:
                if not hasattr(self, "last_attempt_model"):
                    self.last_attempt_model = {}
                self.last_attempt_model[ok_event.task or str(meta.get("task") or "")] = (
                    ok_event.attempt.model)
            _ATTEMPT_CTX.reset(token)
            if self.attempt_sink is not None:
                for event in collector.events():
                    try:
                        self.attempt_sink(event)
                    except Exception:
                        pass
            clear_call_context()

    def _ensure_paid(self, llm: Any, meta: Optional[dict[str, Any]],
                     tag: str) -> None:
        ref = ref_from_handle(llm)
        if ref.billing != BILLING_PAYG:
            return
        identity = (ModelIdentity.from_dict(ref.identity) if ref.identity
                    else self.registry.identity_for(ref.credential_profile, ref.model_id))
        request = RouteRequest(role=tag, tag=tag,
                               task_id=str((meta or {}).get("task") or ""),
                               run_id=str((meta or {}).get("run") or ""))
        if self.router.grants.allows(request, identity):
            return
        choice = self._request_paid(tag, "este papel só tinha um modelo pago disponível")
        if choice:
            self.router.grants.approve_task(request, {choice})
        if not self.router.grants.allows(request, identity):
            raise BudgetSuspended(
                f"{tag}: modelo pago bloqueado pelo circuit breaker para esta tarefa")

    def _invoke(self, llm: Any, messages: list[Any], meta: Optional[dict[str, Any]],
                tag: str, allow_breaker: bool) -> str:
        model = self._model_name(llm)
        run = (meta or {}).get("run")
        started = time.monotonic()
        if self.telemetry is not None and run:
            self.telemetry.record(run, "llm_start", model=model,
                                  tag=tag, task=(meta or {}).get("task"))
        try:
            with self._local_lock if llm is self.worker else _null_lock():
                response = llm.invoke(messages)
        except Exception as e:
            kind = _error_kind(e)
            if self.telemetry is not None and run:
                self.telemetry.record(run, "llm_error", model=model,
                                      tag=tag, task=(meta or {}).get("task"),
                                      kind=kind, error=str(e)[:300])
            raise
        fallback_ref = ref_from_handle(llm)
        self._record_usage(meta, response, model, int((time.monotonic() - started) * 1000),
                           fallback_ref=fallback_ref)
        text = self._content_str(response)
        if not text.strip():
            raise RuntimeError(f"empty response from {model} "
                               f"(finish_reason={getattr(response, 'response_metadata', {}).get('finish_reason')})")
        return text

    # -- budget breaker ----------------------------------------------------- #

    def payg_options(self) -> list[str]:
        """Registered pay-as-you-go models the user may authorize."""
        out: list[str] = []
        for identity in self.registry.models.values():
            if identity.billing != BILLING_PAYG:
                continue
            if identity.credential_profile == "deepseek-api" and not self.deepseek_enabled:
                continue
            if identity.credential_profile == "openrouter" and not self.openrouter_enabled:
                continue
            if identity.model_id not in out:
                out.append(identity.model_id)
        for raw in (self.settings.payg_models or "").split(","):
            m = raw.strip()
            if m and m not in out:
                out.append(m)
        return out

    def _request_paid(self, tag: str, reason: str) -> Optional[str]:
        options = self.payg_options()
        self.budget_events.append({"kind": "suspend", "tag": tag,
                                   "reason": reason, "options": options})
        if not options:
            return None
        if self.on_budget is not None:
            return self.on_budget(reason, options)
        return options[0] if self.allow_paid else None

    def _model_name(self, llm: Any) -> str:
        if getattr(llm, "is_route_chain", False):
            request = getattr(llm, "request", None)
            return f"route:{getattr(request, 'role', '?')}"
        for attr in ("model_name", "model"):
            value = getattr(llm, attr, None)
            if isinstance(value, str) and value:
                return value
        wrapped = getattr(llm, "runnable", None)
        if wrapped is not None:
            inner = self._model_name(wrapped)
            return f"{inner} (fallback)" if inner else "fallback"
        return type(llm).__name__

    def _record_usage(self, meta: Optional[dict[str, Any]], response: Any,
                      model: str = "", elapsed_ms: int = 0,
                      fallback_ref: Optional[ModelRef] = None) -> None:
        if self.telemetry is None or meta is None:
            return
        usage = getattr(response, "usage_metadata", None) or {}
        in_t = usage.get("input_tokens") or usage.get("input_token_count") or 0
        out_t = usage.get("output_tokens") or usage.get("output_token_count") or 0
        served = (getattr(response, "response_metadata", None) or {})
        served_name = served.get("model_name") or served.get("model")
        task_id = (meta or {}).get("task") or None
        collector = _ATTEMPT_CTX.get()
        event = collector.last_ok(task_id) if collector is not None else None
        ref = event.attempt.model if event is not None else fallback_ref
        if ref is None:
            ref = self.registry.ref_for("unknown", served_name or model or "unknown")
        elif served_name and served_name != ref.model_id:
            ref = self.registry.ref_served(ref, served_name)
        identity = (ModelIdentity.from_dict(ref.identity) if ref.identity
                    else self.registry.identity_for(ref.credential_profile, ref.model_id))
        cost: CostEstimate = self.registry.estimate(identity, int(in_t), int(out_t))
        rec = dict(meta)
        run = rec.pop("run", None)
        if run:
            rec.update({
                "in_tokens": in_t, "out_tokens": out_t,
                "model": ref.model_id or served_name or model or "?",
                "billing": identity.billing,
                "provider": identity.provider,
                "endpoint": identity.endpoint,
                "credential_profile": identity.credential_profile,
                "capabilities": sorted(identity.capabilities),
                "identity": identity.to_dict(),
                "cost": str(cost.total),
                "currency": cost.currency,
                "cost_basis": cost.basis,
                "cost_known": cost.known,
                "billable": cost.billable,
                "quota_consumed": cost.quota_consumed,
                "tier": identity.billing,
                "ms": elapsed_ms,
            })
            self.telemetry.record(run, "llm_call", **rec)

    def critic_for(self, producer_tag: str) -> Any:
        """Cheap-but-strong critic: free Gemini Pro by default; Kimi only when the
        producer is itself Gemini Pro (to stay cross-model). Local Qwen is never a critic."""
        if producer_tag == "ARCHITECT":
            return self.critic_cloud
        return self.critic_gemini or self.critic_cloud

    def _route_complexity(self, task: Any) -> str:
        raw = str(getattr(task, "complexity", "auto") or "auto").lower()
        if raw in ("low", "medium", "high", "complex", "hard", "tiny"):
            return raw
        if self._task_is_hard(task):
            return "high"
        if self.local_up and self._task_is_small(task):
            return "low"
        return "medium"

    def critic_candidates(self, producer: Optional[ModelRef]) -> list[tuple[Any, ModelRef]]:
        """Strong critics from a different family than the effective producer."""
        if (producer is None or not producer.family
                or producer.credential_profile in ("", "unknown")):
            return []
        specs = [
            ("gemini-plus", "gemini-3.1-pro-preview"),
            ("opencode-go", "kimi-k3.1-pro"),
            ("openrouter", "nvidia/nemotron-3-ultra"),
            ("opencode-go", "gemini-4-argon"),
        ]
        out: list[tuple[Any, ModelRef]] = []
        seen: set[str] = set()
        for profile, model_id in specs:
            identity = self.registry.identity_for(profile, model_id)
            if not identity.defined:
                continue
            if identity.family == producer.family:
                continue
            if not {"text"}.issubset(identity.capabilities):
                continue
            key = identity_key(identity)
            if key in seen:
                continue
            seen.add(key)
            handle = self._handle_for_identity(identity, "CRITIC", 16384, False)
            if handle is None:
                continue
            ref = self.registry.ref_for_identity(identity)
            out.append((handle, ref))
        return out

    def team_llm(self, task: Any, run_id: str = "") -> tuple[Any, str]:
        """Route a task through the central policy (fallback inside RouteChain)."""
        team = getattr(task, "team", "programming")
        if getattr(task, "review", False):
            role, tag, complexity = "review", "REVIEW", "medium"
        elif team in ("algorithm", "architect"):
            role, tag, complexity = "architecture", "ARCHITECT", "high"
        elif team in ("uiux", "large_context", "qa", "testing"):
            role, tag, complexity = "programming", "GEMINI", "medium"
        else:
            role, tag, complexity = "programming", "DEV", self._route_complexity(task)
        request = RouteRequest(
            role=role, complexity=complexity, tag=tag,
            required_capabilities=frozenset({"text"}),
            estimated_context=estimate_tokens(
                (getattr(task, "description", "") or "") + " " +
                " ".join(getattr(task, "acceptance", []) or [])),
            task_id=str(getattr(task, "id", "") or ""),
            run_id=run_id or "",
        )
        routed = self.router.route(request)
        if not routed.candidates:
            return None, tag
        return routed.chain, tag

    def _task_is_small(self, task: Any) -> bool:
        """Heuristic: only route to the weak local model for trivial, non-visual scripts."""
        target = (getattr(task, "target_file", "") or "").lower()
        if target.endswith((".html", ".htm", ".js", ".jsx", ".ts", ".tsx",
                            ".vue", ".svelte", ".css")):
            return False  # front-end/visual needs a strong model
        text = (getattr(task, "description", "") or "") + " " + " ".join(
            getattr(task, "acceptance", []) or [])
        return estimate_tokens(text) <= self.settings.local_task_tokens

    def _task_is_hard(self, task: Any) -> bool:
        """Route intrinsically complex tasks straight to the strongest model."""
        text = (getattr(task, "description", "") or "") + " " + " ".join(
            getattr(task, "acceptance", []) or [])
        return (estimate_tokens(text) > self.settings.hard_task_tokens
                or len(getattr(task, "acceptance", []) or []) >= 4)

    def status_summary(self) -> str:
        parts = []
        if getattr(self, "registry_error", ""):
            parts.append(f"registry ✗ {self.registry_error[:60]}")
        else:
            parts.append(f"registry ✓ {Path(self.registry.config_source).name}")
        parts.append("local Qwen" if self.local_up else f"local DOWN -> {self.settings.beam_free_model}")
        if not self.settings.opencode_api_key:
            parts.append("cloud ✗ (no OPENCODE_API_KEY)")
        elif not self.cloud_enabled:
            parts.append("cloud off (OPENCODE_MODE=off)")
        else:
            parts.append(f"cloud ✓ ({self.settings.opencode_mode})")
        if self.cloud_enabled and self.settings.senior_probe:
            parts.append(f"{self.settings.senior_model} ✓" if self.big_pickle_up
                         else f"{self.settings.senior_model} ✗→gemini")
        parts.append("openrouter ✓" if self.openrouter_enabled else "openrouter ✗ (no OPENROUTER_API_KEY)")
        parts.append("gemini ✓" if self.gemini_enabled else "gemini ✗ (no GEMINI_API_KEY)")
        if self.deepseek_enabled:
            parts.append("deepseek peak→gemini" if self.is_peak() else "deepseek off-peak ✓")
        if self.gemini_enabled:
            parts.append(f"gerador: {self.settings.gemini_model}")
        return " | ".join(parts)


class _null_lock:
    """Context manager no-op used when no lock is required."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


# --------------------------------------------------------------------------- #
#  Telemetry (append-only JSONL run log)
# --------------------------------------------------------------------------- #

class Telemetry:
    """Append-only JSONL event log shared by all agents/teams.

    Design (matches the `audit.jsonl` convention):
    * one JSON line per event, appended (never rewritten) -> `git diff`-friendly;
    * metadata + hashes only, never full payloads -> tiny, fast to read;
    * payload content lives as files under `workspace/<run_id>/` and is
      referenced by `sha`/`path`;
    * rotated at `log_max_bytes` so the file never grows unbounded;
    * everything stays inside the project dir -> copy/rsync to migrate.
    """

    def __init__(self, settings: Settings):
        self.log_dir = Path(settings.log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.log_dir / "events.jsonl"
        self.max_bytes = settings.log_max_bytes
        self._lock = threading.Lock()
        self.listeners: list[Callable[[dict[str, Any]], None]] = []

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Register an in-process listener (the dashboard) for live events."""
        self.listeners.append(callback)

    def new_run(self) -> str:
        return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"

    def record(self, run_id: str, event: str, **fields: Any) -> None:
        entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "run": run_id,
            "event": event,
        }
        entry.update(fields)
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"), default=str)
        with self._lock:
            self._rotate_if_needed()
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        for cb in list(self.listeners):
            try:
                cb(entry)
            except Exception:
                pass

    def _rotate_if_needed(self) -> None:
        try:
            if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                target = self.log_dir / f"events.{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
                self.path.rename(target)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
#  Spec contract (Pydantic)  — the "blueprint pinned to the wall"
# --------------------------------------------------------------------------- #

class TaskSpec(BaseModel):
    id: str
    title: str
    description: str
    team: str = "programming"
    acceptance: list[str] = Field(default_factory=list)
    target_file: str = ""
    review: bool = False
    status: str = "pending"
    attempts: list[dict] = Field(default_factory=list)
    delivered_by: Optional[dict] = None
    verification: Optional[dict] = None


class SpecContract(BaseModel):
    goal: str
    context: str = ""
    review: bool = False
    non_negotiables: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    tasks: list[TaskSpec] = Field(default_factory=list)

    def render(self) -> str:
        lines = [f"GOAL: {self.goal}"]
        if self.non_negotiables:
            lines.append("NON-NEGOTIABLES:\n  - " + "\n  - ".join(self.non_negotiables))
        if self.assumptions:
            lines.append("ASSUMPTIONS:\n  - " + "\n  - ".join(self.assumptions))
        if self.acceptance_criteria:
            lines.append("ACCEPTANCE CRITERIA:\n  - " + "\n  - ".join(self.acceptance_criteria))
        if self.questions:
            lines.append("QUESTIONS (answer to proceed):\n  - " + "\n  - ".join(self.questions))
        if self.tasks:
            lines.append("TASKS:")
            for t in self.tasks:
                acc = ", ".join(t.acceptance) if t.acceptance else "-"
                lines.append(f"  [{t.id}] {t.title} ({t.team}) — acc: {acc}")
        return "\n".join(lines)

    def plan(self) -> str:
        """Compact plan a human can read in 15 seconds (before any code runs)."""
        lines = [f"OBJETIVO: {self.goal}"]
        if self.non_negotiables:
            lines.append("REGRAS: " + "; ".join(self.non_negotiables[:4]))
        if self.acceptance_criteria:
            lines.append("ACEITE: " + "; ".join(self.acceptance_criteria[:4]))
        if self.tasks:
            lines.append(f"PLANO ({len(self.tasks)} passos):")
            for i, t in enumerate(self.tasks[:8], 1):
                target = f" → {t.target_file}" if t.target_file else ""
                lines.append(f"  {i}. {t.title} [{t.team}]{target}")
            if len(self.tasks) > 8:
                lines.append(f"  … +{len(self.tasks) - 8}")
        if self.questions:
            lines.append(f"PERGUNTAS ({len(self.questions)}):")
            for i, q in enumerate(self.questions[:6], 1):
                lines.append(f"  {i}) {q}")
        return "\n".join(lines)


# -- role system prompts ---------------------------------------------------- #

GENERATOR_SYSTEM = ("You produce code or deliverables that satisfy the acceptance criteria "
                    "exactly.")

# Local Qwen is the "chapa 7" junior: mechanical, objective, no story.
WORKER_SYSTEM = ("You are a junior coder. Produce ONLY objective, mechanical, boilerplate code: "
                 "small functions with a single clear responsibility, small well-encapsulated "
                 "classes. No narrative, no creative flourishes, no over-engineering, no "
                 "comments unless asked. Return exactly what was requested and nothing more.")

# Local Qwen also reads files and reports a terse brief to the thinkers.
READER_SYSTEM = ("You read source files and summarise them into a terse, factual technical "
                 "report for a senior engineer who cannot see the files.")


REVIEWER_SYSTEM = ("You are a senior code reviewer. You never write or modify project files. "
                   "You produce concise, falsifiable review reports: each finding cites the exact "
                   "file (:line when possible), the evidence, the impact and a concrete fix. "
                   "You never invent facts; you list what you could not verify.")

SPEC_SCHEMA_HINT = """
Return STRICT JSON only, with this exact shape:
{
  "goal": "the real decision/outcome this serves (not the literal task)",
  "context": "extra relevant context (may be empty)",
  "non_negotiables": ["hard constraints that must never be violated"],
  "assumptions": ["explicit assumptions you are making"],
  "acceptance_criteria": ["measurable, falsifiable criteria for 'good'"],
  "questions": ["anything you cannot decide and MUST ask the human"],
  "tasks": [
    {"id": "t1", "title": "short", "description": "what to build",
     "team": "programming|algorithm|uiux|large_context|qa",
     "target_file": "relative/path/inside/project",
     "acceptance": ["specific check for this task"]}
  ]
}
Keep tasks SMALL and compartmentalized (agile specking). If any non-negotiable
or goal is ambiguous, put it in "questions" instead of guessing.
`target_file` must be a relative path (e.g. "super_mario/index.html"); use "" only
when the deliverable is prose rather than a file. NEVER assign the same
`target_file` to two tasks: one file = one task (bundle its steps in the description).
"""


def coerce_spec(data: Any) -> SpecContract:
    """Lenient spec parsing with graceful degradation."""
    try:
        return SpecContract.model_validate(data)
    except Exception:
        if isinstance(data, dict):
            try:
                tasks = [TaskSpec.model_validate(t) if isinstance(t, dict) else TaskSpec(
                    id=f"t{i}", title=str(t), description=str(t)) for i, t in enumerate(data.get("tasks", []), 1)]
            except Exception:
                tasks = []
            return SpecContract(
                goal=str(data.get("goal", data.get("title", "")) or "unspecified goal"),
                context=str(data.get("context", "")),
                non_negotiables=[str(x) for x in data.get("non_negotiables", [])],
                assumptions=[str(x) for x in data.get("assumptions", [])],
                acceptance_criteria=[str(x) for x in data.get("acceptance_criteria", [])],
                questions=[str(x) for x in data.get("questions", [])],
                tasks=tasks,
            )
        return SpecContract(goal=str(data)[:500])


# --------------------------------------------------------------------------- #
#  Verifier  (oracle + cross-model critic)
# --------------------------------------------------------------------------- #

@dataclass
class OracleResult:
    passed: bool
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    files: list[Path] = field(default_factory=list)

    def render(self) -> str:
        if self.passed:
            return f"ORACLE PASS (exit {self.exit_code})"
        return f"ORACLE FAIL (exit {self.exit_code})\n  {self.stderr.strip()[:800]}"


@dataclass
class VerificationReport:
    task_id: str
    oracle: OracleResult
    critic: dict[str, Any]
    passed: bool = False


class Verifier:
    """Two independent verification levers (Karpathy's Layer 2)."""

    def __init__(self, settings: Settings, pool: LLMPool, emit: Callable[[str, str], None],
                 telemetry: Optional["Telemetry"] = None):
        self.settings = settings
        self.pool = pool
        self.emit = emit  # (tag, text) -> UI
        self.telemetry = telemetry

    # -- external oracle ---------------------------------------------------- #

    def run_oracle(self, task_id: str, artifact: str, run_id: Optional[str] = None,
                   suffix: str = "") -> OracleResult:
        """Check the artifact in the workspace (the project file is only written on PASS)."""
        self.emit("ORACLE", f"running external checks on {task_id} …")
        ws = Path(self.settings.workspace_dir)
        if run_id:
            ws = ws / run_id
        ws.mkdir(parents=True, exist_ok=True)
        blocks = [(lang, code) for lang, code in extract_code_blocks_typed(artifact) if code]
        if not blocks:
            res = OracleResult(False, -1, stderr="no code blocks found")
            self._record_oracle(run_id, task_id, res)
            return res

        failures: list[str] = []
        notes: list[str] = []
        written: list[Path] = []
        for i, (lang, block) in enumerate(blocks, 1):
            ext = suffix if (len(blocks) == 1 and suffix) else LANG_SUFFIX.get(lang, ".txt")
            fname = ws / f"{task_id}_{i}{ext}"
            fname.write_text(block, encoding="utf-8")
            written.append(fname)
            if suffix:
                lang = guess_lang(ext)

            ok, out, err, note = self._check(fname, lang)
            if note:
                notes.append(f"{fname.name}: {note}")
            if not ok:
                failures.append(f"{fname.name}:\n{err}")

        if failures:
            res = OracleResult(False, 1, stderr="\n".join(failures), files=written)
        else:
            res = OracleResult(True, 0, stdout="; ".join(notes) or "all files check cleanly", files=written)
        self._record_oracle(run_id, task_id, res)
        return res

    def _check(self, path: Path, lang: str) -> tuple[bool, str, str, str]:
        """Dispatch to the right checker for `lang`. Unknown lang -> permissive."""
        lang = lang or guess_lang(path)
        if lang in {"python", "py", "python3"}:
            ok, out, err = self._py_compile(path)
            if not ok:
                return ok, out, err, ""
            ruff = self._ruff(path) if shutil.which("ruff") else (True, "", "")
            if not ruff[0]:
                return False, ruff[1], f"ruff: {ruff[2] or ruff[1]}", ""
            return True, out, "", ""
        if lang in {"javascript", "js", "node", "mjs"}:
            node = shutil.which("node")
            if node is None:
                return True, "", "", "node not installed — syntax check skipped"
            try:
                proc = subprocess.run([node, "--check", str(path)],
                                      capture_output=True, text=True, timeout=30)
            except Exception as e:
                return True, "", "", f"node check skipped ({e})"
            return proc.returncode == 0, proc.stdout, proc.stderr, ""
        if lang in {"html", "css", "markdown", "md", "txt", ""}:
            return True, "", "", f"no oracle for '{lang or 'text'}' — critic only"
        return True, "", "", f"no oracle for '{lang}' — critic only"

    def _record_oracle(self, run_id: Optional[str], task_id: str, res: OracleResult) -> None:
        if self.telemetry and run_id:
            self.telemetry.record(run_id, "oracle", task=task_id,
                                  passed=res.passed, exit_code=res.exit_code)

    @staticmethod
    def _py_compile(path: Path) -> tuple[bool, str, str]:
        try:
            proc = subprocess.run([sys.executable, "-m", "py_compile", str(path)],
                                  capture_output=True, text=True, timeout=60)
            return proc.returncode == 0, proc.stdout, proc.stderr
        except Exception as e:
            return False, "", str(e)

    @staticmethod
    def _ruff(path: Path) -> tuple[bool, str, str]:
        try:
            proc = subprocess.run(["ruff", "check", str(path)],
                                  capture_output=True, text=True, timeout=60)
            return proc.returncode == 0, proc.stdout, proc.stderr
        except Exception as e:
            return False, "", str(e)

    # -- visual verification ----------------------------------------------- #

    @staticmethod
    def screenshot(url_path: Path, out_png: Path, width: int = 900,
                   height: int = 600, timeout: int = 90) -> bool:
        """Render an HTML page headless and dump a PNG (Firefox is bundled)."""
        firefox = shutil.which("firefox")
        if firefox is None:
            return False
        profile = out_png.parent / ".ffprofile"
        profile.mkdir(parents=True, exist_ok=True)
        url = url_path.resolve().as_uri()
        cmd = [firefox, "--headless", "--no-remote", "--profile", str(profile),
               "--screenshot", str(out_png), f"--window-size={width},{height}", url]
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except Exception:
            return False
        return out_png.exists() and out_png.stat().st_size > 1500

    def run_visual(self, task: Any, html_path: Path,
                   run_id: Optional[str] = None) -> dict[str, Any]:
        """Screenshot an HTML deliverable and use only a vision-capable model."""
        if html_path is None or not html_path.is_file():
            return {"verdict": verify_policy.NA, "objections": [],
                    "note": "no html to render"}
        ws = html_path.parent
        png = ws / f"{html_path.stem}_preview.png"
        ok = self.screenshot(html_path, png)
        if not ok:
            return {"verdict": verify_policy.UNVERIFIED, "objections": [],
                    "note": "screenshot failed (sem Firefox headless?)", "png": str(png)}
        request = RouteRequest(role="visual", tag="VISUAL",
                               required_capabilities=frozenset({"vision"}),
                               task_id=str(task.id), run_id=run_id or "",
                               max_tokens=8192)
        routed = self.pool.router.route(request)
        if not routed.candidates:
            self.emit("VISUAL", f"screenshot guardado: {png} (sem modelo de visão)")
            return {"verdict": verify_policy.UNVERIFIED, "objections": [],
                    "note": "sem modelo multimodal disponível", "png": str(png)}
        try:
            img = base64.b64encode(png.read_bytes()).decode()
        except Exception as e:
            return {"verdict": verify_policy.UNVERIFIED, "objections": [],
                    "note": str(e), "png": str(png)}
        criteria = "; ".join(task.acceptance) if task.acceptance else "looks right"
        prompt = f"""You are reviewing a screenshot of a web deliverable to decide if it renders correctly.

Task: {task.title}
Acceptance criteria: {criteria}

Look at the image and judge ONLY what you can actually see. Return STRICT JSON:
{{"verdict": "PASS" | "FAIL",
  "objections": [{{"claim": str, "why_wrong": str, "confidence": 0.0-1.0}}]}}

PASS means the page rendered and visibly satisfies the acceptance criteria. FAIL names
concrete visual problems (broken layout, missing elements, blank canvas, overflow, errors)."""
        self.emit("VISUAL", f"{task.id}: a pedir ao modelo de visão para avaliar {png.name} …")
        try:
            raw = self.pool.complete(routed.chain, [
                HumanMessage(content=[
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}},
                ]),
            ], meta={"run": run_id, "tag": "VISUAL", "task": task.id} if run_id else None)
            data = json.loads(extract_json_block(raw))
            verdict = verify_policy.normalize_verdict(data.get("verdict"))
            if verdict not in (verify_policy.PASS, verify_policy.FAIL):
                raise ValueError(f"visual verdict inválido: {data.get('verdict')!r}")
            data["verdict"] = verdict
            data["png"] = str(png)
            if routed.candidates:
                data["vision"] = routed.candidates[0].ref.to_dict()
        except Exception as e:
            data = {"verdict": verify_policy.UNVERIFIED, "objections": [],
                    "note": f"vision check failed: {str(e)[:200]}", "png": str(png)}
        if self.telemetry and run_id:
            objs = data.get("objections", []) if isinstance(data, dict) else []
            self.telemetry.record(run_id, "visual", task=task.id,
                                  verdict=data.get("verdict") if isinstance(data, dict) else "?",
                                  png=str(png), objections=len(objs))
        return data

    def run_test_oracle(self, task: TaskSpec, artifact: str,
                        run_id: Optional[str] = None) -> dict[str, Any]:
        """Local Qwen writes a self-contained test; we run it. A failing assert is a real signal."""
        if self.pool.worker is None or not self.pool.local_up:
            return {"verdict": "SKIP", "note": "sem Qwen local para escrever o teste"}
        blocks = [(lang, code) for lang, code in extract_code_blocks_typed(artifact) if code]
        py = next((c for l, c in blocks if l in ("python", "py", "python3") or
                   (not l and c.lstrip().startswith(("def ", "import ", "from ", "class ")))), None)
        if py is None:
            return {"verdict": "SKIP", "note": "não é Python"}
        criteria = "; ".join(task.acceptance) if task.acceptance else "behaves correctly"
        prompt = (f"Write ONE self-contained Python test script: paste the functions/classes under "
                  f"test at the TOP (no imports beyond the standard library), then assert their "
                  f"behaviour against: {criteria}.\n\nCODE:\n{py[:6000]}\n\n"
                  f"Return a single ```python block. When run with `python`, it must print OK and "
                  f"exit 0 only if every assertion passes.")
        try:
            raw = self.pool.complete(self.pool.worker, [
                SystemMessage(WORKER_SYSTEM),
                HumanMessage(prompt),
            ], meta={"run": run_id, "tag": "READER", "task": task.id} if run_id else None)
        except Exception as e:
            return {"verdict": "UNAVAILABLE", "note": f"Qwen falhou a escrever o teste: {str(e)[:120]}"}
        test_blocks = [c for _l, c in extract_code_blocks_typed(raw) if c]
        if not test_blocks:
            return {"verdict": "UNAVAILABLE", "note": "Qwen não devolveu um teste"}
        ws = Path(self.settings.workspace_dir) / (run_id or "latest")
        ws.mkdir(parents=True, exist_ok=True)
        test_path = ws / f"test_{task.id}.py"
        test_path.write_text(test_blocks[0], encoding="utf-8")
        if not self._py_compile(test_path)[0]:
            return {"verdict": "UNAVAILABLE", "note": "teste do Qwen não compila", "test": str(test_path)}
        try:
            proc = subprocess.run([sys.executable, str(test_path)],
                                  capture_output=True, text=True, timeout=120)
        except Exception as e:
            return {"verdict": "UNAVAILABLE", "note": f"timeout/erro: {str(e)[:120]}", "test": str(test_path)}
        if proc.returncode == 0:
            return {"verdict": "PASS", "note": proc.stdout.strip()[:200], "test": str(test_path)}
        return {"verdict": "FAIL", "note": (proc.stdout + proc.stderr).strip()[-400:], "test": str(test_path)}

    # -- cross-model critic ------------------------------------------------- #

    def run_critic(self, producer: Optional[ModelRef], task: Any, artifact: str,
                   run_id: Optional[str] = None, review: bool = False) -> dict[str, Any]:
        """Mandatory critic, selected by the effective producer identity."""
        producer_known = bool(producer and producer.family
                              and producer.credential_profile not in ("", "unknown"))
        base: dict[str, Any] = {
            "verdict": "UNAVAILABLE", "objections": [], "critic": None,
            "critic_family": "",
            "producer": producer.to_dict() if producer else None,
            "producer_family": producer.family if producer else "",
            "producer_known": producer_known,
            "note": "no cross-family critic available",
        }
        if not producer_known:
            base["note"] = "effective producer identity unknown"
            return base
        critics = self.pool.critic_candidates(producer)
        if not critics:
            return base

        self.emit("CRITIC", f"cross-model review of {task.id} …")
        criteria = "\n".join(f"- {c}" for c in (task.acceptance or ["correctness", "safety"]))
        lang = guess_lang(task.target_file) if task.target_file else ""
        fence = lang if lang else ""
        if review:
            kind = "review report (Markdown prose; code fences inside are illustrative)"
            artifact_block = f"REPORT START\n{artifact[:12000]}\nREPORT END"
        else:
            kind = "code deliverable"
            artifact_block = f"```{fence}\n{artifact[:8000]}\n```"
        prompt = f"""You are a ruthless critic of deliverables. You are FORBIDDEN to praise.

Producer identity: {producer.model_id} (family {producer.family})
Task: {task.title}
{task.description}
{f"Deliverable file: {task.target_file}" if task.target_file else ""}

Acceptance criteria:
{criteria}

Artifact under review ({kind}):
{artifact_block}

Return STRICT JSON only:
{{"verdict": "PASS" | "FAIL",
  "objections": [{{"claim": str, "why_wrong": str, "how_to_test": str, "confidence": 0.0-1.0}}],
  "missing_assumptions": [str]}}

verdict = PASS only if NO objection is actionable. Be concrete and falsifiable:
an objection must name the exact line/section that is wrong and how a reviewer
could falsify it. Style preferences, hypotheticals and "could be better" are NOT
objections — omit them. If the artifact satisfies every acceptance criterion,
answer PASS."""
        last_note = "no critic answered"
        for critic, ref in critics:
            if not verify_policy.critic_is_valid(producer.family, ref.family, True):
                continue
            try:
                raw = self.pool.complete(critic, [
                    SystemMessage("You output valid JSON only."),
                    HumanMessage(prompt),
                ], meta={"run": run_id, "tag": "CRITIC", "task": task.id} if run_id else None)
                data = json.loads(extract_json_block(raw))
                verdict = verify_policy.normalize_verdict(data.get("verdict"))
                if verdict not in (verify_policy.PASS, verify_policy.FAIL):
                    raise ValueError(f"critic verdict inválido: {data.get('verdict')!r}")
                data.update({
                    "verdict": verdict,
                    "critic": ref.to_dict(),
                    "critic_family": ref.family,
                    "producer": producer.to_dict(),
                    "producer_family": producer.family,
                    "producer_known": True,
                })
                if self.telemetry and run_id:
                    self.telemetry.record(run_id, "critic", task=task.id,
                                          verdict=verdict,
                                          critic_family=ref.family,
                                          producer_family=producer.family,
                                          critic_model=ref.model_id,
                                          objections=len(data.get("objections") or []))
                return data
            except Exception as exc:
                last_note = f"critic unavailable: {str(exc)[:200]}"
                continue
        base["note"] = last_note
        if self.telemetry and run_id:
            self.telemetry.record(run_id, "critic", task=task.id,
                                  verdict="UNAVAILABLE", note=last_note,
                                  producer_family=producer.family)
        return base


# --------------------------------------------------------------------------- #
#  Context guard (deterministic OOM protection)
# --------------------------------------------------------------------------- #

class ContextGuard:
    """Never trust a manager model to route >32k contexts: enforce in code."""

    def __init__(self, settings: Settings, pool: LLMPool, emit: Callable[[str, str], None],
                 telemetry: Optional["Telemetry"] = None):
        self.settings = settings
        self.pool = pool
        self.emit = emit
        self.telemetry = telemetry

    def protect_local(self, content: str, run_id: Optional[str] = None) -> str:
        """Return content safe for the local worker (summarise/truncate)."""
        tokens = estimate_tokens(content)
        if tokens <= self.settings.local_safe_tokens:
            return content
        self.emit("SYSTEM", f"context {tokens} tokens > local safe limit "
                            f"({self.settings.local_safe_tokens}) — compressing")
        if self.pool.gemini is not None:
            if self.telemetry and run_id:
                self.telemetry.record(run_id, "context_compress", tokens=tokens, method="gemini")
            return self._summarize(content)
        if self.telemetry and run_id:
            self.telemetry.record(run_id, "context_compress", tokens=tokens, method="truncate")
        truncated = content[: self.settings.local_safe_tokens * 3]
        self.emit("SYSTEM", "no gemini available — truncating context (data loss possible)")
        return truncated + "\n[TRUNCATED]"

    def local_brief(self, content: str, run_id: Optional[str] = None) -> str:
        """Have the LOCAL Qwen read the files and report a terse brief for the thinkers.

        Keeps large/sensitive content on the machine: only the summary leaves."""
        if self.pool.worker is None or not self.pool.local_up:
            return ""
        self.emit("SYSTEM", "Qwen local a ler os ficheiros e a resumir para os thinkers …")
        prompt = (READER_SYSTEM + "\n\n"
                  "Report: 1) purpose of the project; 2) key files and what each does; "
                  "3) public functions/classes (signatures only); 4) config/dependencies; "
                  "5) TODO/FIXME. Terse, factual, no opinions.\n\nFILES:\n"
                  + content[: self.settings.scan_total_chars])
        try:
            raw = self.pool.complete(self.pool.worker, [
                SystemMessage(READER_SYSTEM),
                HumanMessage(prompt),
            ], meta={"run": run_id, "tag": "READER", "task": "brief"} if run_id else None)
            return raw.strip()
        except Exception as e:
            self.emit("SYSTEM", f"resumo local falhou: {str(e)[:120]}")
            return ""

    def is_large(self, content: str) -> bool:
        return estimate_tokens(content) > self.settings.large_context_tokens

    def _summarize(self, content: str) -> str:
        prompt = ("Summarise the following content into a compact STRICT JSON object with keys "
                  "\"summary\" (bullet points), \"key_facts\" (list) and \"entities\" (list). "
                  "Preserve every fact that could affect code/decisions. Do not truncate facts.\n\n"
                  + content[:200_000])
        try:
            raw = self.pool.complete(self.pool.gemini, [
                SystemMessage("You compress large contexts into structured JSON, losslessly for facts."),
                HumanMessage(prompt),
            ])
            block = extract_json_block(raw)
            json.loads(block)  # validate
            return block
        except Exception as e:
            self.emit("SYSTEM", f"gemini summarisation failed: {e} — truncating")
            return content[: self.settings.local_safe_tokens * 3] + "\n[TRUNCATED]"


# --------------------------------------------------------------------------- #
#  Project scanner ("trabalha na pasta X")
# --------------------------------------------------------------------------- #

class ProjectScanner:
    """Reads a folder into a bounded, safe text brief for the agents.

    * skips VCS/venv/build/cache dirs and binary/secret files (never .env/pem/key);
    * caps file count, per-file chars and total chars (OOM/quota safeguard);
    * returns a plain-text brief (tree + contents) ready to inject in prompts.
    """

    SKIP_DIRS = {
        ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
        ".ruff_cache", ".pytest_cache", ".cache", "dist", "build", "target",
        ".idea", ".vscode", ".next", ".nuxt", "vendor", "logs", "specs", "workspace",
    }
    SKIP_NAMES = {".env", "id_rsa", "id_ed25519", "authorized_keys", "known_hosts"}
    SKIP_EXTS = {
        ".gguf", ".bin", ".pyc", ".pyo", ".png", ".jpg", ".jpeg", ".gif", ".pdf",
        ".zip", ".gz", ".tar", ".xz", ".so", ".o", ".exe", ".dll", ".woff", ".ttf",
        ".ico", ".mp3", ".mp4", ".mov", ".lock", ".kdbx", ".pem", ".key", ".crt",
    }
    TEXT_EXTS = {
        ".py", ".js", ".ts", ".tsx", ".jsx", ".md", ".txt", ".rst", ".json",
        ".yaml", ".yml", ".toml", ".cfg", ".ini", ".sh", ".bash", ".css", ".scss",
        ".html", ".sql", ".go", ".rs", ".java", ".c", ".h", ".cpp", ".hpp", ".cs",
        ".php", ".rb", ".swift", ".kt", ".lua", ".vue", ".svelte",
    }

    def __init__(self, settings: Settings):
        self.settings = settings

    def scan(self, root: str | Path) -> dict[str, Any]:
        root = Path(root).expanduser().resolve()
        files: list[tuple[Path, str, int]] = []
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root)
            if set(rel.parts) & self.SKIP_DIRS:
                continue
            if not p.is_file():
                continue
            if p.name in self.SKIP_NAMES or p.name.startswith(".env"):
                continue
            if p.suffix.lower() in self.SKIP_EXTS:
                continue
            if not self._is_text(p):
                continue
            files.append((p, rel.as_posix(), p.stat().st_size))
            if len(files) >= self.settings.scan_max_files:
                break

        lines = [f"=== PROJECT: {root} ===", "FILES:"]
        for _p, rel, size in files:
            lines.append(f"  {rel}  ({self._hum(size)})")

        contents: list[str] = []
        total = 0
        for p, rel, _size in files:
            if total >= self.settings.scan_total_chars:
                contents.append("[... mais ficheiros, fora do orçamento ...]")
                break
            data = self._read(p, self.settings.scan_max_chars_per_file)
            total += len(data)
            contents.append(f"--- {rel} ---\n{data}")
        if contents:
            lines.append("CONTENTS:")
            lines.extend(contents)

        return {
            "root": str(root),
            "files": [r for _p, r, _s in files],
            "text": "\n".join(lines),
        }

    def _is_text(self, p: Path) -> bool:
        if p.suffix.lower() in self.TEXT_EXTS:
            return True
        try:
            with open(p, "rb") as fh:
                return b"\x00" not in fh.read(4096)
        except Exception:
            return False

    @staticmethod
    def _read(p: Path, limit: int) -> str:
        try:
            data = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return "[unreadable]"
        if len(data) > limit:
            return data[:limit] + f"\n[... truncated at {limit} chars ...]"
        return data

    @staticmethod
    def _hum(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.0f}{unit}"
            n /= 1024
        return f"{n:.1f}TB"


# --------------------------------------------------------------------------- #
#  Orchestrator (state machine SPEC -> BUILD -> VERIFY)
# --------------------------------------------------------------------------- #

class Orchestrator:
    def __init__(self, settings: Settings, pool: LLMPool, emit: Callable[[str, str], None],
                 telemetry: Optional["Telemetry"] = None):
        self.settings = settings
        self.pool = pool
        self.emit = emit
        self.telemetry = telemetry
        self.guard = ContextGuard(settings, pool, emit, telemetry)
        self.verifier = Verifier(settings, pool, emit, telemetry)
        self.scanner = ProjectScanner(settings)
        self.power = make_power(settings, pool, emit, on_resume=self._power_resume)
        self.spec: Optional[SpecContract] = None
        self.run_id: Optional[str] = None
        self.project_root: Optional[Path] = None
        self.project_brief: str = ""
        self.project_tree: str = ""
        # session memory (survives runs and restarts)
        self.last_goal: str = ""
        self.last_project_root: Optional[Path] = None
        self.results: dict[str, dict[str, Any]] = {}
        self.history: list[dict[str, Any]] = []
        self.qa: list[tuple[str, str]] = []
        self.instruction: str = ""

    # -- session persistence ------------------------------------------------ #

    @property
    def session_path(self) -> Path:
        return Path(self.settings.log_dir) / "session.json"

    def load_session(self) -> bool:
        """Restore the last run's memory (goal, folder, results) from disk."""
        try:
            data = json.loads(self.session_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        goal = str(data.get("goal") or "")
        root = data.get("project_root") or ""
        if not goal:
            return False
        self.last_goal = goal
        if root:
            p = Path(root)
            if p.is_dir():
                self.last_project_root = p
                self.project_root = p
                self.project_tree = "\n".join(str(x) for x in data.get("files", [])[:200])
        self.results = data.get("results", {}) if isinstance(data.get("results"), dict) else {}
        self.history = data.get("history", []) if isinstance(data.get("history"), list) else []
        return True

    def save_session(self) -> None:
        try:
            path = self.session_path
            path.parent.mkdir(parents=True, exist_ok=True)
            files: list[str] = []
            if self.project_root and self.project_root.is_dir():
                files = sorted(str(p.relative_to(self.project_root))
                               for p in self.project_root.rglob("*") if p.is_file())[:200]
            payload = {
                "goal": self.last_goal,
                "project_root": str(self.last_project_root or self.project_root or ""),
                "files": files,
                "results": self.results,
                "history": self.history[-10:],
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            self.emit("SYSTEM", f"não consegui guardar a sessão: {e}")

    def answer(self, question: str) -> str:
        """Answer a follow-up question from session memory (no new spec/run)."""
        ctx = self.summary()
        llm = (self.pool.fast_cloud or self.pool.gemini or self.pool.manager
               or (self.pool.worker if self.pool.local_up else None))
        if llm is None:
            return ctx
        prompt = ("Answer the user's question about the work already done. Be brief and concrete. "
                  "Use ONLY the facts in RUN MEMORY; if something is not there, say you don't know.\n\n"
                  f"RUN MEMORY:\n{ctx}\n\nQUESTION: {question}")
        try:
            return self.pool.complete(llm, [
                SystemMessage("You answer strictly from the given run memory."),
                HumanMessage(prompt),
            ])
        except Exception as e:
            return f"{ctx}\n\n(não consegui consultar o modelo: {str(e)[:120]})"

    def summary(self) -> str:
        """One-paragraph memory of the last run (used by the chat and the UI)."""
        if not self.last_goal:
            return "Ainda não fiz nada nesta sessão."
        files = []
        if self.last_project_root and self.last_project_root.is_dir():
            files = sorted(str(p.relative_to(self.last_project_root))
                           for p in self.last_project_root.rglob("*") if p.is_file())[:12]
        lines = [f"Último objetivo: {self.last_goal}"]
        if self.last_project_root:
            lines.append(f"Pasta: {self.last_project_root}")
        if files:
            lines.append("Ficheiros: " + ", ".join(files))
        if self.results:
            for tid, res in self.results.items():
                bits = [res.get("status", "?"), res.get("team", "")]
                if res.get("file"):
                    bits.append(str(res["file"]))
                if res.get("error"):
                    bits.append(f"erro: {res['error']}")
                if res.get("reason"):
                    bits.append(f"motivo: {res['reason']}")
                if res.get("objections"):
                    obs = res["objections"]
                    if isinstance(obs, list) and obs and isinstance(obs[0], dict):
                        bits.append(f"objeção: {obs[0].get('claim', '')}")
                    else:
                        bits.append(f"{len(obs)} objeção(ões)")
                lines.append(f"  {tid}: " + " · ".join(b for b in bits if b))
        return "\n".join(lines)

    # -- run identity + persistence ---------------------------------------- #

    def _new_run_id(self) -> str:
        if self.telemetry is not None:
            return self.telemetry.new_run()
        return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"

    def _record(self, event: str, **fields: Any) -> None:
        if self.telemetry is not None and self.run_id is not None:
            self.telemetry.record(self.run_id, event, **fields)

    def _save_spec(self, spec: SpecContract) -> None:
        if self.run_id is None:
            return
        spec_dir = Path(self.settings.spec_dir)
        spec_dir.mkdir(parents=True, exist_ok=True)
        payload = spec.model_dump_json(indent=2)
        path = spec_dir / f"{self.run_id}.json"
        path.write_text(payload, encoding="utf-8")
        self._record("spec_frozen", spec_sha=sha256_hex(payload), path=str(path),
                     tasks=len(spec.tasks), questions=len(spec.questions),
                     goal=spec.goal[:120])

    def _save_artifact(self, task: TaskSpec, content: str) -> Path:
        run_dir = Path(self.settings.workspace_dir) / (self.run_id or "latest")
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / f"{task.id}.txt"
        path.write_text(content, encoding="utf-8")
        return path

    # -- deliverables ------------------------------------------------------- #

    FILE_HINT_RE = re.compile(
        r"([\w./\\-]+\.(?:py|js|mjs|ts|tsx|jsx|html|htm|css|json|md|sql|sh|txt))\b", re.I)

    def _target_for(self, task: TaskSpec) -> str:
        """Relative file path for this task's deliverable ('' = not a file task)."""
        raw = (task.target_file or "").strip()
        if not raw:
            for text in (task.description, task.title):
                m = self.FILE_HINT_RE.search(text or "")
                if m:
                    raw = m.group(1)
                    break
        if not raw:
            return ""
        return raw.replace("\\", "/").lstrip("./").strip()

    def _deliver(self, task: TaskSpec, artifact: str) -> Optional[Path]:
        """Write the artifact into the user's project folder (path-traversal safe).

        Review runs are read-only: the report goes to workspace/<run>/review.md.
        """
        if self.spec is not None and self.spec.review:
            path = Path(self.settings.workspace_dir) / (self.run_id or "latest") / "review.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(artifact.strip() + "\n", encoding="utf-8")
            return path
        if self.project_root is None:
            return None
        target = self._target_for(task)
        if not target:
            return None
        root = self.project_root.resolve()
        dest = (root / target).resolve()
        try:
            dest.relative_to(root)
        except ValueError:
            self.emit("SYSTEM", f"alvo fora do projeto ignorado: {target}")
            return None
        blocks = [(lang, code) for lang, code in extract_code_blocks_typed(artifact) if code]
        if not blocks:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(blocks[0][1].rstrip("\n") + "\n", encoding="utf-8")
        for i, (lang, code) in enumerate(blocks[1:], 2):
            suffix = LANG_SUFFIX.get(lang, dest.suffix)
            sibling = dest.with_name(f"{dest.stem}_{i}{suffix}")
            sibling.write_text(code.rstrip("\n") + "\n", encoding="utf-8")
        return dest

    # -- spec --------------------------------------------------------------- #

    def on_attempt(self, event: AttemptEvent) -> None:
        """Record generation attempts; verifier calls are evidence, not producers."""
        if event.tag in ("CRITIC", "VISUAL", "READER", "ORACLE", "TEST"):
            self.history.append({"kind": "attempt", **event.to_dict()})
            return
        if self.spec is not None and event.task:
            for task in self.spec.tasks:
                if task.id == event.task:
                    task.attempts.append(event.attempt.to_dict())
                    if event.attempt.outcome == "ok":
                        task.delivered_by = event.attempt.model.to_dict()
                    break
        else:
            self.history.append({"kind": "attempt", **event.to_dict()})
        try:
            if self.spec is not None:
                self._save_spec(self.spec)
        except Exception:
            pass

    def _mark_delivered(self, task: TaskSpec) -> None:
        for raw in reversed(task.attempts):
            if raw.get("outcome") == "ok" and raw.get("model"):
                task.delivered_by = raw["model"]
                return

    def draft_spec(self, instruction: str, feedback: str = "",
                   continues: bool = False) -> SpecContract:
        self.pool.attempt_sink = self.on_attempt
        fresh = self.run_id is None
        review = bool(REVIEW_RE.search(instruction or ""))
        if fresh:
            self.run_id = self._new_run_id()
            self.instruction = instruction
            self.project_root = None
            self.project_brief = ""
            self.project_tree = ""
            self._resolve_input(instruction, review=review)
            if self.project_root is None and continues and self.last_project_root is not None:
                # follow-up on the same deliverable: keep folder + file listing
                self.project_root = self.last_project_root
                if self.last_project_root.is_dir():
                    self._scan_project(self.last_project_root, quiet=True, summarise=not review)
        self.emit("SYSTEM", "SPEC phase — co-designing the contract …")
        if fresh:
            self.qa = []
            self._record("run_start", phase="SPEC", **content_digest(instruction))
        if feedback:
            prev_q = self.spec.questions[0] if (self.spec and self.spec.questions) else ""
            self.qa.append((prev_q, feedback))
            self._record("spec_refined", feedback_sha=sha256_hex(feedback))

        candidates: list[tuple[Any, str]] = []
        # Qwen drafts the spec for FREE first; strong models only take over if it fails.
        for llm, tag in (
            (self.pool.worker if self.pool.local_up else None, "WORKER"),
            (self.pool.architect, "ARCHITECT"),
            (self.pool.gemini, "GEMINI"),
            (self.pool.manager, "FIELDMARSHAL"),
            (self.pool.fast_cloud, "WORKER"),
        ):
            if llm is not None and all(llm is not c[0] for c in candidates):
                candidates.append((llm, tag))
        if not candidates:
            self.emit("SYSTEM", "no manager available — building a minimal spec")
            spec = coerce_spec({"goal": instruction, "tasks": [{"id": "t1", "title": instruction, "description": instruction, "team": "programming"}]})
            self._record("spec_fallback", reason="no manager")
            self.spec = spec
            self._save_spec(spec)
            return spec

        guard_note = (
            f"Context protection is active: local worker must never receive >{self.settings.local_safe_tokens} "
            "tokens; large contexts go to gemini.\n"
        )
        prompt = (
            "You are the Spec Architect (a ghost, not a person: do not guess, do not flatter).\n"
            "Your job is to turn the user's instruction into a precise, agile spec.\n"
            f"{guard_note}\n"
            f"User instruction:\n{instruction}\n"
            + (f"\nUser feedback from previous round:\n{feedback}\n" if feedback else "")
            + "\nPHASE: PLANNING — no code is written yet and nothing runs until the human types 'go'.\n"
            "- First sketch a SHORT synthetic plan a human reads in 15 seconds (goal + ordered steps + how you verify).\n"
            "- Then ask 2-5 decisive questions about anything that changes implementation: target folder/file,\n"
            "  inputs/outputs, tech constraints and dependencies, what is explicitly OUT of scope, how success is checked.\n"
            "- Never repeat a question already answered in HUMAN Q&A. No cosmetic or bikeshed questions.\n"
            "- If the instruction is already fully specified, ask at most 2 confirmation questions.\n"
            + (("\nHUMAN Q&A SO FAR (treat these as decided facts):\n"
                + "\n".join(f"Q: {q}\nA: {a}" for q, a in self.qa[-6:]) + "\n") if self.qa else "")
            + "\n" + SPEC_SCHEMA_HINT
        )
        if self.project_brief:
            prompt = ("PROJECT CONTEXT — the user pointed you to this folder (\"trabalha na pasta X\"); "
                      "everything you need is here:\n" + self.project_brief + "\n\n" + prompt)
        if continues and self.last_goal:
            prompt = ("PREVIOUS RUN (this is a follow-up; keep everything that already works):\n"
                      + self.summary() + "\n\n" + prompt)

        spec: Optional[SpecContract] = None
        errors: list[str] = []
        for llm, tag in candidates:
            retries = 2 if tag == "WORKER" else 1
            for attempt in range(retries):
                try:
                    this_prompt = prompt
                    if attempt > 0:
                        this_prompt += ("\n\nYour previous answer was not valid STRICT JSON or was "
                                        "structurally weak. Fix it: return a valid spec only.")
                    if llm is self.pool.worker and self.pool.local_up:
                        this_prompt = self.guard.protect_local(this_prompt, run_id=self.run_id)
                    raw = self.pool.complete(llm, [
                        SystemMessage("You are a precise spec engineer. You output valid JSON only."),
                        HumanMessage(this_prompt),
                    ], meta={"run": self.run_id, "tag": tag, "task": "spec"})
                    candidate = coerce_spec(json.loads(extract_json_block(raw)))
                    ok, why = self._spec_is_ok(candidate, instruction)
                    if not ok:
                        raise ValueError(why)
                    spec = candidate
                    if tag != "FIELDMARSHAL":
                        self.emit("SYSTEM", f"spec drafted by {tag} ({'local, gratuito' if tag == 'WORKER' else 'free fallback'})")
                        self._record("spec_drafted", by=tag)
                    break
                except Exception as e:
                    errors.append(f"{tag}: {str(e)[:120]}")
                    continue
            if spec is not None:
                break

        if spec is None:
            self.emit("SYSTEM", f"spec drafting failed ({'; '.join(errors)}) — using fallback spec")
            spec = coerce_spec({"goal": instruction, "tasks": [{"id": "t1", "title": instruction, "description": instruction, "team": "programming"}]})
            self._record("spec_fallback", reason="; ".join(errors)[:300])
        self.spec = spec
        if review:
            spec = self._as_review_spec(spec)
            self.spec = spec
            self.emit("SYSTEM", "modo REVISÃO (read-only): relatório em workspace/, projeto intocado.")
            self._record("review_mode", tasks=len(spec.tasks))
        if fresh and not feedback and not spec.questions and len(instruction.split()) < 12:
            spec.questions = [
                "Qual é o objetivo concreto e quem vai usar isto?",
                "Onde deve ficar (pasta/ficheiro) e em que formato entregar?",
                "Como verifico que está pronto (teste ou critério observável)?",
            ]
            self.emit("SYSTEM", "instrução curta — pedi clarificação antes de construir.")
            self._record("spec_questions_auto", count=len(spec.questions))
        self._adopt_project_from_spec(spec)
        self._save_spec(spec)
        return spec

    @staticmethod
    def _as_review_spec(spec: SpecContract) -> SpecContract:
        """Collapse any plan into ONE read-only review task (report, not code)."""
        steps = "; ".join(f"{t.title}: {t.description[:80]}" for t in spec.tasks[:6])
        spec.review = True
        spec.non_negotiables = list(dict.fromkeys(
            list(spec.non_negotiables)
            + ["read-only: não alterar nem criar ficheiros no projeto"]))
        spec.tasks = [TaskSpec(
            id="t1",
            title="Revisão read-only do projeto",
            description=("Lê todos os ficheiros e produz um relatório de revisão acionável: "
                         "defeitos concretos por ficheiro/linha, contratos quebrados entre ficheiros, "
                         "referências em falta, ficheiros truncados/incompletos, APIs incompatíveis, "
                         "riscos e prioridades."
                         + (f" Foco do plano proposto: {steps}" if steps else "")),
            team="large_context",
            target_file="",
            review=True,
            acceptance=[
                "cada defeito cita ficheiro (e linha quando possível) e o efeito observável",
                "cada defeito tem correção concreta",
                "não altera ficheiros do projeto",
                "lista omissões/incertezas em vez de inventar",
            ],
        )]
        return spec

    @staticmethod
    def _spec_is_ok(spec: SpecContract, instruction: str) -> tuple[bool, str]:
        """Cheap structural gate: reject specs that are empty or pure echoes."""
        if not spec.tasks:
            return False, "sem tarefas"
        if not (spec.goal or "").strip():
            return False, "goal vazio"
        if len(spec.tasks) > 12:
            return False, f"{len(spec.tasks)} tarefas é demasiado"
        for tk in spec.tasks:
            if not (tk.description or "").strip():
                return False, f"tarefa {tk.id} sem descrição"
        if len(spec.tasks) == 1 and (spec.tasks[0].description or "").strip() == instruction.strip():
            return False, "spec é apenas eco da instrução (sem decomposição)"
        return True, ""

    # -- project context --------------------------------------------------- #

    def _resolve_input(self, instruction: str, review: bool = False) -> None:
        d = self._find_dir_in(instruction) or self._create_dir_from(instruction)
        if d is None and self.settings.project_dir:
            p = Path(os.path.expanduser(self.settings.project_dir))
            if p.is_dir():
                d = p
        if d is not None:
            self._scan_project(d, summarise=not review)

    def _create_dir_from(self, text: str) -> Optional[Path]:
        """If the user named a folder that does not exist yet, create it."""
        base = Path(os.path.expanduser(self.settings.project_dir or "~")).resolve()
        for m in re.finditer(r"(?:~/|/|\.\.?/|[A-Za-z]:[\\/])[^\s\"'`,;]*", text):
            raw = m.group(0).rstrip(".,;:!?)]}\"'`")
            if not raw or "/v1" in raw or raw.startswith("//"):
                continue
            p = Path(os.path.expanduser(raw))
            if p.suffix:
                p = p.parent
            p = p if p.is_absolute() else (base / p)
            try:
                p = p.resolve()
            except Exception:
                continue
            if p.is_dir():
                return p
            if p.exists():
                continue
            if p.is_absolute() and len(p.parts) < 3:
                continue
            try:
                p.mkdir(parents=True, exist_ok=True)
                self.emit("SYSTEM", f"pasta criada: {p}")
                return p
            except Exception:
                continue
        return None

    def _adopt_project_from_spec(self, spec: SpecContract) -> None:
        """Use absolute paths the spec itself mentions to fix the deliverable folder."""
        if self.project_root is not None or not spec.tasks:
            return
        blob = "\n".join(
            " ".join([t.target_file, t.description, " ".join(t.acceptance)]) for t in spec.tasks
        )
        d = self._find_dir_in(blob) or self._create_dir_from(blob)
        if d is not None:
            self._scan_project(d)
            return
        # Still nowhere to put files? Use the default projects dir + goal slug.
        if any(t.target_file for t in spec.tasks):
            base = os.path.expanduser(self.settings.project_dir or "").strip()
            if not base:
                return
            slug = self._slug(spec.goal or spec.tasks[0].title) or "run"
            self.project_root = Path(base).resolve() / slug
            self.project_root.mkdir(parents=True, exist_ok=True)
            self.emit("SYSTEM", f"pasta do projeto: {self.project_root}")

    @staticmethod
    def _slug(text: str, limit: int = 40) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
        return slug[:limit].rstrip("_")

    def _find_dir_in(self, text: str) -> Optional[Path]:
        def clean(token: str) -> Path:
            return Path(os.path.expanduser(token.strip().strip("`'\"()[]<>.,;:!? ")))

        for m in re.finditer(r"@(\S+)", text):
            p = clean(m.group(1))
            if p.is_dir():
                return p
        for m in re.finditer(r"(?:~/|/|\.\.?/|[A-Za-z]:[\\/])[^\s\"'`]*", text):
            p = clean(m.group(0))
            if p.is_dir():
                return p
        # last resort: a bare name that exists as a sibling dir ("pasta super_mario.")
        for m in re.finditer(r"\b([\w.-]{2,})\b", text):
            p = clean(m.group(1))
            if p.is_dir():
                return p
        return None

    def _scan_project(self, root: Path, quiet: bool = False,
                      summarise: bool = True) -> None:
        if not quiet:
            self.emit("SYSTEM", f"projeto detetado: {root} — a ler ficheiros…")
        try:
            brief = self.scanner.scan(root)
            self.project_root = root
            self.project_brief = brief["text"]
            files = brief["files"][:200]
            self.project_tree = "\n".join(files)
            self._record("project_scanned", root=str(root), files=len(brief["files"]),
                         brief_chars=len(brief["text"]))
            if (summarise and len(brief["text"]) > self.settings.local_summarize_chars
                    and self.pool.local_up):
                report = self.guard.local_brief(brief["text"], run_id=self.run_id)
                if report:
                    self.project_brief = report
                    self._record("project_summarised_locally", by="qwen",
                                 in_chars=len(brief["text"]), out_chars=len(report))
                    if not quiet:
                        self.emit("SYSTEM", f"Qwen local resumiu o projeto "
                                            f"({len(brief['text'])} -> {len(report)} chars)")
            if not quiet:
                self.emit("SYSTEM", f"projeto lido: {len(brief['files'])} ficheiros "
                                    f"({len(brief['text'])} chars de contexto)")
        except Exception as e:
            self.emit("SYSTEM", f"erro ao ler projeto: {e}")
            self.project_brief = ""

    # -- build + verify ----------------------------------------------------- #

    def _power_resume(self) -> None:
        """After an off-peak wake: re-detect local worker and rebuild handles."""
        try:
            self.pool.discover()
        except Exception:
            self.pool.refresh_peak()

    def execute(self) -> None:
        self.power.start()
        try:
            self._execute()
        finally:
            self.power.stop()

    def _execute(self) -> None:
        self.pool.attempt_sink = self.on_attempt
        if self.spec is None:
            return
        self.results = {}
        self.last_goal = self.spec.goal
        self.last_project_root = self.project_root
        if self.spec.review:
            self.emit("SYSTEM", "REVIEW phase — a analisar o projeto (read-only, relatório no fim) …")
        else:
            self.emit("SYSTEM", "BUILD phase — executing tasks …")
        artifacts: dict[str, str] = {}
        producer_tags: dict[str, str] = {}

        for task in self.spec.tasks:
            self.power.before_step(f"task {task.id}")
            self.pool.refresh_peak()
            llm, tag = self.pool.team_llm(task, run_id=self.run_id or "")
            if llm is None:
                task.status = "skipped"
                self.results[task.id] = {"status": "skipped", "team": task.team}
                self.emit("SYSTEM", f"task {task.id} skipped (no model for team '{task.team}')")
                self._record("task_skipped", task=task.id, team=task.team)
                continue
            task.status = "running"
            self._record("task_start", task=task.id, team=task.team, producer=tag)
            prompt = self._build_prompt(task)
            if llm is self.pool.worker and self.pool.local_up:
                prompt = self.guard.protect_local(prompt, run_id=self.run_id)
            try:
                sys_prompt = (REVIEWER_SYSTEM if (self.spec and self.spec.review)
                              else WORKER_SYSTEM if tag == "WORKER" else GENERATOR_SYSTEM)
                content = self.pool.complete(llm, [SystemMessage(sys_prompt),
                    HumanMessage(prompt)], meta={"run": self.run_id, "tag": tag, "task": task.id})
                path = self._save_artifact(task, content)
                artifacts[task.id] = content
                producer_tags[task.id] = tag
                task.status = "built"
                self._record("task_built", task=task.id, producer=tag, path=str(path),
                             **content_digest(content))
            except Exception as e:
                self.emit("SYSTEM", f"task {task.id} failed: {e}")
                task.status = "failed"
                self.results[task.id] = {"status": "failed", "team": task.team,
                                         "error": str(e)[:300]}
                self._record("task_failed", task=task.id, error=str(e)[:300])

        self.emit("SYSTEM", "VERIFY phase — critic review …" if self.spec.review
                  else "VERIFY phase — oracle + critic + feedback loop …")
        for task in self.spec.tasks:
            if task.status != "built":
                continue
            artifact = artifacts[task.id]
            producer = producer_tags[task.id]
            last_objections: list[Any] = []
            last_reason = ""
            last_verdict = "FAIL"

            for attempt in range(1, self.settings.max_verify_loops + 1):
                self.power.before_step(f"verify {task.id}")
                passed, feedback, details = self._check_artifact(task, artifact, producer, self.run_id)
                objections = details.get("objections", []) if isinstance(details, dict) else []
                oracle = details.get("oracle") if isinstance(details, dict) else None
                critic = details.get("critic", {}) if isinstance(details, dict) else {}
                verdict = details.get("verdict", "FAIL") if isinstance(details, dict) else "FAIL"
                last_verdict = verdict
                if objections:
                    last_objections = objections
                if oracle is not None and not oracle.passed:
                    last_reason = f"oracle: {oracle.stderr.strip()[:160]}"
                elif verdict == "FAIL" and objections:
                    last_reason = f"critic: {str(objections[0].get('claim',''))[:160]}" if isinstance(objections[0], dict) else "critic FAIL"
                elif verdict == verify_policy.UNVERIFIED:
                    last_reason = str(critic.get("note", ""))[:160] if isinstance(critic, dict) else ""
                if isinstance(details, dict) and details.get("visual", {}).get("verdict") == "FAIL":
                    vobjs = details["visual"].get("objections", []) or []
                    last_reason = "visual FAIL: " + (str(vobjs[0].get("claim",""))[:160] if vobjs and isinstance(vobjs[0], dict) else "visual FAIL")
                if isinstance(details, dict) and details.get("test_oracle", {}).get("verdict") == "FAIL":
                    last_reason = "teste local FAIL: " + str(details["test_oracle"].get("note", ""))[:160]

                if passed:
                    status = "unverified" if verdict == verify_policy.UNVERIFIED else "verified"
                    task.status = "verified"
                    written = self._deliver(task, artifact)
                    self.results[task.id] = {
                        "status": status, "team": task.team,
                        "file": str(written) if written else "",
                        "chars": len(artifact),
                        "critic": critic.get("verdict") if isinstance(critic, dict) else verdict,
                        "verification": details.get("verification"),
                        "unverified_override": verdict == verify_policy.UNVERIFIED,
                    }
                    self._record("task_verified", task=task.id,
                                 delivered=str(written) if written else None,
                                 verdict=verdict)
                    self.emit("SYSTEM", f"✓ {task.id} {status}")
                    if written:
                        self.emit("SYSTEM", f"{task.id} -> {written}")
                    break

                if verdict == verify_policy.UNVERIFIED:
                    self.emit("SYSTEM", f"{task.id}: UNVERIFIED — sem entrega")
                    break

                self.emit("SYSTEM", f"↻ feedback loop {attempt}/{self.settings.max_verify_loops} for {task.id}")
                self._record("feedback_loop", task=task.id, attempt=attempt)
                llm, tag = self.pool.team_llm(task, run_id=self.run_id or "")
                if llm is None:
                    break
                try:
                    sys_prompt = (REVIEWER_SYSTEM if (self.spec and self.spec.review)
                                  else WORKER_SYSTEM if tag == "WORKER" else GENERATOR_SYSTEM)
                    self.power.before_step(f"rebuild {task.id}")
                    artifact = self.pool.complete(llm, [
                        SystemMessage(sys_prompt),
                        HumanMessage(f"Original task:\n{self._build_prompt(task)}\n\n"
                                     f"Previous artifact:\n```\n{artifact[:6000]}\n```\n\n"
                                     f"Verification feedback:\n{feedback}"),
                    ], meta={"run": self.run_id, "tag": tag, "task": task.id})
                    path = self._save_artifact(task, artifact)
                    self._record("task_rebuilt", task=task.id, attempt=attempt, path=str(path))
                except Exception as e:
                    self.emit("SYSTEM", f"fix failed: {e}")
                    break
            else:
                if (self.spec is not None and self.spec.review
                        and last_verdict != verify_policy.UNVERIFIED):
                    # review report can be delivered with documented objections,
                    # but a missing/invalid critic is UNVERIFIED (no delivery).
                    task.status = "verified"
                    written = self._deliver(task, artifact)
                    self.results[task.id] = {
                        "status": "verified-degraded", "team": task.team,
                        "file": str(written) if written else "",
                        "objections": last_objections,
                    }
                    self._record("task_verified_degraded", task=task.id,
                                 delivered=str(written) if written else None,
                                 objections=len(last_objections))
                    self.emit("SYSTEM", f"✓ {task.id} entregue com ressalvas "
                                        f"({len(last_objections)} objeção(ões) do crítico)")
                    if written:
                        self.emit("SYSTEM", f"{task.id} -> {written}")
                    continue
                if last_verdict == verify_policy.UNVERIFIED:
                    task.status = "unverified"
                    self.results[task.id] = {
                        "status": "unverified", "team": task.team,
                        "reason": last_reason or "crítico indisponível/não cross-family",
                        "verification": task.verification,
                    }
                    self._record("task_unverified", task=task.id,
                                 reason=last_reason[:200])
                    continue
                escalated = self._escalate(task, artifact, producer, last_reason, last_objections)
                if not escalated:
                    task.status = "failed-verification"
                    cand = str(Path(self.settings.workspace_dir) / (self.run_id or "latest"))
                    self.results[task.id] = {
                        "status": "failed-verification", "team": task.team,
                        "candidate": cand, "objections": last_objections,
                        "reason": last_reason,
                    }
                    self.emit("SYSTEM", f"✗ {task.id} não passou em "
                                        f"{self.settings.max_verify_loops} tentativas + escalação — "
                                        f"nada escrito no projeto (candidato: {cand})")
                    if last_reason:
                        self.emit("SYSTEM", f"  motivo: {last_reason[:200]}")
                    self._record("task_unverified", task=task.id, candidate=cand,
                                 reason=last_reason[:200])

            if task.status == "built" and last_verdict == verify_policy.UNVERIFIED:
                task.status = "unverified"
                self.results[task.id] = {
                    "status": "unverified", "team": task.team,
                    "reason": last_reason or "UNVERIFIED sem entrega",
                    "verification": task.verification,
                }
                self._record("task_unverified", task=task.id,
                             reason=last_reason[:200])

        statuses: dict[str, int] = {}
        for t in self.spec.tasks:
            statuses[t.status] = statuses.get(t.status, 0) + 1
        self._record("run_done", statuses=statuses)
        self.history.append({"goal": self.last_goal, "statuses": statuses,
                             "project_root": str(self.last_project_root or ""),
                             "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        # Step 9: Chief Sign-Off & Project Memory (project_memory.json)
        if self.project_root and self.project_root.is_dir():
            try:
                mem_path = self.project_root / "project_memory.json"
                memory_data = {
                    "goal": self.last_goal,
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "statuses": statuses,
                    "results": self.results,
                    "architecture": {t.id: {"title": t.title, "target": t.target_file, "team": t.team} for t in self.spec.tasks} if self.spec else {}
                }
                mem_path.write_text(json.dumps(memory_data, ensure_ascii=False, indent=2), encoding="utf-8")
                self.emit("SYSTEM", f"✓ project_memory.json atualizado em {mem_path}")
            except Exception as e:
                self.emit("SYSTEM", f"erro ao gerar project_memory.json: {e}")
        self.save_session()
        self.emit("RESUMO", self.summary())
        self.emit("SYSTEM", "DONE — podes continuar a falar comigo sobre isto.")
        self.run_id = None  # next instruction starts a fresh run

    # -- prompt helpers ----------------------------------------------------- #

    def _read_existing(self, target: str, limit: int = 12000) -> str:
        """Current content of the deliverable, if it already exists in the project."""
        if self.project_root is None:
            return ""
        root = self.project_root.resolve()
        path = (root / target).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            return ""
        if not path.is_file():
            return ""
        try:
            data = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""
        if len(data) > limit:
            return data[:limit] + f"\n[... truncated at {limit} chars ...]"
        return data

    def _build_prompt(self, task: TaskSpec) -> str:
        criteria = "\n".join(f"- {c}" for c in task.acceptance)
        nn = "\n".join(f"- {c}" for c in self.spec.non_negotiables) if self.spec else ""
        parts: list[str] = []
        if self.spec is not None and self.spec.review:
            brief = self.project_brief or self.project_tree
            return (
                "READ-ONLY REVIEW — do NOT write, create or modify any project file.\n"
                f"Goal: {self.spec.goal}\n"
                f"Task: {task.title}\n"
                f"{task.description}\n"
                f"Acceptance criteria:\n{criteria}\n"
                f"Non-negotiable constraints:\n{nn}\n\n"
                "Write a concise, falsifiable review report in Markdown (PT-PT).\n"
                "For each finding: severity (BLOCKER/BUG/RISK/NIT), file(:line), evidence, impact, "
                "and a concrete fix. Group by file. End with a short ordered action list.\n"
                "Do not restate the code; cite only what proves the defect.\n\n"
                f"PROJECT CONTENT (authoritative):\n{brief}"
            )
        if self.project_tree:
            parts.append("PROJECT FILES (paths only — read the target file content below):\n"
                         + self.project_tree)
        parts += [
            f"Goal: {self.spec.goal if self.spec else task.title}",
            f"Task: {task.title}",
            f"Description: {task.description}",
        ]
        if nn:
            parts.append(f"Non-negotiable constraints:\n{nn}")
        if criteria:
            parts.append(f"Acceptance criteria (measurable):\n{criteria}")
        target = self._target_for(task)
        if target:
            lang = guess_lang(target) or "text"
            parts.append(
                f"Deliverable file: {target}\n"
                f"Output the COMPLETE file content in ONE fenced block tagged ```{lang}. "
                "No prose, no placeholders, no elided sections."
            )
            existing = self._read_existing(target)
            if existing:
                parts.append(
                    f"CURRENT CONTENT of {target} (extend/refine it; keep everything that already "
                    f"works):\n```{lang}\n{existing}\n```"
                )
        elif self.project_root is not None:
            parts.append("Output the COMPLETE deliverable in a single fenced block with the "
                         "correct language tag (```python, ```html, ```javascript, ...).")
        else:
            parts.append("Produce the deliverable now. If code, wrap it in a fenced block with the "
                         "correct language tag.")
        return "\n\n".join(parts)

    def _producer_ref(self, task: TaskSpec) -> Optional[ModelRef]:
        if getattr(task, "delivered_by", None):
            return ModelRef.from_dict(task.delivered_by)
        for attempt in reversed(getattr(task, "attempts", []) or []):
            if attempt.get("outcome") == "ok" and attempt.get("model"):
                return ModelRef.from_dict(attempt["model"])
        return self.pool.last_attempt_model.get(task.id)

    def _check_artifact(self, task: TaskSpec, artifact: str, producer: str,
                        run_id: Optional[str]) -> tuple[bool, str, dict[str, Any]]:
        """Run evidence collection and the P5 delivery gate once."""
        target = self._target_for(task)
        suffix = Path(target).suffix if target else ""
        review = bool(self.spec is not None and self.spec.review)
        producer_ref = self._producer_ref(task)
        if review:
            oracle = OracleResult(True, 0, stdout="review report — external oracle skipped")
        else:
            oracle = self.verifier.run_oracle(task.id, artifact, run_id, suffix=suffix)
        critic = self.verifier.run_critic(producer_ref, task, artifact, run_id,
                                          review=review)
        objections = critic.get("objections", []) if isinstance(critic, dict) else []

        visual: dict[str, Any] = {"verdict": verify_policy.NA}
        html_file = next((f for f in oracle.files if f.suffix in (".html", ".htm")), None)
        if html_file is not None and self.settings.visual_check:
            visual = self.verifier.run_visual(task, html_file, run_id)
        vverdict = verify_policy.normalize_verdict(
            visual.get("verdict", verify_policy.NA) if isinstance(visual, dict) else verify_policy.NA)
        if vverdict == verify_policy.PASS:
            self.emit("VISUAL", f"{task.id}: visual PASS")
        elif vverdict == verify_policy.FAIL:
            self.emit("VISUAL", f"{task.id}: visual FAIL ({len(visual.get('objections', []))} problema(s))")
        elif vverdict == verify_policy.UNVERIFIED:
            self.emit("VISUAL", f"{task.id}: visual UNVERIFIED ({visual.get('note','')})")

        test_oracle: dict[str, Any] = {"verdict": "SKIP", "advisory": True}
        if suffix in (".py",) and self.settings.test_oracle:
            test_oracle = self.verifier.run_test_oracle(task, artifact, run_id)
            test_oracle["advisory"] = True
            tv = str(test_oracle.get("verdict") or "SKIP")
            if tv == "PASS":
                self.emit("TEST", f"{task.id}: teste local PASS (advisory)")
            elif tv == "FAIL":
                self.emit("TEST", f"{task.id}: teste local FAIL advisory — {test_oracle.get('note','')[:160]}")
            elif tv == "UNAVAILABLE":
                self.emit("TEST", f"{task.id}: teste local UNAVAILABLE ({test_oracle.get('note','')})")

        if review:
            decision = verify_policy.decide_review(
                critic, allow_unverified=self.settings.allow_unverified)
        else:
            decision = verify_policy.decide_build(
                oracle.passed, critic, visual=visual, tests=test_oracle,
                allow_unverified=self.settings.allow_unverified)
        verdict = decision.verdict
        passed = decision.deliver

        self.emit("ORACLE", oracle.render())
        cverdict = verify_policy.normalize_verdict(
            critic.get("verdict") if isinstance(critic, dict) else "UNAVAILABLE")
        if cverdict == verify_policy.PASS:
            self.emit("CRITIC", f"{task.id}: critic PASS ({critic.get('critic_family','?')})")
        elif cverdict == verify_policy.FAIL:
            self.emit("CRITIC", f"{task.id}: critic FAIL ({len(objections)} objection(s))")
            for ob in objections[:4]:
                self.emit("CRITIC", f"  · {str(ob.get('claim',''))[:160]}" if isinstance(ob, dict) else f"  · {str(ob)[:160]}")
        else:
            note = critic.get("note", "") if isinstance(critic, dict) else ""
            self.emit("CRITIC", f"{task.id}: critic UNVERIFIED ({note})")
            self._record("critic_unavailable", task=task.id, note=note[:200])

        record = VerificationRecord(
            verdict=verdict, criterion_ids=[],
            oracle={"passed": oracle.passed, "exit_code": oracle.exit_code},
            critic=critic if isinstance(critic, dict) else {},
            visual=visual if isinstance(visual, dict) else {},
            tests=test_oracle if isinstance(test_oracle, dict) else {},
            reason="; ".join(decision.reasons),
            at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        task.verification = record.to_dict()
        self._record("verification", task=task.id, verdict=verdict,
                     deliver=passed, reasons=decision.reasons[:4])
        feedback = self._feedback_text(oracle, critic, visual, test_oracle)
        if decision.reasons:
            feedback += "\nDELIVERY: " + "; ".join(decision.reasons)
        details = {"oracle": oracle, "critic": critic, "visual": visual,
                   "test_oracle": test_oracle, "objections": objections,
                   "verdict": verdict, "decision": decision.to_dict(),
                   "verification": record.to_dict()}
        return passed, feedback, details

    def _escalate(self, task: TaskSpec, artifact: str, producer: str,
                  reason: str, objections: list[Any]) -> bool:
        """Quality escalation: a stronger chain, not a technical fallback."""
        if producer == "ESCALATE":
            return False
        producer_ref = (ModelRef.from_dict(task.delivered_by)
                        if getattr(task, "delivered_by", None) else None)
        request = RouteRequest(
            role="fixer", complexity="high", escalation=True,
            required_capabilities=frozenset({"text"}),
            task_id=task.id, run_id=self.run_id or "", producer=producer_ref,
            tag="ESCALATE",
        )
        routed = self.pool.router.route(request)
        if not routed.candidates:
            self.emit("ESCALATE", f"{task.id}: sem candidatos de escalação")
            return False
        esc = routed.chain
        self.power.before_step(f"escalate {task.id}")
        self.emit("ESCALATE", f"{task.id}: falha de qualidade — a escalar para "
                              f"uma cadeia mais forte …")
        self._record("escalate", task=task.id, reason=reason[:160])
        feedback = reason
        if objections:
            feedback += "\n" + "\n".join(str(o.get('claim', '')) for o in objections if isinstance(o, dict))
        try:
            new_artifact = self.pool.complete(esc, [
                SystemMessage(GENERATOR_SYSTEM),
                HumanMessage(f"Original task:\n{self._build_prompt(task)}\n\n"
                             f"Previous failed artifact:\n```\n{artifact[:8000]}\n```\n\n"
                             f"Why it failed verification:\n{feedback}\n\n"
                             f"Produce a correct, complete solution."),
            ], meta={"run": self.run_id, "tag": "ESCALATE", "task": task.id})
        except Exception as e:
            self.emit("ESCALATE", f"{task.id}: escalação falhou: {str(e)[:120]}")
            return False
        self._save_artifact(task, new_artifact)
        self.power.before_step(f"escalate verify {task.id}")
        passed, _, details = self._check_artifact(task, new_artifact, "ESCALATE", self.run_id)
        if passed:
            task.status = "verified"
            written = self._deliver(task, new_artifact)
            critic = details.get("critic", {}) if isinstance(details.get("critic"), dict) else {}
            self.results[task.id] = {"status": "verified", "team": task.team,
                                     "file": str(written) if written else "",
                                     "chars": len(new_artifact),
                                     "critic": critic.get("verdict", "?"),
                                     "escalated": True,
                                     "verification": details.get("verification")}
            self._record("task_verified", task=task.id,
                         delivered=str(written) if written else None, escalated=True)
            self.emit("SYSTEM", f"✓ {task.id} verified (via escalação)")
            if written:
                self.emit("SYSTEM", f"{task.id} -> {written}")
            return True
        self.emit("ESCALATE", f"{task.id}: mesmo com a cadeia de escalação não passou")
        return False

    @staticmethod
    def _feedback_text(oracle: OracleResult, critic: dict[str, Any],
                       visual: Optional[dict[str, Any]] = None,
                       test_oracle: Optional[dict[str, Any]] = None) -> str:
        lines = [oracle.render()]
        if isinstance(critic, dict):
            for obj in critic.get("objections", []):
                if isinstance(obj, dict):
                    lines.append(f"CRITIC OBJECTION: {obj.get('claim')}\n  fix: {obj.get('how_to_test')}")
        if isinstance(visual, dict) and visual.get("verdict") == "FAIL":
            for obj in visual.get("objections", []):
                if isinstance(obj, dict):
                    lines.append(f"VISUAL PROBLEM: {obj.get('claim')}")
        if isinstance(test_oracle, dict) and test_oracle.get("verdict") == "FAIL":
            lines.append(f"LOCAL TEST FAILED:\n{test_oracle.get('note','')}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  Textual TUI
# --------------------------------------------------------------------------- #

@dataclass
class AgentCard:
    tag: str
    model: str = ""
    task: str = ""
    calls: int = 0
    in_tokens: int = 0
    out_tokens: int = 0
    errors: int = 0
    active_since: float = 0.0
    last_ms: int = 0

    @property
    def active(self) -> bool:
        return self.active_since > 0.0


@dataclass
class TaskCard:
    id: str
    title: str = ""
    team: str = ""
    status: str = "pending"
    producer: str = ""
    attempts: int = 0
    file: str = ""


STATUS_GLYPH: dict[str, tuple[str, str]] = {
    "pending": ("·", "grey50"),
    "running": ("▶", "yellow"),
    "built": ("◆", "cyan"),
    "verified": ("✓", "green"),
    "failed": ("✗", "red"),
    "failed-verification": ("✗", "red"),
    "skipped": ("–", "grey50"),
}

STATUS_SHORT = {
    "pending": "pend",
    "running": "corre",
    "built": "feito",
    "verified": "ok",
    "failed": "falhou",
    "failed-verification": "falhou",
    "skipped": "saltou",
}

AGENT_ORDER = ["FIELDMARSHAL", "ARCHITECT", "GEMINI", "REVIEW", "WORKER", "ESCALATE", "CRITIC"]
AGENT_SHORT = {"FIELDMARSHAL": "FIELD", "ARCHITECT": "ARCH", "ESCALATE": "ESC"}
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _hum(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}".rstrip("0").rstrip(".") + "k"
    return str(int(n))


class DashboardState:
    """Read-mostly projection of a run, fed by telemetry events (UI thread only)."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.phase = "BOOT"
        self.goal = ""
        self.project = ""
        self.run = ""
        self.agents: dict[str, AgentCard] = {}
        self.tasks: dict[str, TaskCard] = {}
        self.warnings: deque[tuple[str, str]] = deque(maxlen=8)
        self.calls = 0
        self.in_tokens = 0
        self.out_tokens = 0
        self.cost = Decimal("0")
        self.errors = 0
        self.tier_tokens: dict[str, int] = {k: 0 for k in TIER_LABEL}
        self.tier_cost: dict[str, Decimal] = {k: Decimal("0") for k in TIER_LABEL}
        self.oracle_runs = 0
        self.oracle_fails = 0
        self.critic_runs = 0
        self.critic_fails = 0
        self.critic_unavailable = 0
        self.loops = 0
        self.run_started = time.monotonic()

    # -- ingestion ---------------------------------------------------------- #

    def agent(self, tag: str) -> AgentCard:
        card = self.agents.get(tag)
        if card is None:
            card = AgentCard(tag=tag)
            self.agents[tag] = card
        return card

    def warn(self, kind: str, text: str) -> None:
        text = " ".join((text or "").split())
        if not text:
            return
        item = (kind, text[:170])
        if self.warnings and self.warnings[-1] == item:
            return
        self.warnings.append(item)

    def new_run(self, run_id: str, goal: str = "") -> None:
        self.run = run_id
        self.goal = goal or self.goal
        self.tasks.clear()
        self.calls = self.in_tokens = self.out_tokens = self.errors = 0
        self.cost = Decimal("0")
        self.tier_tokens = {k: 0 for k in TIER_LABEL}
        self.tier_cost = {k: Decimal("0") for k in TIER_LABEL}
        self.oracle_runs = self.oracle_fails = 0
        self.critic_runs = self.critic_fails = self.critic_unavailable = 0
        self.loops = 0
        self.warnings.clear()
        self.run_started = time.monotonic()

    def load_spec(self, spec: SpecContract) -> None:
        self.goal = spec.goal
        previous = self.tasks
        self.tasks = {}
        for task in spec.tasks:
            card = previous.get(task.id) or TaskCard(id=task.id)
            card.title = task.title
            card.team = task.team
            self.tasks[task.id] = card

    def on_event(self, e: dict[str, Any]) -> None:
        ev = str(e.get("event") or "")
        tag = str(e.get("tag") or "")
        if ev == "run_start":
            self.new_run(str(e.get("run") or ""))
        elif ev in ("llm_start", "llm_call", "llm_error"):
            card = self.agent(tag or "?")
            card.model = str(e.get("model") or card.model)
            card.task = str(e.get("task") or card.task)
            if ev == "llm_start":
                card.active_since = time.monotonic()
                return
            card.active_since = 0.0
            card.last_ms = int(e.get("ms") or card.last_ms or 0)
            if ev == "llm_error":
                card.errors += 1
                self.errors += 1
                self.warn(str(e.get("kind") or "erro"),
                          f"{tag or '?'} · {e.get('model') or card.model}: {e.get('error', '')}")
                return
            inc = int(e.get("in_tokens") or 0)
            out = int(e.get("out_tokens") or 0)
            card.calls += 1
            card.in_tokens += inc
            card.out_tokens += out
            billing = _event_billing(e)
            try:
                spend = Decimal(str(e.get("cost") or "0"))
            except (InvalidOperation, ValueError):
                spend = Decimal("0")
            if not bool(e.get("billable", billing == "payg")):
                spend = Decimal("0")
            if not bool(e.get("cost_known", True)):
                spend = Decimal("0")
            self.calls += 1
            self.in_tokens += inc
            self.out_tokens += out
            self.cost += spend
            self.tier_tokens[billing] = self.tier_tokens.get(billing, 0) + inc + out
            self.tier_cost[billing] = self.tier_cost.get(billing, Decimal("0")) + spend
        elif ev in ("task_start", "task_built", "task_rebuilt", "task_verified",
                    "task_unverified", "task_failed", "task_skipped"):
            tid = str(e.get("task") or "?")
            card = self.tasks.setdefault(tid, TaskCard(id=tid))
            card.team = str(e.get("team") or card.team)
            card.producer = str(e.get("producer") or card.producer)
            if ev == "task_start":
                card.status = "running"
            elif ev in ("task_built", "task_rebuilt"):
                card.status = "built"
                card.attempts = int(e.get("attempt") or card.attempts)
            elif ev == "task_verified":
                card.status = "verified"
                card.file = Path(str(e.get("delivered") or "")).name
            elif ev == "task_skipped":
                card.status = "skipped"
            else:
                card.status = "failed" if ev == "task_failed" else "failed-verification"
                self.warn("falha", f"{tid}: {e.get('reason') or e.get('error') or ''}")
        elif ev == "feedback_loop":
            self.loops += 1
        elif ev == "oracle":
            self.oracle_runs += 1
            if not e.get("passed", True):
                self.oracle_fails += 1
        elif ev == "critic":
            self.critic_runs += 1
            if str(e.get("verdict") or "").upper() == "FAIL":
                self.critic_fails += 1
        elif ev == "critic_unavailable":
            self.critic_unavailable += 1
            self.warn("crítico", f"{e.get('task')}: {e.get('note') or 'indisponível'}")
        elif ev == "run_done":
            for card in self.agents.values():
                card.active_since = 0.0

    # -- rendering ---------------------------------------------------------- #

    def render_head(self) -> Text:
        now = time.monotonic()
        elapsed = now - self.run_started if self.run else 0.0
        out = Text()
        out.append(f" {self.phase} ", style="bold reverse")
        out.append("  ")
        out.append(self.run or "sem run", style="grey62")
        out.append("  ")
        out.append(time.strftime("%H:%M:%S"), style="grey62")
        if self.run:
            out.append(f"  +{int(elapsed // 60):02d}:{int(elapsed % 60):02d}", style="grey50")
        if self.calls:
            out.append(f"  {self.calls} calls", style="grey50")
            if self.errors:
                out.append(f" {self.errors} err", style="red")
        if self.project:
            out.append(f"  {self.project}", style="cyan")
        if self.goal:
            out.append("  · ", style="grey50")
            out.append(self.goal[:90], style="white")
        return out

    def render_agents(self) -> Table:
        table = Table(box=None, expand=True, padding=(0, 1), header_style="bold grey58")
        table.add_column("agente", width=8, no_wrap=True)
        table.add_column("modelo", ratio=1, no_wrap=True, overflow="ellipsis")
        table.add_column("tok", width=6, justify="right", no_wrap=True)
        table.add_column("t", width=6, justify="right", no_wrap=True)
        tags = [t for t in AGENT_ORDER if t in self.agents]
        tags += [t for t in self.agents if t not in AGENT_ORDER]
        if not tags:
            table.add_row(Text("à espera de trabalho…", style="grey50"), "", "", "")
            return table
        for i, tag in enumerate(tags):
            card = self.agents[tag]
            if card.active:
                state = Text(SPINNER[int(time.monotonic() * 8) % len(SPINNER)],
                             style="bold yellow")
            elif card.errors:
                state = Text("!", style="red")
            elif card.calls:
                state = Text("·", style="green")
            else:
                state = Text("·", style="grey42")
            name = Text()
            name.append_text(state)
            name.append(" ")
            name.append(AGENT_SHORT.get(tag, tag), style="bold " + COLORS.get(tag, "white"))
            tokens = card.in_tokens + card.out_tokens
            if card.active:
                timing = f"{int(time.monotonic() - card.active_since)}s"
            elif card.last_ms:
                timing = f"{card.last_ms / 1000:.1f}s"
            else:
                timing = "—"
            table.add_row(name, Text(card.model or "—", style="grey62"),
                          Text(_hum(tokens) if tokens else "—", style="grey70"),
                          Text(timing, style="grey50"))
        return table

    def render_tasks(self) -> Table:
        table = Table(box=None, expand=True, padding=(0, 1), header_style="bold grey58")
        table.add_column("#", width=3, no_wrap=True)
        table.add_column("tarefa", ratio=1, no_wrap=True, overflow="ellipsis")
        table.add_column("estado", width=9, no_wrap=True)
        table.add_column("por", width=6, no_wrap=True, overflow="ellipsis")
        if not self.tasks:
            table.add_row(Text("sem spec ainda", style="grey50"), "", "", "")
            return table
        for tid, card in self.tasks.items():
            glyph, style = STATUS_GLYPH.get(card.status, ("?", "white"))
            state = Text(f"{glyph} {STATUS_SHORT.get(card.status, card.status)}", style=style)
            if card.attempts:
                state.append(f"·{card.attempts}", style="grey50")
            title = card.title or card.id
            table.add_row(Text(tid, style="grey62"), Text(title, style="white"),
                          state, Text(card.producer or card.team or "—", style="grey62"))
        return table

    def render_metrics(self) -> Table:
        table = Table(box=None, expand=True, padding=(0, 1), show_header=False)
        table.add_column("m", width=7, style="grey58", no_wrap=True)
        table.add_column("v", ratio=1, justify="right", no_wrap=True, overflow="ellipsis")

        total_tok = self.in_tokens + self.out_tokens
        table.add_row("tokens", Text(f"{_hum(total_tok)}  {_hum(self.in_tokens)}↓{_hum(self.out_tokens)}↑",
                                     style="bold white"))
        payg = self.tier_cost.get("payg", Decimal("0"))
        quota = self.tier_tokens.get("subscription", 0) + self.tier_tokens.get("free_quota", 0)
        detail = f"payg {payg:.4f}"
        if quota:
            detail += f" · quota {_hum(quota)}"
        table.add_row("custo", Text(f"${self.cost:.4f}  {detail}", style="green"))
        done = sum(1 for t in self.tasks.values() if t.status == "verified")
        bad = sum(1 for t in self.tasks.values()
                  if t.status in ("failed", "failed-verification"))
        table.add_row("tarefas", Text(f"{done}/{len(self.tasks)} · {bad}✗ · {self.loops}↻",
                                      style="red" if bad else "white"))
        table.add_row("verif", Text(f"o {self.oracle_fails}/{self.oracle_runs}  "
                                    f"c {self.critic_fails}/{self.critic_runs}  "
                                    f"{self.errors}err",
                                    style="red" if self.errors else "white"))
        table.add_row("mix", self.render_tier_bar())
        return table

    def render_warnings(self) -> Text:
        out = Text()
        if not self.warnings:
            out.append("sem avisos", style="grey42")
            return out
        for kind, text in list(self.warnings)[-3:]:
            style = "red" if kind in ("429/quota", "auth", "falha", "timeout", "offline") else "yellow"
            out.append("⚠ ", style=style)
            out.append(f"{kind}: ", style="bold " + style)
            out.append(text + "\n", style="grey70")
        return out

    def render_tier_bar(self) -> Text:
        total = sum(self.tier_tokens.values()) or 1
        out = Text()
        for tier, letter in (("local", "L"), ("free_quota", "Q"),
                             ("subscription", "S"), ("payg", "P"),
                             ("unknown", "U")):
            n = self.tier_tokens.get(tier, 0)
            if not n:
                continue
            pct = n * 100 // total
            out.append(f"{letter}{pct}% ", style=TIER_STYLE[tier])
        return out or Text("—", style="grey42")


class BudgetOverrideModal(ModalScreen[Optional[str]]):
    """Human-in-the-loop authorization for a pay-as-you-go model.

    Dismisses with the chosen model id, or ``None`` to keep the breaker closed.
    """

    CSS = """
    BudgetOverrideModal { align: center middle; }
    #budget-dialog {
        width: 64;
        height: auto;
        padding: 2;
        border: thick $warning;
        background: $surface;
    }
    #budget-title { text-style: bold; color: $warning; margin-bottom: 1; }
    #budget-reason { margin-bottom: 1; color: $text-muted; }
    #budget-select { margin-bottom: 1; }
    #budget-buttons { height: auto; align: right middle; }
    #budget-buttons Button { margin-left: 1; }
    """

    BINDINGS = [("escape", "cancel", "Cancelar")]

    def __init__(self, reason: str, options: list[str]):
        super().__init__()
        self.reason = reason
        self.options = options

    def compose(self) -> ComposeResult:
        with Vertical(id="budget-dialog"):
            yield Label("⚠ Circuit breaker financeiro", id="budget-title")
            yield Static(self.reason, id="budget-reason")
            yield Static("Escolhe o modelo pago com que queres continuar:")
            yield Select([(m, m) for m in self.options], id="budget-select",
                         value=self.options[0] if self.options else Select.BLANK)
            with Horizontal(id="budget-buttons"):
                yield Button("Autorizar para esta tarefa", id="budget-ok", variant="warning")
                yield Button("Cancelar", id="budget-cancel", variant="error")

    @on(Button.Pressed, "#budget-ok")
    def _ok(self) -> None:
        value = self.query_one("#budget-select", Select).value
        self.dismiss(value if isinstance(value, str) else None)

    @on(Button.Pressed, "#budget-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class CrewTUIApp(App):
    TITLE = "Agent Pool — Dashboard"
    SUB_TITLE = "SPEC → BUILD → VERIFY"

    CSS = """
    #dash-head {
        height: 1;
        padding: 0 1;
        background: $boost;
        color: $text;
    }
    #dash-row {
        height: 1fr;
        min-height: 7;
    }
    .panel {
        border: round $primary-darken-1;
        padding: 0 1;
    }
    #panel-agents { width: 1fr; }
    #panel-tasks { width: 1fr; }
    #bottom-row {
        height: 8;
    }
    #panel-metrics { width: 1fr; }
    #panel-warn { width: 2fr; }
    .panel-title {
        height: 1;
        color: $text-muted;
        text-style: bold;
    }
    #dash-agents, #dash-tasks, #dash-metrics, #dash-warn {
        height: 1fr;
    }
    #progress {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    #log {
        height: 8;
        padding: 0 1;
        border-top: solid $primary-darken-2;
    }
    #input {
        dock: bottom;
    }
    #button-bar {
        dock: bottom;
        height: 3;
        padding: 0 1;
        background: $surface;
    }
    #button-bar Button {
        margin-right: 2;
        min-width: 10;
    }
    """

    BINDINGS = [("ctrl+q", "quit", "Quit")]

    def __init__(self):
        super().__init__()
        self.settings = Settings.from_env()
        self.pool: Optional[LLMPool] = None
        self.orchestrator: Optional[Orchestrator] = None
        self.phase = "BOOT"
        self.dash = DashboardState(self.settings)
        self._busy = False
        self._progress_text = ""
        self.factory: Optional[Any] = None
        self.engine: Optional[Any] = None
        self._factory_mode = False

    # -- lifecycle ---------------------------------------------------------- #

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static("", id="dash-head")
        with Horizontal(id="dash-row"):
            with Vertical(classes="panel", id="panel-agents"):
                yield Static("AGENTES", classes="panel-title")
                yield Static("", id="dash-agents")
            with Vertical(classes="panel", id="panel-tasks"):
                yield Static("TAREFAS", classes="panel-title")
                yield Static("", id="dash-tasks")
        with Horizontal(id="bottom-row"):
            with Vertical(classes="panel", id="panel-metrics"):
                yield Static("MÉTRICAS", classes="panel-title")
                yield Static("", id="dash-metrics")
            with Vertical(classes="panel", id="panel-warn"):
                yield Static("AVISOS", classes="panel-title")
                yield Static("", id="dash-warn")
        yield Static("", id="progress")
        yield RichLog(highlight=True, markup=False, wrap=True, id="log", max_lines=500)
        with Horizontal(id="button-bar"):
            yield Button("▶ Go", id="btn-go", variant="success")
            yield Button("↺ Resume", id="btn-resume", variant="primary")
        yield Input(placeholder="Descreve o que queres (ex.: 'script que valida emails')…", id="input")
        yield Footer()

    @on(Button.Pressed)
    def _on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-go":
            if self.phase in ("AWAIT", "PLAN"):
                if self._factory_mode and self.factory is not None:
                    self._write(tagged("SYSTEM", "go recebido (botão) — a executar passos 2-9 …", bold=True))
                    self._set_phase("BUILD")
                    self._busy = True
                    self._run_factory_build()
                else:
                    self._write(tagged("SYSTEM", "go recebido (botão) — a construir o plano aprovado …", bold=True))
                    self._set_phase("BUILD")
                    self._busy = True
                    self._run_execute()
            else:
                self._write(tagged("SYSTEM", "Botão Go ativo apenas na fase PLAN."))
        elif event.button.id == "btn-resume":
            self._start_factory("", resume=True)

    def on_mount(self) -> None:
        try:
            self.query_one("#input", Input).focus()
        except Exception:
            pass
        self._write(tagged("SYSTEM", "booting agent pool …", bold=True))
        self.telemetry = Telemetry(self.settings)
        self.telemetry.subscribe(self._on_telemetry)
        self.pool = LLMPool(self.settings, sink_factory=self._sink_factory,
                            telemetry=self.telemetry, progress_factory=self._progress_factory)
        self.pool.on_budget = self._budget_override
        self.orchestrator = Orchestrator(self.settings, self.pool, emit=self._emit_ctrl, telemetry=self.telemetry)
        if self.orchestrator.load_session():
            self._write(tagged("SYSTEM", "sessão anterior recuperada:", bold=True))
            for line in self.orchestrator.summary().splitlines():
                self._write(tagged("SYSTEM", line))
        self.set_interval(0.8, self._tick)
        self._refresh_dashboard()
        self._boot()

    @work(thread=True)
    def _boot(self) -> None:
        info = self.pool.discover()
        self.call_from_thread(self._boot_done, info)

    def _boot_done(self, info: dict[str, Any]) -> None:
        self._write(tagged("SYSTEM", f"discovery: {self.pool.status_summary()}"))
        if info.get("local_up"):
            self._write(tagged("WORKER", f"local Qwen online ({self.settings.local_model_name})"))
        else:
            self._write(tagged("WORKER", f"local Qwen DOWN → falling back to cloud '{self.settings.beam_free_model}'"))
            self.dash.warn("local down", f"Qwen offline → fallback '{self.settings.beam_free_model}'")
        if not self.pool.gemini_enabled:
            self.dash.warn("gemini off", "GEMINI_API_KEY ausente (sem gerador/crítico grátis)")
        if not self.pool.cloud_enabled:
            self.dash.warn("cloud off", "OPENCODE_API_KEY ausente (sem fallback pago)")
        for m in info.get("cloud_models", [])[:20]:
            self._write(tagged("SYSTEM", f"  cloud model: {m}"))
        self._write(tagged("SYSTEM", "ready. Type an instruction and press Enter.", bold=True))
        self.phase = "IDLE"
        self._refresh_dashboard()

    # -- dashboard refresh -------------------------------------------------- #

    def _on_telemetry(self, entry: dict[str, Any]) -> None:
        if threading.current_thread() is threading.main_thread():
            self.dash.on_event(entry)
        else:
            try:
                self.call_from_thread(self.dash.on_event, entry)
            except Exception:
                pass

    def _tick(self) -> None:
        self._refresh_dashboard()

    def _refresh_dashboard(self) -> None:
        self.dash.phase = self.phase
        try:
            self.query_one("#dash-head", Static).update(self.dash.render_head())
            self.query_one("#dash-agents", Static).update(self.dash.render_agents())
            self.query_one("#dash-tasks", Static).update(self.dash.render_tasks())
            self.query_one("#dash-metrics", Static).update(self.dash.render_metrics())
            self.query_one("#dash-warn", Static).update(self.dash.render_warnings())
        except Exception:
            return
        self._render_progress()

    def _render_progress(self) -> None:
        try:
            if self._progress_text:
                glyph = SPINNER[int(time.monotonic() * 8) % len(SPINNER)]
                label = f"{glyph} {self.phase} · {self._progress_text}"
            else:
                label = f"{self.phase} · {'a trabalhar…' if self._busy else 'pronto'}"
            self.query_one("#progress", Static).update(label)
        except Exception:
            pass

    # -- UI helpers (called on the event-loop thread) ----------------------- #

    def _sink_factory(self, tag: str) -> Callable[[Text], None]:
        return lambda renderable: self.call_from_thread(self._write, renderable)

    def _progress_factory(self, tag: str) -> Callable[[str, int], None]:
        return lambda t, chars: self.call_from_thread(self._set_progress, t, chars)

    def _set_progress(self, tag: str, chars: int) -> None:
        self._progress_text = f"{tag}: a gerar… {chars} chars" if chars else f"{tag}: a pensar…"
        self._render_progress()

    def _clear_progress(self) -> None:
        self._progress_text = ""
        self._render_progress()

    def _write(self, renderable: Text) -> None:
        try:
            self.query_one("#log", RichLog).write(renderable)
        except Exception:
            pass

    def _emit_ctrl(self, tag: str, text: str) -> None:
        self.call_from_thread(self._write, tagged(tag, text))

    def _budget_override(self, reason: str, options: list[str]) -> Optional[str]:
        """Block a worker thread until the human authorizes (or refuses) paid use.

        Called from the agent worker; the modal lives on the Textual loop, so
        we schedule it with ``call_from_thread`` and park on an Event.
        """
        if not options:
            return None
        if getattr(self, "_budget_modal_open", False):
            return None
        box: dict[str, Optional[str]] = {"value": None}
        done = threading.Event()

        def show() -> None:
            self._budget_modal_open = True

            def chosen(value: Optional[str]) -> None:
                box["value"] = value
                self._budget_modal_open = False
                done.set()

            self.push_screen(BudgetOverrideModal(reason, options), chosen)

        try:
            self.call_from_thread(show)
        except Exception:
            return None
        done.wait()
        if box["value"]:
            self._write(tagged("SYSTEM", f"breaker: autorizado {box['value']}", bold=True))
        else:
            self._write(tagged("SYSTEM", "breaker: override recusado — execução parada", bold=True))
        return box["value"]

    def _refresh_status(self) -> None:
        self._refresh_dashboard()

    def _set_phase(self, phase: str) -> None:
        self.phase = phase
        self._refresh_dashboard()

    # -- software factory (steps 1-9) --------------------------------------- #

    FACTORY_RE = re.compile(r"^/(?:factory|fabrica)\b\s*(.*)$", re.I)

    def _factory_project(self, goal: str) -> Path:
        root = self.orchestrator._find_dir_in(goal) if self.orchestrator else None
        if root is None and self.orchestrator is not None and self.orchestrator.last_project_root:
            root = self.orchestrator.last_project_root
        if root is None:
            slug = re.sub(r"[^a-z0-9]+", "_", goal.lower()).strip("_")[:40] or "projeto"
            root = Path(os.path.expanduser(self.settings.project_dir)) / slug
        root = Path(root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _factory_emit(self, tag: str, text: str) -> None:
        self._emit_ctrl(tag, text)
        self.call_from_thread(self._refresh_factory_dash)

    def _refresh_factory_dash(self) -> None:
        try:
            if self.factory is None or not self.factory.state.tasks:
                return
            from types import SimpleNamespace
            tasks = [SimpleNamespace(id=t.id, title=t.title, team=t.role, status=t.status)
                     for t in self.factory.state.tasks]
            self.dash.load_spec(SimpleNamespace(goal=self.factory.state.goal, tasks=tasks))
            self._refresh_dashboard()
        except Exception:
            pass

    def _start_factory(self, goal: str, resume: bool = False) -> None:
        store = StateManager(self.settings.workspace_dir, self.settings.log_dir)
        root = self._factory_project(goal)
        state = None
        kind = None
        persistence = "crew"
        if resume:
            try:
                target = store.resolve_resume(root)
            except StateCorruptError as exc:
                self._write(tagged("SYSTEM", str(exc), bold=True))
                self._write(tagged("SYSTEM", "estado preservado; corrige/migra antes de retomar.",
                                   bold=True))
                return
            except StateError as exc:
                self._write(tagged("SYSTEM", str(exc), bold=True))
                self._write(tagged("SYSTEM", "corre --migrate-schema ou --migrate-engine.",
                                   bold=True))
                return
            if target is not None and target.project_state is not None:
                state = target.project_state
                kind = target.engine_kind
                persistence = "crew"
            elif (target is not None and target.legacy_raw is not None
                  and target.path == store.legacy_factory_path(root)):
                if target.engine_kind == ENGINE_UNIFIED:
                    self._write(tagged("SYSTEM", "estado .factory inconsistente (unified)", bold=True))
                    return
                state = from_legacy_factory(target.legacy_raw, engine_kind=ENGINE_LEGACY)
                kind = ENGINE_LEGACY
                persistence = "factory"
        if kind is None and not resume:
            try:
                kind = resolve_engine()
            except EngineError:
                kind = ENGINE_LEGACY
            persistence = "factory" if kind == ENGINE_LEGACY else "crew"
            if kind == ENGINE_LEGACY and store.project_state_path(root).is_file():
                self._write(tagged("SYSTEM", "existe .crew neste projeto; usa /resume "
                                             "ou --migrate-engine antes de iniciar legacy.", bold=True))
                return

        if state is None:
            if kind == ENGINE_LEGACY:
                state = ProjectState(run_id=new_id("run_"), project_root=str(root),
                                     goal=goal or "projeto", engine_kind=ENGINE_LEGACY,
                                     engine_version="legacy", phase="SPEC")
            else:
                state = new_unified_state(goal or "projeto", str(root),
                                          engine_version=ENGINE_VERSION,
                                          profile=self.settings.workflow_profile)
        if not state.project_root:
            state.project_root = str(root)
        adapter = FactoryAdapter(self.settings, self.pool, self._factory_emit, state,
                                 legacy_order=(kind == ENGINE_LEGACY),
                                 isolate_workspace=(kind == ENGINE_UNIFIED))
        eng = WorkflowEngine(state, store, adapter, emit=self._factory_emit,
                             allow_unknown=self.settings.allow_unknown,
                             persistence=persistence)
        eng.apply_pool(self.pool)
        eng.save()
        self.engine = eng
        self.factory = EngineFacade(eng, store)
        self._factory_mode = True
        self._write(tagged("CREW", f"projeto: {root} · motor: {kind} · estado: {persistence}",
                           bold=True))
        if resume and self.factory.state.requirements:
            self._write(tagged("SYSTEM", f"a retomar do passo {self.factory.state.step} …"))
            self._set_phase("BUILD")
            self._busy = True
            self._run_factory_build()
            return
        self._set_phase("SPEC")
        self._busy = True
        self._run_factory_prepare(goal)

    @work(thread=True, group="agent")
    def _run_factory_prepare(self, goal: str, feedback: str = "") -> None:
        try:
            req = self.factory.prepare(goal, feedback)
            self.call_from_thread(self._present_factory_reqs, req)
        except Exception as e:
            self._fail(f"factory (requirements): {e}")

    @work(thread=True, group="agent")
    def _run_factory_refine(self, feedback: str) -> None:
        try:
            req = self.factory.prepare(self.factory.state.goal, feedback)
            self.call_from_thread(self._present_factory_reqs, req)
        except Exception as e:
            self._fail(f"factory (refine): {e}")

    @work(thread=True, group="agent")
    def _run_factory_build(self) -> None:
        try:
            ok = self.factory.build()
            self.call_from_thread(self._factory_done, ok)
        except Exception as e:
            self._fail(f"factory (build): {e}")

    def _present_factory_reqs(self, req: dict[str, Any]) -> None:
        self._clear_progress()
        self._write(tagged("ANALYST", "contrato de requisitos:", bold=True))
        self._write(tagged("ANALYST", f"objetivo: {req.get('objective', '')}"))
        for key, label in (("scope_in", "no âmbito"), ("scope_out", "fora do âmbito"),
                           ("constraints", "restrições"), ("acceptance", "aceite"),
                           ("assumptions", "pressupostos")):
            items = req.get(key) or []
            if items:
                self._write(tagged("PLAN", f"{label}: " + "; ".join(str(i) for i in items)[:600]))
        qs = req.get("open_questions") or []
        if qs:
            for i, q in enumerate(qs, 1):
                self._write(tagged("SYSTEM", f"  {i}) {q}"))
            self._write(tagged("SYSTEM", "responde numa linha, ou 'go' para arrancar já.", bold=True))
        else:
            self._write(tagged("SYSTEM", "sem perguntas. Escreve 'go' para arrancar.", bold=True))
        self._set_phase("PLAN")
        self._busy = False

    def _factory_done(self, ok: bool) -> None:
        pause = getattr(self.factory, "pause_reason", "") if self.factory else ""
        if not ok and pause == "architecture_approval":
            self._clear_progress()
            self._busy = False
            self._set_phase("PLAN")
            arch = getattr(self.factory, "state", None)
            self._write(tagged("SYSTEM", "arquitetura pronta — revê os módulos/interfaces.",
                               bold=True))
            if arch is not None:
                self._write(tagged("ARCHITECT", f"{len(arch.tasks)} tarefa(s) definidas."))
            self._write(tagged("SYSTEM", "escreve 'go' para APROVAR a arquitetura e continuar.",
                               bold=True))
            return
        if not ok and pause.endswith("_failed"):
            self._clear_progress()
            self._busy = False
            self._set_phase("PLAN")
            self._write(tagged("SYSTEM", f"parou em {pause}: vê o log e responde com feedback.",
                               bold=True))
            return
        self._clear_progress()
        self._busy = False
        self._factory_mode = False
        self._set_phase("CHAT")
        if self.factory is None:
            return
        st = self.factory.state
        done = sum(1 for t in st.tasks if t.status == "done")
        self._write(tagged("CREW", f"passo final: {st.step} · {done}/{len(st.tasks)} tarefas concluídas",
                           bold=True))
        integ = st.integration.get("verdict") if st.integration else None
        vis = st.visual.get("verdict") if st.visual else None
        if integ or vis:
            self._write(tagged("CREW", f"integração: {integ or '—'} · visual: {vis or '—'}"))
        for t in st.tasks:
            if t.status != "done":
                self._write(tagged("SYSTEM", f"  {t.id} {t.target_file}: {t.status}"
                                             + (f" — {t.errors[-1][:160]}" if t.errors else "")))
        mem = self.factory.root / "project_memory.json"
        if mem.is_file():
            self._write(tagged("SIGN", f"memória: {mem}"))
        self._write(tagged("SYSTEM", "pipeline terminada. Podes pedir correções ou perguntar o que falhou.",
                           bold=True))

    # -- input handling ----------------------------------------------------- #

    @on(Input.Submitted)
    def _on_submit(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            if self.phase in ("AWAIT", "PLAN"):
                self._write(tagged("SYSTEM", "escreve 'go' para arrancar, responde a uma pergunta, "
                                             "ou dá feedback para refinar o plano.", bold=True))
            return
        if self._busy:
            self._write(tagged("SYSTEM", "a run is already in progress …"))
            return

        if text.lower().strip() in ("/resume", "/retomar"):
            self._start_factory("", resume=True)
            return
        m = self.FACTORY_RE.match(text)
        if m:
            goal = m.group(1).strip()
            self._write(tagged("SYSTEM", "/factory está deprecated: escreve o objetivo "
                                         "diretamente (entrada única do Crew).", bold=True))
            if not goal:
                self._write(tagged("SYSTEM", "ex.: 'cria uma API de tarefas em ~/projetos/api'"))
                return
            self._start_factory(goal)
            return

        if self.phase in ("AWAIT", "PLAN"):
            if self._factory_mode and self.factory is not None:
                if text.lower().strip(" .!") in APPROVE_KEYWORDS:
                    self._write(tagged("SYSTEM", "go recebido — a executar as fases do motor "
                                                 "(SPEC → ARCHITECTURE → SCAFFOLD → BUILD → "
                                                 "VERIFY → INTEGRATE → VISUAL → SIGN-OFF) …",
                                       bold=True))
                    self._set_phase("BUILD")
                    self._busy = True
                    self._run_factory_build()
                else:
                    self._write(tagged("SYSTEM", f"resposta/feedback: {text}"))
                    self._set_phase("PLAN")
                    self._busy = True
                    self._run_factory_refine(text)
                return
            if text.lower().strip(" .!") in APPROVE_KEYWORDS:
                review = bool(self.orchestrator and self.orchestrator.spec
                              and self.orchestrator.spec.review)
                self._write(tagged("SYSTEM", "go recebido — a analisar o projeto (read-only) …"
                                   if review else
                                   "go recebido — a construir o plano aprovado …", bold=True))
                self._set_phase("REVIEW" if review else "BUILD")
                self._busy = True
                self._run_execute()
            else:
                self._write(tagged("SYSTEM", f"resposta: {text}"))
                self._write(tagged("SYSTEM", "a refinar o plano e as perguntas em aberto …"))
                self._set_phase("PLAN")
                self._busy = True
                self._run_refine(text)
        else:
            self._write(tagged("SYSTEM", f"> {text}"))
            self._clear_progress()
            self._busy = True
            if self.orchestrator is not None and self.orchestrator.last_goal:
                if self._looks_like_question(text):
                    self._set_phase("CHAT")
                    self._run_answer(text)
                    return
            # Transitional read-only profile; the build path is the single
            # Crew entry below.
            if REVIEW_RE.search(text):
                continues = self._continues_last_run(text)
                self._set_phase("SPEC")
                self._run_spec(text, continues)
                return
            if self.NEW_PROJECT_RE.search(text) and self.orchestrator is not None:
                self.orchestrator.last_project_root = None
            self._start_factory(text)

    QUESTION_RE = re.compile(
        r"^\s*(o que|que (fizeste|faz|está|houve|se passou)|porque|por que|onde|quando|"
        r"como (ficou|está|funciona)|quem|qual|quais|explica|mostra|diz-me|what|why|where|how)", re.I)
    NEW_PROJECT_RE = re.compile(r"\b(novo projeto|outro projeto|de raiz|from scratch|começa do zero)\b", re.I)

    def _looks_like_question(self, text: str) -> bool:
        return text.strip().endswith("?") or bool(self.QUESTION_RE.match(text))

    def _continues_last_run(self, text: str) -> bool:
        if self.NEW_PROJECT_RE.search(text):
            self.orchestrator.last_goal = ""
            return False
        d = self.orchestrator._find_dir_in(text) if self.orchestrator else None
        if d is not None and self.orchestrator.last_project_root is not None:
            try:
                return d.resolve() == self.orchestrator.last_project_root.resolve()
            except Exception:
                return False
        return True

    # -- background workers ------------------------------------------------- #

    @work(thread=True, group="agent")
    def _run_spec(self, instruction: str, continues: bool = False) -> None:
        try:
            spec = self.orchestrator.draft_spec(instruction, continues=continues)
            self.call_from_thread(self._present_spec, spec)
        except Exception as e:
            self._fail(f"spec error: {e}")

    @work(thread=True, group="agent")
    def _run_answer(self, question: str) -> None:
        try:
            reply = self.orchestrator.answer(question)
            self.call_from_thread(self._show_answer, reply)
        except Exception as e:
            self._fail(f"chat error: {e}")

    def _show_answer(self, reply: str) -> None:
        for line in reply.splitlines():
            self._write(tagged("CHAT", line))
        self._clear_progress()
        self._set_phase("CHAT")
        self._busy = False

    @work(thread=True, group="agent")
    def _run_refine(self, feedback: str) -> None:
        try:
            base = self.orchestrator.instruction or self.orchestrator.spec.goal
            spec = self.orchestrator.draft_spec(base, feedback)
            self.call_from_thread(self._present_spec, spec)
        except Exception as e:
            self._fail(f"refine error: {e}")

    @work(thread=True, group="agent")
    def _run_execute(self) -> None:
        try:
            self.orchestrator.execute()
            self.call_from_thread(self._finish_run)
        except Exception as e:
            self._fail(f"run error: {e}")

    # -- publish results to the UI ----------------------------------------- #

    def _present_spec(self, spec: SpecContract) -> None:
        self._clear_progress()
        self.dash.load_spec(spec)
        self._write(tagged("ARCHITECT", "plano proposto (revê antes de arrancar):", bold=True))
        for line in spec.plan().splitlines():
            tag = "ARCHITECT" if not line.startswith("  ") else "PLAN"
            self._write(tagged(tag, line))
        n = len(spec.questions)
        if n:
            hint = (f"{n} pergunta(s) em aberto. Responde numa linha (podes responder às restantes "
                    "nas submissões seguintes), escreve 'go' para arrancar já, ou dá feedback.")
        else:
            hint = "Sem perguntas em aberto. Escreve 'go' para arrancar ou dá feedback para ajustar."
        self._write(tagged("SYSTEM", hint, bold=True))
        self._set_phase("PLAN")
        self._busy = False

    def _finish_run(self) -> None:
        self._clear_progress()
        self._set_phase("CHAT")
        self._busy = False
        failed = [tid for tid, res in self.orchestrator.results.items()
                  if res.get("status") not in ("verified", "verified-degraded", "skipped")]
        if failed:
            self._write(tagged("SYSTEM", f"run concluída com {len(failed)} tarefa(s) não verificada(s). Podes perguntar o que falhou.", bold=True))
        else:
            self._write(tagged("SYSTEM", "run terminada com sucesso. Podes pedir correções, perguntar o que fiz, "
                                         "ou dar uma nova instrução.", bold=True))

    def _fail(self, message: str) -> None:
        self.call_from_thread(self._clear_progress)
        self.call_from_thread(self._write, tagged("SYSTEM", message, bold=True))
        self.call_from_thread(self._set_phase, "IDLE")
        self.call_from_thread(self._clear_busy)

    def _clear_busy(self) -> None:
        self._busy = False


def _legacy_factory_cli(args: Any, settings: Any = None, store: Any = None) -> int:
    """Headless entrypoint: ``crew-tui --factory GOAL [--project DIR] [--yes|--resume]``."""
    from factory import Factory

    if settings is None:
        settings = Settings.from_env()
    settings.unattended = bool(getattr(args, "unattended", False))
    if getattr(args, "allow_paid", False):
        settings.allow_paid = True
    if getattr(args, "allow_unknown", False):
        settings.allow_unknown = True
    if getattr(args, "allow_unverified", False):
        settings.allow_unverified = True
    if getattr(args, "profile", None):
        settings.workflow_profile = str(args.profile)
    if settings.unattended:
        settings.sleep_if_active = True
    telemetry = Telemetry(settings)
    emit = lambda tag, text: print(f"[{tag}] {text}", flush=True)
    pool = LLMPool(settings, sink_factory=lambda tag: (lambda r: None),
                   telemetry=telemetry, progress_factory=lambda tag: (lambda t, c: None))
    pool.attempt_sink = None
    print("discover:", pool.status_summary(), flush=True)
    pool.discover()

    root = Path(os.path.expanduser(args.project)) if args.project else None
    if root is None:
        slug = re.sub(r"[^a-z0-9]+", "_", (args.factory or "projeto").lower()).strip("_")[:40] or "projeto"
        root = Path(os.path.expanduser(settings.project_dir)) / slug
    root.mkdir(parents=True, exist_ok=True)
    fac = Factory(settings, pool, emit=emit, project_root=root, telemetry=telemetry)

    if args.resume:
        if not fac.state.requirements:
            print(f"nada para retomar em {fac.state_path}", flush=True)
            return 2
        print(f"a retomar do passo {fac.state.step} ({fac.root})", flush=True)
    else:
        if not args.factory:
            print("uso: crew-tui --factory \"objetivo\" [--project DIR] [--yes]", flush=True)
            return 2
        req = fac.prepare(args.factory)
        print(json.dumps(req, ensure_ascii=False, indent=2), flush=True)
        questions = req.get("open_questions") or []
        if questions and not args.yes:
            for q in questions:
                try:
                    ans = input(f"Q: {q}\nA: ").strip()
                except EOFError:
                    ans = ""
                if ans:
                    fac.answer(q, ans)
            if fac.state.answers:
                req = fac.prepare(args.factory)
                print(json.dumps(req, ensure_ascii=False, indent=2), flush=True)
    ok = fac.build()
    print(("PASS" if ok else "FAIL") + f" · passo {fac.state.step} · {fac.root}", flush=True)
    print(f"estado: {fac.state_path}", flush=True)
    return 0 if ok else 1


def _engine_cli_run(args: Any, kind: str, settings: Any, store: StateManager,
                    state: Any = None, persistence: str = "crew") -> int:
    """Run the unified/legacy engine with canonical .crew persistence."""
    telemetry = Telemetry(settings)
    emit = lambda tag, text: print(f"[{tag}] {text}", flush=True)
    pool = LLMPool(settings, sink_factory=lambda tag: (lambda r: None),
                   telemetry=telemetry, progress_factory=lambda tag: (lambda t, c: None))
    print("discover:", pool.status_summary(), flush=True)
    pool.discover()

    root = Path(os.path.expanduser(args.project)) if args.project else None
    if state is not None and state.project_root:
        root = Path(state.project_root)
    if root is None:
        goal = args.factory or (state.goal if state is not None else "projeto")
        slug = re.sub(r"[^a-z0-9]+", "_", goal.lower()).strip("_")[:40] or "projeto"
        root = Path(os.path.expanduser(settings.project_dir)) / slug
    root.mkdir(parents=True, exist_ok=True)

    if state is None:
        if not args.factory:
            print("uso: crew-tui --factory \"objetivo\" [--project DIR]", flush=True)
            return 2
        state = new_unified_state(args.factory, str(root),
                                  engine_version=ENGINE_VERSION,
                                  profile=settings.workflow_profile)
        state.engine_kind = kind
        if kind == ENGINE_LEGACY:
            state.engine_version = "legacy"
    else:
        kind = state.engine_kind
        if not state.project_root:
            state.project_root = str(root)

    env_kind = resolve_engine()
    if args.resume and env_kind != kind:
        print(f"aviso: CREW_ENGINE={env_kind} ignorado no resume; "
              f"o estado usa engine_kind={kind}", flush=True)

    adapter = FactoryAdapter(settings, pool, emit, state,
                             legacy_order=(kind == ENGINE_LEGACY),
                             isolate_workspace=(kind == ENGINE_UNIFIED))
    eng = WorkflowEngine(state, store, adapter, emit=emit,
                         allow_unknown=bool(getattr(args, "allow_unknown", False)
                                            or settings.allow_unknown),
                         persistence=persistence)
    eng.apply_pool(pool)
    eng.save()

    for _ in range(16):
        result = eng.run()
        if result == "done":
            done = sum(1 for t in state.tasks if t.status in ("done", "verified"))
            print(f"PASS · fase {state.phase} · {done}/{len(state.tasks)} tarefas · {root}",
                  flush=True)
            if eng.persistence == "factory" and state.project_root:
                path = eng.store.legacy_factory_path(root)
            elif state.project_root:
                path = eng.store.project_state_path(root)
            else:
                path = eng.store.staging_path(state.run_id)
            print(f"estado: {path}", flush=True)
            return 0
        if result == "failed":
            print(f"FAIL · fase {state.phase} · {state.pause.detail or state.pause.reason}",
                  flush=True)
            return 1
        reason = state.pause.reason
        if reason in ("spec_questions", "spec_approval"):
            questions = list(state.requirements.open_questions)
            answered = False
            if questions and not args.yes:
                for q in questions:
                    try:
                        ans = input(f"Q: {q}\nA: ").strip()
                    except EOFError:
                        ans = ""
                    if ans:
                        adapter.answer(q, ans)
                        answered = True
            if answered:
                eng.spec_step()
            eng.approve_spec()
            print("[SPEC] aprovado — a avançar para a arquitetura", flush=True)
            continue
        if reason == "architecture_approval":
            print(f"[GATE] arquitetura pronta: {state.architecture_hash()}", flush=True)
            if getattr(args, "approve_architecture", False):
                eng.approve_architecture()
                print("[GATE] arquitetura aprovada", flush=True)
                continue
            print("gate fechado: usa --approve-architecture (--yes não o contorna)",
                  flush=True)
            return 4
        print(f"PAUSED · {reason}: {state.pause.detail}", flush=True)
        return 4
    print("PAUSED · limite de iterações do motor", flush=True)
    return 4


def _factory_cli(args: Any) -> int:
    """Dispatch new runs by CREW_ENGINE and resumes by the persisted engine."""
    settings = Settings.from_env()
    settings.unattended = bool(getattr(args, "unattended", False))
    if getattr(args, "allow_paid", False):
        settings.allow_paid = True
    if getattr(args, "allow_unknown", False):
        settings.allow_unknown = True
    if getattr(args, "allow_unverified", False):
        settings.allow_unverified = True
    if getattr(args, "profile", None):
        settings.workflow_profile = str(args.profile)
    if settings.unattended:
        settings.sleep_if_active = True
    store = StateManager(settings.workspace_dir, settings.log_dir)

    if args.resume:
        root = Path(os.path.expanduser(args.project)) if args.project else None
        target = store.resolve_resume(root)
        if target is not None and target.project_state is not None:
            return _engine_cli_run(args, target.engine_kind, settings, store,
                                   target.project_state, persistence="crew")
        if (target is not None and target.project_state is None
                and target.legacy_raw is not None
                and target.path == store.legacy_factory_path(root)):
            if target.engine_kind == ENGINE_UNIFIED:
                print(f"[STATE] estado inconsistente: {target.path} declara unified "
                      "mas não tem formato canónico", flush=True)
                return 6
            state = from_legacy_factory(target.legacy_raw,
                                        engine_kind=ENGINE_LEGACY)
            return _engine_cli_run(args, ENGINE_LEGACY, settings, store, state,
                                   persistence="factory")
        if (target is not None and target.project_state is None
                and target.engine_kind == ENGINE_UNIFIED):
            print(f"[STATE] estado inconsistente: {target.path} declara unified "
                  "mas não tem formato canónico", flush=True)
            return 6
        return _legacy_factory_cli(args, settings, store)

    try:
        kind = resolve_engine()
    except EngineError as exc:
        print(f"[ENGINE] {exc}", flush=True)
        return 2
    if kind == ENGINE_UNIFIED:
        return _engine_cli_run(args, kind, settings, store, None, persistence="crew")
    root = Path(os.path.expanduser(args.project)) if args.project else None
    if root is not None and store.project_state_path(root).is_file():
        print("[STATE] existe .crew neste projeto; usa --resume ou remove/migra "
              "antes de iniciar um run legacy", flush=True)
        return 2
    return _engine_cli_run(args, kind, settings, store, None, persistence="factory")


def _resolve_call_cli(args: Any) -> int:
    if not args.project or not args.call_outcome:
        print("--resolve-call exige --project DIR e --call-outcome "
              "confirmed|failed", flush=True)
        return 2
    root = Path(os.path.expanduser(args.project)).expanduser().resolve()
    ledger = CallLedger(root / ".crew" / "calls.json")
    try:
        rec = ledger.resolve(args.resolve_call, args.call_outcome)
    except CallLedgerError as exc:
        print(f"[CALL] {exc}", flush=True)
        return 6
    print(json.dumps(rec.to_dict(), ensure_ascii=False, indent=2), flush=True)
    return 0


def _resolve_publish_cli(args: Any) -> int:
    settings = Settings.from_env()
    store = StateManager(settings.workspace_dir, settings.log_dir)
    if not args.project:
        print("--resolve-publish-conflict exige --project DIR", flush=True)
        return 2
    root = Path(os.path.expanduser(args.project)).expanduser().resolve()
    try:
        state = store.load_project(store.project_state_path(root))
    except Exception as exc:
        print(f"[STATE] {exc}", flush=True)
        return 6
    ws = state.extensions.get("workspace_root") or str(
        default_workspace(settings, state.run_id))
    origin = dict(state.extensions.get("workspace_origin") or {})
    mgr = PublishManager(root, ws, root / ".crew", origin,
                         rules=IgnoreRules.from_project(root),
                         deletions=state.architecture.deletions)
    if not args.journal:
        print(json.dumps(mgr.list_issues(), ensure_ascii=False, indent=2), flush=True)
        return 0
    if not args.file or not args.choice:
        print("usa --journal ID --file REL --choice "
              "human|original|generated|manual [--manual-file PATH]", flush=True)
        return 2
    try:
        result = mgr.resolve(args.journal, args.file, args.choice, args.manual_file)
    except (PublishError, RecoveryRequired) as exc:
        print(f"[PUBLISH] {exc}", flush=True)
        return 4
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    if result.get("status") == "REVALIDATION_REQUIRED":
        print("[PUBLISH] revalidação necessária antes de DONE; "
              "não foi marcado como entregue", flush=True)
    return 0


def _migrate_cli(args: Any) -> int:
    settings = Settings.from_env()
    store = StateManager(settings.workspace_dir, settings.log_dir)
    if not args.project:
        print("--migrate-state/--migrate-schema/--migrate-engine exigem --project DIR",
              flush=True)
        return 2
    root = Path(os.path.expanduser(args.project))
    if getattr(args, "migrate_state", False):
        state = migrate_state(store, str(root))
        print(f"formato migrado: {store.project_state_path(root)} "
              f"(engine_kind={state.engine_kind}, schema={state.schema_version})", flush=True)
    if getattr(args, "migrate_schema", False):
        state = store.migrate_schema_to_v3(str(root))
        print(f"schema migrado: {store.project_state_path(root)} "
              f"(schema={state.schema_version})", flush=True)
    if getattr(args, "migrate_engine", False):
        state = store.load_project(store.project_state_path(root))
        state = migrate_engine(store, state)
        print(f"motor migrado: engine_kind={state.engine_kind} "
              f"(engine_version={state.engine_version}, phase={state.phase}, "
              f"pause={state.pause.reason or '-'})", flush=True)
    return 0


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="crew-tui", description="Agent pool dashboard + software factory")
    parser.add_argument("goal", nargs="?", metavar="GOAL",
                        help="objetivo (entrada única do Crew)")
    parser.add_argument("--factory", metavar="GOAL",
                        help="DEPRECATED: usa o objetivo posicional")
    parser.add_argument("--project", metavar="DIR", help="pasta do projeto (default: PROJECT_DIR/<slug>)")
    parser.add_argument("--resume", action="store_true",
                        help="retoma a execução a partir do estado persistido (.crew/.factory)")
    parser.add_argument("--yes", action="store_true", help="não faz perguntas (assume pressupostos)")
    parser.add_argument("--unattended", action="store_true",
                        help="no pico, suspende mesmo com a sessão ativa (acorda off-peak)")
    parser.add_argument("--allow-paid", action="store_true",
                        help="autoriza pay-as-you-go (DeepSeek direto / OpenRouter pago)")
    parser.add_argument("--allow-unknown", action="store_true",
                        help="autoriza billing desconhecido (nunca por omissão)")
    parser.add_argument("--allow-unverified", action="store_true",
                        help="entrega explicitamente artefacts UNVERIFIED")
    parser.add_argument("--profile", metavar="PROFILE",
                        help="workflow: auto|web|library|script|review")
    parser.add_argument("--approve-architecture", action="store_true",
                        help="aprova a arquitetura (o --yes não a aprova)")
    parser.add_argument("--resolve-publish-conflict", action="store_true",
                        help="resolve/recover a pending transactional publish")
    parser.add_argument("--journal", metavar="ID",
                        help="journal id for --resolve-publish-conflict")
    parser.add_argument("--file", metavar="REL",
                        help="file path to resolve in the journal")
    parser.add_argument("--choice", choices=["human", "original", "generated", "manual"],
                        help="resolution choice")
    parser.add_argument("--manual-file", metavar="PATH",
                        help="replacement file for --choice manual")
    parser.add_argument("--resolve-call", metavar="ID",
                        help="resolve an ambiguous external call in .crew/calls.json")
    parser.add_argument("--call-outcome", choices=["confirmed", "failed"],
                        help="resolution for --resolve-call")
    parser.add_argument("--migrate-state", action="store_true",
                        help="converte .factory/state.json para .crew/state.json (não muda o motor)")
    parser.add_argument("--migrate-schema", action="store_true",
                        help="converte .crew de schema v2 para v3 (não muda o motor)")
    parser.add_argument("--migrate-engine", action="store_true",
                        help="muda engine_kind para unified (operação distinta)")
    args = parser.parse_args()
    if args.resolve_publish_conflict:
        raise SystemExit(_resolve_publish_cli(args))
    if args.resolve_call:
        raise SystemExit(_resolve_call_cli(args))
    if args.goal and args.factory:
        parser.error("dá o objetivo como posicional OU --factory (deprecated), não ambos")
    if args.factory:
        print("[DEPRECATED] --factory: usa 'crew-tui \"objetivo\"'", file=sys.stderr, flush=True)
        args.goal = args.factory
    if args.goal:
        args.factory = args.goal
    if args.resume and args.factory:
        parser.error("--resume não aceita um objetivo")
    if args.migrate_state or args.migrate_schema or args.migrate_engine:
        try:
            raise SystemExit(_migrate_cli(args))
        except (StateError, FileNotFoundError) as e:
            print(f"[STATE] {e}", flush=True)
            raise SystemExit(6)
    if args.factory or args.resume:
        try:
            raise SystemExit(_factory_cli(args))
        except BudgetSuspended as e:
            print(f"[BREAKER] {e}", flush=True)
            raise SystemExit(3)
        except UnknownBilling as e:
            print(f"[BILLING] {e}", flush=True)
            raise SystemExit(5)
        except UnknownModelError as e:
            print(f"[BILLING] {e}", flush=True)
            raise SystemExit(5)
        except RouterExhausted as e:
            print(f"[ROUTER] {e}", flush=True)
            raise SystemExit(7)
        except EngineError as e:
            print(f"[ENGINE] {e}", flush=True)
            raise SystemExit(2)
        except StateCorruptError as e:
            print(f"[STATE] {e}", flush=True)
            raise SystemExit(6)
        except StateError as e:
            print(f"[STATE] {e}", flush=True)
            raise SystemExit(6)
    CrewTUIApp().run()


if __name__ == "__main__":
    main()
