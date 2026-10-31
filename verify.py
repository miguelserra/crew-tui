"""Verification policy and delivery gate (stdlib only).

The engine collects evidence; this module decides whether a build may be
delivered. It never treats a missing critic as PASS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

PASS = "PASS"
FAIL = "FAIL"
UNVERIFIED = "UNVERIFIED"
NA = "N/A"

VALID_VERDICTS = {PASS, FAIL, UNVERIFIED, NA}


def normalize_verdict(value: Any) -> str:
    v = str(value or "").upper()
    if v in ("NA", "N/A", "SKIP", "NOT_APPLICABLE"):
        return NA
    if v in VALID_VERDICTS:
        return v
    return UNVERIFIED


def critic_is_valid(producer_family: str, critic_family: str,
                    producer_known: bool = True) -> bool:
    """Cross-family is mandatory; the same endpoint/family is not enough."""
    p = (producer_family or "").strip().lower()
    c = (critic_family or "").strip().lower()
    return bool(producer_known) and bool(p) and bool(c) and p != c


@dataclass
class Decision:
    verdict: str
    deliver: bool
    reasons: list[str] = field(default_factory=list)
    critic_valid: bool = False
    critic_executed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "deliver": self.deliver,
            "reasons": list(self.reasons),
            "critic_valid": self.critic_valid,
            "critic_executed": self.critic_executed,
        }


def _critic_fields(critic: dict[str, Any]) -> tuple[str, bool, bool, list[Any]]:
    verdict = normalize_verdict(critic.get("verdict"))
    producer_family = str(critic.get("producer_family") or "")
    critic_family = str(critic.get("critic_family") or "")
    producer_known = bool(critic.get("producer_known", True))
    executed = verdict in (PASS, FAIL) and bool(critic.get("critic"))
    valid = executed and critic_is_valid(producer_family, critic_family,
                                         producer_known)
    objections = list(critic.get("objections") or [])
    return verdict, valid, executed, objections


def decide_build(oracle_ok: bool, critic: dict[str, Any],
                 visual: Optional[dict[str, Any]] = None,
                 tests: Optional[dict[str, Any]] = None,
                 allow_unverified: bool = False) -> Decision:
    reasons: list[str] = []
    cverdict, valid, executed, objections = _critic_fields(critic)
    visual = visual or {"verdict": NA}
    tests = tests or {"verdict": "SKIP", "advisory": True}
    vverdict = normalize_verdict(visual.get("verdict", NA))
    tverdict = normalize_verdict(tests.get("verdict", "SKIP"))
    advisory = bool(tests.get("advisory", False))

    if not oracle_ok:
        reasons.append("oracle falhou")
    if not executed:
        reasons.append("crítico não executou")
    elif not valid:
        reasons.append("crítico não é cross-family / produtor não identificado")
    if cverdict == FAIL or objections:
        reasons.append("crítico FAIL")
    if vverdict == FAIL:
        reasons.append("visual FAIL")
    if vverdict == UNVERIFIED:
        reasons.append("visual UNVERIFIED")
    if tverdict == FAIL and not advisory:
        reasons.append("testes determinísticos FAIL")

    unverified = (not executed or not valid) or vverdict == UNVERIFIED
    if unverified:
        return Decision(UNVERIFIED, allow_unverified, reasons or ["UNVERIFIED"],
                         valid, executed)
    if reasons:
        return Decision(FAIL, False, reasons, valid, executed)
    return Decision(PASS, True, [], valid, executed)


def decide_review(critic: dict[str, Any],
                  allow_unverified: bool = False) -> Decision:
    """A review report may be delivered with objections, but not without a critic."""
    cverdict, valid, executed, objections = _critic_fields(critic)
    if not executed or not valid:
        return Decision(UNVERIFIED, allow_unverified,
                        ["crítico de review inválido/indisponível"], valid, executed)
    if cverdict == FAIL or objections:
        return Decision(FAIL, True, ["review com objeções documentadas"], valid, executed)
    return Decision(PASS, True, [], valid, executed)
