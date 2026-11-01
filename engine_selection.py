"""Central adapter identity selection for new, resumed and adopted projects."""

from __future__ import annotations

import os
from typing import Any, Optional

from state import ENGINE_LEGACY, ENGINE_UNIFIED, ProjectState

NATIVE = "native"
FACTORY = "factory"
IDENTITIES = {NATIVE, FACTORY}
DEFAULT_NEW_IDENTITY = NATIVE
IDENTITY_KEY = "engine_identity"
ORIGIN_KEY = "project_origin"
CREATED_BY_CREW = "CREATED_BY_CREW"
EXTERNAL_EXISTING_REPOSITORY = "EXTERNAL_EXISTING_REPOSITORY"


class EngineIdentityError(Exception):
    """Ambiguous or conflicting project engine identity."""


def _normalise(value: Optional[str]) -> Optional[str]:
    raw = (value or "").strip().lower()
    if not raw:
        return None
    if raw in ("legacy", FACTORY):
        return FACTORY
    if raw in ("unified", NATIVE):
        return NATIVE
    raise EngineIdentityError(
        f"motor inválido: {value!r} (esperado native|factory)")


def identity_of(state: ProjectState) -> Optional[str]:
    raw = state.extensions.get(IDENTITY_KEY)
    return _normalise(str(raw)) if raw else None


def origin_of(state: ProjectState) -> str:
    return str(state.extensions.get(ORIGIN_KEY) or CREATED_BY_CREW)


def set_identity(state: ProjectState, identity: str, *,
                 origin: Optional[str] = None) -> str:
    normal = _normalise(identity)
    if normal is None:
        raise EngineIdentityError("identidade de motor vazia")
    state.extensions[IDENTITY_KEY] = normal
    if origin:
        state.extensions[ORIGIN_KEY] = str(origin)
    return normal


def new_identity(explicit: Optional[str] = None,
                 env: Optional[str] = None) -> tuple[str, str]:
    value = explicit if explicit is not None else (
        env if env is not None else os.environ.get("CREW_ENGINE"))
    normal = _normalise(value) if value not in (None, "") else None
    if normal is not None:
        return normal, "explicit selection"
    return DEFAULT_NEW_IDENTITY, "NativeAdapter is the default for new projects"


def resume_identity(state: ProjectState,
                    explicit: Optional[str] = None) -> tuple[str, str]:
    persisted = identity_of(state)
    requested = _normalise(explicit) if explicit not in (None, "") else None
    if persisted is not None:
        if requested is not None and requested != persisted:
            raise EngineIdentityError(
                f"engine override conflict: persisted={persisted} "
                f"requested={requested}")
        return persisted, "persisted project engine identity"
    if requested is not None:
        return requested, "explicit resolution of missing identity"
    if state.engine_kind == ENGINE_LEGACY:
        return FACTORY, "legacy .factory project implies FactoryAdapter"
    if origin_of(state) == EXTERNAL_EXISTING_REPOSITORY:
        return NATIVE, "adopted external project is NativeAdapter"
    raise EngineIdentityError(
        "project engine identity is ambiguous; pass --engine native|factory")


def resolve_and_persist(state: ProjectState,
                        explicit: Optional[str] = None) -> tuple[str, str]:
    persisted = identity_of(state)
    identity, reason = resume_identity(state, explicit)
    if persisted is None:
        set_identity(state, identity, origin=origin_of(state))
    return identity, reason


def describe(state: ProjectState, explicit: Optional[str] = None) -> dict[str, Any]:
    try:
        identity, reason = resume_identity(state, explicit)
        error = ""
    except EngineIdentityError as exc:
        identity, reason, error = None, "", str(exc)
    if identity == FACTORY:
        effective = "FactoryAdapter"
    elif identity == NATIVE:
        effective = "NativeAdapter"
    else:
        effective = "blocked"
    return {
        "project_origin": origin_of(state),
        "persisted_engine_identity": identity_of(state),
        "effective_adapter": effective,
        "selection_reason": reason,
        "error": error,
        "engine_kind": state.engine_kind,
        "phase": state.phase,
    }
