"""Workflow profiles and deterministic resolution (stdlib only)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

PROFILE_VERSION = 1
WEB = "web"
LIBRARY = "library"
SCRIPT = "script"
REVIEW = "review"
AUTO = "auto"
VALID = {WEB, LIBRARY, SCRIPT, REVIEW, AUTO}

WEB_HINTS = (
    "browser", "playwright", "frontend", "front-end", "ui", "ux", "html",
    "css", "javascript", "react", "vue", "svelte", "screenshot", "visual",
)
LIBRARY_HINTS = (
    "library", "lib", "sdk", "package", "public api", "pytest",
    "compatibility", "semver", "plugin",
)
REVIEW_HINTS = ("review", "audit", "analisar", "análise", "read-only", "relatório")


def phases_for(profile: str) -> tuple[str, ...]:
    p = profile if profile in VALID else WEB
    if p == AUTO:
        p = WEB  # full workflow until auto is resolved
    if p == REVIEW:
        return ("SPEC", "VERIFY", "SIGNOFF", "DONE")
    base = ("SPEC", "ARCHITECTURE", "SCAFFOLD", "BUILD", "VERIFY",
            "INTEGRATE", "SIGNOFF", "DONE")
    if p == WEB:
        return ("SPEC", "ARCHITECTURE", "SCAFFOLD", "BUILD", "VERIFY",
                "INTEGRATE", "VISUAL", "SIGNOFF", "DONE")
    return base


@dataclass
class ProfileSelection:
    requested: str
    resolved: str
    reason: str
    profile_version: int = PROFILE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "resolved": self.resolved,
            "reason": self.reason,
            "profile_version": self.profile_version,
        }


def _text(*values: Any) -> str:
    parts: list[str] = []
    for value in values:
        if isinstance(value, dict):
            parts.append(" ".join(str(v) for v in value.values()))
        elif isinstance(value, (list, tuple, set)):
            parts.extend(str(v) for v in value)
        else:
            parts.append(str(value or ""))
    return " ".join(parts).lower()


def _has_hint(text: str, hint: str) -> bool:
    pattern = r"(?<![a-z0-9])" + re.escape(hint) + r"(?![a-z0-9])"
    return re.search(pattern, text) is not None


def resolve_profile(requested: str, *, goal: str = "",
                    requirements: Any = None, architecture: Any = None,
                    tasks: Iterable[Any] = ()) -> ProfileSelection:
    req = str(requested or AUTO).strip().lower()
    if req not in VALID:
        req = AUTO
    if req in (WEB, LIBRARY, SCRIPT, REVIEW):
        return ProfileSelection(req, req, "explícito")

    targets = [str(getattr(t, "target_file", "") or "") for t in tasks]
    web_blob = _text(goal, requirements, architecture, targets,
                     [getattr(t, "title", "") for t in tasks],
                     [getattr(t, "description", "") for t in tasks])
    if any(t.lower().endswith((".html", ".htm", ".css", ".js", ".jsx", ".ts",
                               ".tsx", ".vue", ".svelte")) for t in targets):
        return ProfileSelection(AUTO, WEB,
                                "alvos de UI/browser no plano de tarefas")
    if any(_has_hint(web_blob, h) for h in WEB_HINTS):
        return ProfileSelection(AUTO, WEB,
                                "requisitos/arquitetura indicam UI ou browser")
    if any(_has_hint(web_blob, h) for h in LIBRARY_HINTS):
        return ProfileSelection(AUTO, LIBRARY,
                                "requisitos/arquitetura indicam biblioteca/API/Testes")
    if any(_has_hint(web_blob, h) for h in REVIEW_HINTS):
        return ProfileSelection(AUTO, REVIEW,
                                "requisitos indicam revisão read-only")
    return ProfileSelection(AUTO, SCRIPT,
                            "sem UI nem biblioteca; script por omissão")
