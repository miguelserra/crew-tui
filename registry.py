"""Model identity and credential profiles (stdlib only).

Billing is a property of the *credential profile* (provider + endpoint), never
inferred from the model's display name. A model id that exists on two
endpoints (e.g. an OpenCode Go alias and the direct DeepSeek API) produces two
different ``ModelRef`` values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

BILLING_LOCAL = "local"
BILLING_FREE = "free_quota"
BILLING_SUBSCRIPTION = "subscription"
BILLING_PAYG = "payg"
BILLING_UNKNOWN = "unknown"

VALID_BILLING = {
    BILLING_LOCAL,
    BILLING_FREE,
    BILLING_SUBSCRIPTION,
    BILLING_PAYG,
    BILLING_UNKNOWN,
}


def normalize_billing(value: str | None) -> str:
    """Never coerce an unknown billing class into a cheaper one."""
    v = (value or "").strip().lower()
    if v == "free":
        return BILLING_FREE
    return v if v in VALID_BILLING else BILLING_UNKNOWN


@dataclass(frozen=True)
class CredentialProfile:
    """Where a model is reached and how that endpoint bills."""

    name: str
    provider: str
    endpoint: str
    api_key_env: str = ""
    billing: str = BILLING_UNKNOWN

    def __post_init__(self) -> None:
        object.__setattr__(self, "billing", normalize_billing(self.billing))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "endpoint": self.endpoint,
            "api_key_env": self.api_key_env,
            "billing": self.billing,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "CredentialProfile":
        data = data or {}
        return cls(
            name=str(data.get("name") or ""),
            provider=str(data.get("provider") or ""),
            endpoint=str(data.get("endpoint") or ""),
            api_key_env=str(data.get("api_key_env") or ""),
            billing=normalize_billing(data.get("billing")),
        )


# Known profiles. OpenRouter billing varies per model, so the profile is
# unknown and the concrete ModelRef carries free/payg.
PROFILES: dict[str, CredentialProfile] = {
    "local": CredentialProfile("local", "local", "local", "", BILLING_LOCAL),
    "gemini-plus": CredentialProfile(
        "gemini-plus", "google", "gemini", "GEMINI_API_KEY", BILLING_SUBSCRIPTION
    ),
    "opencode-go": CredentialProfile(
        "opencode-go", "opencode", "go", "OPENCODE_API_KEY", BILLING_SUBSCRIPTION
    ),
    "opencode-free": CredentialProfile(
        "opencode-free", "opencode", "zen-free", "OPENCODE_API_KEY", BILLING_FREE
    ),
    "openrouter": CredentialProfile(
        "openrouter", "openrouter", "api", "OPENROUTER_API_KEY", BILLING_UNKNOWN
    ),
    "deepseek-api": CredentialProfile(
        "deepseek-api", "deepseek", "direct-api", "DEEPSEEK_API_KEY", BILLING_PAYG
    ),
    "unknown": CredentialProfile("unknown", "unknown", "unknown", "", BILLING_UNKNOWN),
}


def profile(name: str) -> CredentialProfile:
    return PROFILES.get(name, PROFILES["unknown"])


@dataclass(frozen=True)
class ModelRef:
    """The effective identity of one model call."""

    provider: str
    endpoint: str
    model_id: str
    family: str = ""
    billing: str = BILLING_UNKNOWN
    credential_profile: str = ""
    requested_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "billing", normalize_billing(self.billing))

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "endpoint": self.endpoint,
            "model_id": self.model_id,
            "family": self.family,
            "billing": self.billing,
            "credential_profile": self.credential_profile,
            "requested_id": self.requested_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ModelRef":
        data = data or {}
        return cls(
            provider=str(data.get("provider") or "unknown"),
            endpoint=str(data.get("endpoint") or "unknown"),
            model_id=str(data.get("model_id") or "unknown"),
            family=str(data.get("family") or ""),
            billing=normalize_billing(data.get("billing")),
            credential_profile=str(data.get("credential_profile") or ""),
            requested_id=str(data.get("requested_id") or ""),
        )


def unknown_ref() -> ModelRef:
    return ModelRef(
        provider="unknown",
        endpoint="unknown",
        model_id="unknown",
        family="unknown",
        billing=BILLING_UNKNOWN,
        credential_profile="unknown",
    )


def ref_for(
    profile_name: str,
    model_id: str,
    *,
    family: str = "",
    billing: Optional[str] = None,
    requested_id: str = "",
) -> ModelRef:
    p = profile(profile_name)
    return ModelRef(
        provider=p.provider,
        endpoint=p.endpoint,
        model_id=model_id or "unknown",
        family=family or p.provider,
        billing=normalize_billing(billing) if billing is not None else p.billing,
        credential_profile=p.name,
        requested_id=requested_id,
    )


def ref_from_handle(handle: Any, served_id: str = "") -> ModelRef:
    """Read the identity attached by the LLM constructors."""
    ref = getattr(handle, "_model_ref", None)
    if ref is None:
        return unknown_ref()
    if served_id and served_id != ref.model_id:
        return ModelRef(
            provider=ref.provider,
            endpoint=ref.endpoint,
            model_id=served_id,
            family=ref.family,
            billing=ref.billing,
            credential_profile=ref.credential_profile,
            requested_id=ref.model_id,
        )
    return ref


def family_of(model_id: str) -> str:
    """Coarse model family used to enforce cross-family critics later."""
    n = (model_id or "").lower()
    if "gemini" in n:
        return "gemini"
    if "deepseek" in n:
        return "deepseek"
    if "kimi" in n or "moonshot" in n:
        return "kimi"
    if "qwen" in n or "llama" in n:
        return "local"
    if "nemotron" in n:
        return "nvidia"
    if "claude" in n:
        return "anthropic"
    if "gpt" in n or "openai" in n:
        return "openai"
    return (n.split("/")[0] if "/" in n else n) or "unknown"
