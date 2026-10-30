"""Model registry: identities, credential profiles, billing and pricing.

``ModelIdentity`` is what the system knows about a model and its endpoint;
``ModelRef`` is the effective model that answered one attempt (with an identity
snapshot for historical telemetry).

No billing or capability is inferred from a model's display name. Unknown
models get ``billing=unknown`` and are blocked by the dispatch guard unless the
user explicitly authorizes them.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
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
VALID_CAPABILITIES = {"text", "vision", "json", "tools"}


class RegistryError(Exception):
    pass


class TomlUnavailable(RegistryError):
    pass


def normalize_billing(value: str | None) -> str:
    """Never coerce an unknown billing class into a cheaper one."""
    v = (value or "").strip().lower()
    if v in ("free", "free_tier", "free-quota", "free_quota"):
        return BILLING_FREE
    if v in ("fixed", "sub", "subscription"):
        return BILLING_SUBSCRIPTION
    return v if v in VALID_BILLING else BILLING_UNKNOWN


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _date() -> str:
    return time.strftime("%Y-%m-%d")


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


@dataclass(frozen=True)
class ModelPrice:
    currency: str = "USD"
    input_per_million: Optional[Decimal] = None
    output_per_million: Optional[Decimal] = None
    source: str = ""
    updated: str = ""

    @property
    def confirmed(self) -> bool:
        return self.input_per_million is not None and self.output_per_million is not None

    @property
    def free(self) -> bool:
        return (self.confirmed and self.input_per_million == 0
                and self.output_per_million == 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "input_per_million": (str(self.input_per_million)
                                  if self.input_per_million is not None else None),
            "output_per_million": (str(self.output_per_million)
                                   if self.output_per_million is not None else None),
            "source": self.source,
            "updated": self.updated,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ModelPrice":
        data = data or {}
        return cls(
            currency=str(data.get("currency") or "USD"),
            input_per_million=_decimal(data.get("input_per_million")),
            output_per_million=_decimal(data.get("output_per_million")),
            source=str(data.get("source") or data.get("pricing_source") or ""),
            updated=str(data.get("updated") or data.get("pricing_updated") or ""),
        )


@dataclass(frozen=True)
class ModelIdentity:
    provider: str
    endpoint: str
    model_id: str
    family: str = ""
    credential_profile: str = ""
    billing: str = BILLING_UNKNOWN
    capabilities: frozenset[str] = frozenset()
    price: Optional[ModelPrice] = None
    quota_consumed: bool = False
    defined: bool = True
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "billing", normalize_billing(self.billing))
        caps = {c for c in self.capabilities if c in VALID_CAPABILITIES}
        object.__setattr__(self, "capabilities", frozenset(caps))

    @property
    def key(self) -> tuple[str, str]:
        return (self.credential_profile, self.model_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "endpoint": self.endpoint,
            "model_id": self.model_id,
            "family": self.family,
            "credential_profile": self.credential_profile,
            "billing": self.billing,
            "capabilities": sorted(self.capabilities),
            "price": self.price.to_dict() if self.price else None,
            "quota_consumed": self.quota_consumed,
            "defined": self.defined,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ModelIdentity":
        data = data or {}
        return cls(
            provider=str(data.get("provider") or "unknown"),
            endpoint=str(data.get("endpoint") or "unknown"),
            model_id=str(data.get("model_id") or "unknown"),
            family=str(data.get("family") or ""),
            credential_profile=str(data.get("credential_profile") or ""),
            billing=normalize_billing(data.get("billing")),
            capabilities=frozenset(str(c) for c in (data.get("capabilities") or [])),
            price=(ModelPrice.from_dict(data.get("price"))
                   if data.get("price") else None),
            quota_consumed=bool(data.get("quota_consumed")),
            defined=bool(data.get("defined", True)),
            note=str(data.get("note") or ""),
        )


@dataclass(frozen=True)
class CostEstimate:
    currency: str
    input_cost: Decimal
    output_cost: Decimal
    total: Decimal
    basis: str
    billable: bool
    quota_consumed: bool
    known: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "input_cost": str(self.input_cost),
            "output_cost": str(self.output_cost),
            "total": str(self.total),
            "basis": self.basis,
            "billable": self.billable,
            "quota_consumed": self.quota_consumed,
            "known": self.known,
        }


@dataclass(frozen=True)
class ModelRef:
    """The effective identity of one model call (with a historical snapshot)."""

    provider: str
    endpoint: str
    model_id: str
    family: str = ""
    billing: str = BILLING_UNKNOWN
    credential_profile: str = ""
    requested_id: str = ""
    identity: dict[str, Any] = field(default_factory=dict)
    available: Optional[bool] = None

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
            "identity": dict(self.identity),
            "available": self.available,
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
            identity=dict(data.get("identity") or {}),
            available=data.get("available"),
        )


def _price(inp: str, out: str, source: str, updated: str = "2026-10-30") -> ModelPrice:
    return ModelPrice(input_per_million=Decimal(inp),
                      output_per_million=Decimal(out),
                      source=source, updated=updated)


def _identity(profile: str, model_id: str, family: str, billing: str,
              capabilities: list[str], price: Optional[ModelPrice],
              quota: bool, note: str = "") -> ModelIdentity:
    p = PROFILES[profile]
    return ModelIdentity(
        provider=p.provider, endpoint=p.endpoint, model_id=model_id,
        family=family, credential_profile=p.name, billing=billing,
        capabilities=frozenset(capabilities), price=price,
        quota_consumed=quota, defined=True, note=note,
    )


def default_models() -> dict[tuple[str, str], ModelIdentity]:
    zero_sub = _price("0", "0", "subscription (no PAYG marginal charge)")
    out: list[ModelIdentity] = [
        _identity("local", "qwen3.6-35b-a3b", "local", BILLING_LOCAL,
                  ["text", "json"], _price("0", "0", "local inference"), False),
        # Gemini Plus subscription (fixed cost, quota consumed).
        _identity("gemini-plus", "gemini-3.5-flash", "gemini", BILLING_SUBSCRIPTION,
                  ["text", "vision", "json"], zero_sub, True),
        _identity("gemini-plus", "gemini-3.8-flash", "gemini", BILLING_SUBSCRIPTION,
                  ["text", "vision", "json"], zero_sub, True),
        _identity("gemini-plus", "gemini-3.1-pro-preview", "gemini",
                  BILLING_SUBSCRIPTION, ["text", "vision", "json"], zero_sub, True),
        # OpenCode Go subscription models (fixed cost, quota consumed).
        _identity("opencode-go", "deepseek-v4-pro", "deepseek",
                  BILLING_SUBSCRIPTION, ["text", "json"], zero_sub, True),
        _identity("opencode-go", "deepseek-v4.1-flash", "deepseek",
                  BILLING_SUBSCRIPTION, ["text", "json"], zero_sub, True),
        _identity("opencode-go", "kimi-k3.1-pro", "kimi",
                  BILLING_SUBSCRIPTION, ["text", "json"], zero_sub, True),
        _identity("opencode-go", "gemini-4-argon", "gemini",
                  BILLING_SUBSCRIPTION, ["text", "json"], zero_sub, True),
        _identity("opencode-go", "beam-free", "opencode",
                  BILLING_SUBSCRIPTION, ["text", "json"], zero_sub, True),
        # Free zen stealth model (availability probed at startup).
        _identity("opencode-free", "big-pickle", "opencode", BILLING_FREE,
                  ["text", "json"], _price("0", "0", "free zen quota"), True,
                  "availability probed at startup"),
        # OpenRouter: free only if both confirmed prices are zero.
        _identity("openrouter", "nvidia/nemotron-3-ultra", "nvidia",
                  BILLING_FREE, ["text", "json"],
                  _price("0", "0", "openrouter pricing: prompt=completion=0"), True),
        # Direct DeepSeek API (payg, Decimal per 1M tokens).
        _identity("deepseek-api", "deepseek-v4-pro", "deepseek", BILLING_PAYG,
                  ["text", "json"], _price("0.60", "2.20", "DeepSeek API pricing"),
                  False),
    ]
    return {i.key: i for i in out}


class ModelRegistry:
    def __init__(self, profiles: Optional[dict[str, CredentialProfile]] = None,
                 models: Optional[list[ModelIdentity]] = None):
        self.profiles = dict(profiles or PROFILES)
        self.models: dict[tuple[str, str], ModelIdentity] = {}
        for identity in (models if models is not None else default_models().values()):
            self.register(identity)
        self.config_source = "built-in"

    def register(self, identity: ModelIdentity) -> None:
        self.models[identity.key] = identity

    def profile(self, name: str) -> CredentialProfile:
        return self.profiles.get(name, self.profiles.get("unknown", PROFILES["unknown"]))

    def identity_for(self, profile_name: str, model_id: str, *,
                     billing: Optional[str] = None, family: str = "",
                     note: str = "") -> ModelIdentity:
        model_id = model_id or "unknown"
        identity = self.models.get((profile_name, model_id))
        if identity is None:
            p = self.profile(profile_name)
            default_billing = (BILLING_LOCAL if p.billing == BILLING_LOCAL
                               else BILLING_UNKNOWN)
            identity = ModelIdentity(
                provider=p.provider, endpoint=p.endpoint, model_id=model_id,
                family=family or family_of(model_id),
                credential_profile=p.name,
                billing=normalize_billing(billing) if billing is not None else default_billing,
                capabilities=frozenset(),
                price=None,
                quota_consumed=False,
                defined=False,
                note=note or "modelo não registado",
            )
        elif billing is not None:
            identity = replace(identity, billing=normalize_billing(billing))
        if family and identity.family != family:
            identity = replace(identity, family=family)
        if note:
            identity = replace(identity, note=note)
        return identity

    def ref_for_identity(self, identity: ModelIdentity, *,
                         requested_id: str = "",
                         available: Optional[bool] = None) -> ModelRef:
        return ModelRef(
            provider=identity.provider, endpoint=identity.endpoint,
            model_id=identity.model_id, family=identity.family,
            billing=identity.billing,
            credential_profile=identity.credential_profile,
            requested_id=requested_id,
            identity=identity.to_dict(),
            available=available,
        )

    def ref_for(self, profile_name: str, model_id: str, *,
                family: str = "", billing: Optional[str] = None,
                requested_id: str = "", available: Optional[bool] = None) -> ModelRef:
        identity = self.identity_for(profile_name, model_id, billing=billing,
                                     family=family)
        return self.ref_for_identity(identity, requested_id=requested_id,
                                     available=available)

    def ref_served(self, ref: ModelRef, served_id: str,
                   available: Optional[bool] = None) -> ModelRef:
        if not served_id or served_id == ref.model_id:
            if available is None or available == ref.available:
                return ref
            return replace(ref, available=available)
        identity = self.models.get((ref.credential_profile, served_id))
        if identity is None:
            base = ModelIdentity.from_dict(ref.identity) if ref.identity else None
            identity = (replace(base, model_id=served_id) if base
                        else self.identity_for(ref.credential_profile, served_id))
        return ModelRef(
            provider=identity.provider, endpoint=identity.endpoint,
            model_id=served_id, family=identity.family, billing=identity.billing,
            credential_profile=identity.credential_profile,
            requested_id=ref.model_id, identity=identity.to_dict(),
            available=available if available is not None else ref.available,
        )

    def openrouter_identity(self, model_id: str,
                            pricing: Optional[dict[str, Any]] = None) -> ModelIdentity:
        base = self.identity_for("openrouter", model_id)
        if pricing and base.defined:
            pin = _decimal(pricing.get("prompt"))
            pout = _decimal(pricing.get("completion"))
            if pin is not None and pout is not None:
                # OpenRouter quotes USD per token; normalize per million.
                one_m = Decimal(1_000_000)
                price = ModelPrice(
                    currency="USD",
                    input_per_million=pin * one_m,
                    output_per_million=pout * one_m,
                    source="openrouter /models pricing",
                    updated=_date(),
                )
                # free_quota only if both confirmed prices are zero.
                billing = (BILLING_FREE if price.free else BILLING_PAYG)
                return replace(base, billing=billing, price=price,
                               quota_consumed=(billing == BILLING_FREE))
        return base

    def estimate(self, identity: ModelIdentity, in_tokens: int,
                 out_tokens: int) -> CostEstimate:
        billing = identity.billing
        zero = Decimal("0")
        currency = identity.price.currency if identity.price else "USD"
        if billing == BILLING_LOCAL:
            return CostEstimate(currency, zero, zero, zero, "local", False, False, True)
        if billing in (BILLING_SUBSCRIPTION, BILLING_FREE):
            # Not a $0 economic cost: it consumes the subscription/quota.
            return CostEstimate(currency, zero, zero, zero, billing, False, True, True)
        if billing == BILLING_PAYG:
            if identity.price and identity.price.confirmed:
                one_m = Decimal(1_000_000)
                inc = (Decimal(in_tokens) * identity.price.input_per_million) / one_m
                out = (Decimal(out_tokens) * identity.price.output_per_million) / one_m
                return CostEstimate(currency, inc, out, inc + out, "payg",
                                    True, False, True)
            return CostEstimate(currency, zero, zero, zero, "payg", True, False, False)
        return CostEstimate(currency, zero, zero, zero, "unknown", False, False, False)

    # -- serialization ----------------------------------------------------- #

    def to_dict(self) -> dict[str, Any]:
        return {
            "profiles": {k: v.to_dict() for k, v in self.profiles.items()},
            "models": [m.to_dict() for m in self.models.values()],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelRegistry":
        profiles: dict[str, CredentialProfile] = dict(PROFILES)
        for name, raw in (data.get("profiles") or {}).items():
            raw = dict(raw or {})
            raw.setdefault("name", name)
            profiles[name] = CredentialProfile.from_dict(raw)
        # Keep shipped defaults available unless the file overrides the same key.
        models: list[ModelIdentity] = list(default_models().values())
        for raw in data.get("models") or []:
            raw = dict(raw or {})
            profile_name = str(raw.get("profile") or raw.get("credential_profile") or "")
            p = profiles.get(profile_name, PROFILES.get(profile_name, PROFILES["unknown"]))
            price = None
            if any(k in raw for k in ("input_per_million", "output_per_million",
                                      "pricing_source", "pricing_updated")):
                price = ModelPrice(
                    currency=str(raw.get("currency") or "USD"),
                    input_per_million=_decimal(raw.get("input_per_million")),
                    output_per_million=_decimal(raw.get("output_per_million")),
                    source=str(raw.get("pricing_source") or raw.get("source") or ""),
                    updated=str(raw.get("pricing_updated") or raw.get("updated") or ""),
                )
            model_id = str(raw.get("model_id") or raw.get("model") or "")
            if not model_id:
                raise RegistryError("modelo sem model_id")
            models.append(ModelIdentity(
                provider=str(raw.get("provider") or p.provider),
                endpoint=str(raw.get("endpoint") or p.endpoint),
                model_id=model_id,
                family=str(raw.get("family") or family_of(model_id)),
                credential_profile=profile_name or p.name,
                billing=normalize_billing(raw.get("billing") or p.billing),
                capabilities=frozenset(str(c) for c in (raw.get("capabilities") or [])),
                price=price,
                quota_consumed=bool(raw.get("quota_consumed")),
                defined=True,
                note=str(raw.get("note") or ""),
            ))
        return cls(profiles, models)

    @classmethod
    def load_toml(cls, path: str | Path) -> "ModelRegistry":
        try:
            import tomllib as parser  # Python 3.11+
        except Exception:
            try:
                import tomli as parser  # type: ignore
            except Exception as exc:
                raise TomlUnavailable(
                    "models.toml requires tomllib (3.11+) or tomli on Python 3.10"
                ) from exc
        raw = parser.loads(Path(path).read_text(encoding="utf-8"))
        reg = cls.from_dict(raw)
        reg.config_source = str(path)
        return reg

    @classmethod
    def load_default(cls) -> "ModelRegistry":
        path = os.environ.get("MODELS_CONFIG")
        candidate = Path(path) if path else Path(__file__).with_name("models.toml")
        if candidate.is_file():
            try:
                return cls.load_toml(candidate)
            except TomlUnavailable:
                # No TOML parser (3.10 without tomli): the built-in defaults
                # still cover the shipped models.
                return cls()
        return cls()


DEFAULT_REGISTRY = ModelRegistry()


def profile(name: str) -> CredentialProfile:
    return PROFILES.get(name, PROFILES["unknown"])


def unknown_ref() -> ModelRef:
    return ModelRef(
        provider="unknown", endpoint="unknown", model_id="unknown",
        family="unknown", billing=BILLING_UNKNOWN,
        credential_profile="unknown",
        identity=ModelIdentity("unknown", "unknown", "unknown", "unknown",
                               "unknown", BILLING_UNKNOWN, defined=False).to_dict(),
    )


def ref_for(profile_name: str, model_id: str, *, family: str = "",
            billing: Optional[str] = None, requested_id: str = "",
            available: Optional[bool] = None) -> ModelRef:
    return DEFAULT_REGISTRY.ref_for(
        profile_name, model_id, family=family, billing=billing,
        requested_id=requested_id, available=available)


def ref_from_handle(handle: Any, served_id: str = "") -> ModelRef:
    ref = getattr(handle, "_model_ref", None)
    if not isinstance(ref, ModelRef):
        return unknown_ref()
    if served_id and served_id != ref.model_id:
        return DEFAULT_REGISTRY.ref_served(ref, served_id)
    return ref


def family_of(model_id: str) -> str:
    """Coarse family, used only as a hint; capabilities come from the registry."""
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
