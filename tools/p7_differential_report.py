#!/usr/bin/env python3
"""Generate docs/p7-differential-report.md from the P7.1d differential suite.

Run with the runtime deps available (the suite skips without them):

    .venv/bin/python tools/p7_differential_report.py
"""

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCENARIOS = [
    ("Normal BUILD → VERIFY → INTEGRATE → VISUAL → SIGN-OFF",
     "test_build_integrate_signoff_match",
     "artefactos, estado/tarefas, tentativas, ModelRef, verificação, integração"),
    ("FAIL → FIX → PASS (qualidade)",
     "test_build_quality_recovery_fail_then_fix",
     "sequência de tentativas, produtor, verificação"),
    ("UNVERIFIED bloqueia entrega",
     "test_build_unverified_blocks_both",
     "veredicto, ausência de artefacto entregue"),
    ("Visual retry (nova avaliação)",
     "test_factory_visual_rechecks_after_fix",
     "vision_retry/vision, auditoria"),
    ("Fallback técnico (quota/timeout)",
     "test_fallback_quota_and_timeout_then_success",
     "sequência de candidatos, outcome, ModelRef efectivo"),
    ("Fallback total indisponível",
     "test_fallback_all_unavailable_blocks_both",
     "tentativas, ausência de produtor"),
    ("Playwright indisponível",
     "test_playwright_unavailable_is_unverified",
     "UNVERIFIED, nunca PASS artificial; app que não arranca -> FAIL de "
     "integração (`test_web_app_failure_is_integration_fail`)"),
    ("Resume após BUILD",
     "test_resume_after_build_preserves_evidence",
     "evidência preservada, sem tentativas duplicadas"),
    ("Publicação normal (sandboxes)",
     "test_publication_parity",
     "manifest, hashes, ficheiros publicados, journal"),
    ("Publicação com conflito",
     "test_publication_conflict_parity",
     "detecção de conflito e recusa"),
]

PENDING = [
    ("Idempotency header por provider",
     "A chave é gerada e registada, mas ainda não é enviada. Falta uma "
     "capacidade explícita no contrato do provider (suporte, header, âmbito e "
     "retenção) e o envio condicionado a essa declaração."),
]

PUBLISHER = [
    ("Crash antes do journal (projecto intacto)",
     "test_crash_before_journal_leaves_project_untouched"),
    ("Crash durante backups (projecto intacto)",
     "test_crash_during_backup_leaves_project_untouched"),
    ("Crash após journal PREPARED -> ROLLED_BACK",
     "test_crash_after_journal_rolls_back"),
    ("Crash após PUBLISHING -> ROLLED_BACK",
     "test_crash_after_publishing_rolls_back"),
    ("Crash a meio da aplicação -> não COMMITTED -> ROLLED_BACK",
     "test_crash_mid_apply_never_reports_committed"),
    ("Crash após substituições antes do commit -> ROLLED_BACK",
     "test_crash_after_all_applied_before_commit_rolls_back"),
    ("Crash após commit -> PUBLISHED",
     "test_crash_after_commit_reports_published"),
    ("Crash durante rollback -> retomado -> ROLLED_BACK",
     "test_crash_during_rollback_is_retried"),
    ("Rollback impossível -> RECOVERY_REQUIRED",
     "test_rollback_failure_requires_recovery"),
    ("Directório órfão sem journal detectado",
     "test_orphan_run_dir_is_detected_and_cleaned"),
    ("Limpeza de órfãos não toca em journals",
     "test_orphan_cleanup_does_not_touch_journals"),
    ("Morte real do processo (os._exit) -> recuperação",
     "test_real_process_death_is_recovered"),
    ("Publicação normal -> COMMITTED",
     "test_normal_publish_is_committed"),
]

SAFETY = [
    ("PAYG timeout -> UNKNOWN, sem retry",
     "tests.test_p04_calls", "RouteChainLedgerTests",
     "test_payg_timeout_is_unknown_and_not_retried"),
    ("PAYG 429 -> FAILED e fallback seguro",
     "tests.test_p04_calls", "RouteChainLedgerTests",
     "test_payg_429_is_failed_then_fallback"),
    ("UNKNOWN bloqueia e resolução desbloqueia",
     "tests.test_p04_calls", "EngineCallGateTests",
     "test_unknown_pauses_and_manual_resolution_unblocks"),
    ("Ledger durável + resultado após reload",
     "tests.test_p04_calls", "LedgerTests",
     "test_confirmed_persists_result_and_reloads"),
    ("SUBMITTED + resultado reconciliado",
     "tests.test_p04_calls", "LedgerTests",
     "test_reconcile_promotes_submitted_with_result"),
    ("CONFIRMED recuperado sem nova chamada",
     "tests.test_p04_calls", "RouteChainLedgerTests",
     "test_confirmed_result_replays_without_new_call"),
    ("CONFIRMED sem artefacto bloqueia",
     "tests.test_p04_calls", "EngineCallGateTests",
     "test_confirmed_without_result_pauses_recovery"),
    ("PENDING/SUBMITTED bloqueiam",
     "tests.test_p04_calls", "EngineCallGateTests",
     "test_pending_and_submitted_pause"),
    ("Engine marca call consumido",
     "tests.test_p04_calls", "EngineCallGateTests",
     "test_engine_marks_call_consumed_from_attempt"),
    ("Resolução incompatível rejeitada",
     "tests.test_p04_calls", "LedgerTests",
     "test_resolve_failed_rejects_persisted_result"),
    ("Fingerprint inclui contrato de geração",
     "tests.test_p04_calls", "RouteChainLedgerTests",
     "test_replay_requires_same_generation_contract"),
    ("Resposta estruturada não é replayable",
     "tests.test_p04_calls", "RouteChainLedgerTests",
     "test_structured_response_is_not_replayable"),
    ("History reconciliada sem duplicar evento",
     "tests.test_p04_calls", "EngineCallGateTests",
     "test_history_reconciliation_dedups_critic_event"),
]


def _walk(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from _walk(test)
        else:
            yield test


def _run_one(module_name: str, class_name: str, method: str) -> str:
    module = importlib.import_module(module_name)
    case = getattr(module, class_name)(method)
    result = unittest.TestResult()
    case.run(result)
    if result.skipped:
        return "Skip (deps)"
    return "Verde" if result.wasSuccessful() else "Falha"


def run_scenarios() -> list[tuple[str, str, str, str]]:
    rows = []
    for label, name, dims in SCENARIOS:
        status = _run_one("tests.test_p7_differential",
                          "DifferentialSmokeTests", name)
        rows.append((label, name, dims, status))
    return rows


def run_safety() -> list[tuple[str, str, str, str]]:
    rows = []
    for label, module, cls, method in SAFETY:
        status = _run_one(module, cls, method)
        rows.append((label, f"{cls}.{method}", "", status))
    return rows


def run_publisher() -> list[tuple[str, str, str, str]]:
    rows = []
    for label, method in PUBLISHER:
        status = _run_one("tests.test_p061_recovery", "RecoveryTests", method)
        rows.append((label, f"RecoveryTests.{method}", "", status))
    return rows


def render(rows, safety, publisher) -> str:
    lines = [
        "# P7 differential equivalence report (P7.1d)",
        "",
        "Generated by `tools/p7_differential_report.py`. Reference = "
        "`FactoryAdapter`; candidate = `NativeAdapter`. Both receive the same "
        "LLM cassette and verifier script, use separate workspaces, and publish "
        "only into disposable clones.",
        "",
        "## Gate",
        "",
        "The gate is **closed**. Differential equivalence must be green in both "
        "paths with no billing/security/verification divergence. The PAYG "
        "recovery protocol and the publisher crash-recovery matrix are covered; "
        "provider-declared idempotency headers remain pending.",
        "",
        "## Scenarios",
        "",
        "| Cenário | Estado | Teste | Dimensões comparadas |",
        "|---|---|---|---|",
    ]
    for label, name, dims, status in rows:
        lines.append(f"| {label} | {status} | `{name}` | {dims} |")
    lines += [
        "",
        "## Billing safety (PAYG ledger)",
        "",
        "| Caso | Estado | Teste |",
        "|---|---|---|",
    ]
    for label, name, _dims, status in safety:
        lines.append(f"| {label} | {status} | `{name}` |")
    lines += [
        "",
        "## Publisher crash-recovery (P6.1)",
        "",
        "Policy: recovery is **rollback-only**. A crash after every file was "
        "replaced but before COMMITTED resolves to ROLLED_BACK, never a false "
        "PUBLISHED. A crash before the journal is created leaves the project "
        "untouched (no publication attempt is recorded).",
        "",
        "| Fronteira | Estado | Teste |",
        "|---|---|---|",
    ]
    for label, name, _dims, status in publisher:
        lines.append(f"| {label} | {status} | `{name}` |")
    lines += [
        "",
        "## Comparator",
        "",
        "- artefactos: conteúdo integral por ficheiro (hashes normalizados pelo "
        "conteúdo, sem caminhos temporários);",
        "- estado: fases/transições e checkpoint VERIFY;",
        "- tarefas: id, status, dependências e erros relevantes;",
        "- tentativas: sequência, modelo, endpoint, family, billing, "
        "credential_profile e outcome (UUIDs/timestamps excluídos);",
        "- produtor: `ModelRef` completo por tarefa;",
        "- verificação: verdict, oracle/passed, critic e autorização "
        "`UNVERIFIED` persistida;",
        "- integração: relatório e `fix_records` + revalidações;",
        "- publicação: manifest/hashes, ficheiros publicados, journal, conflito "
        "e recusa.",
        "",
        "## Ainda não coberto (gate fechado)",
        "",
        "| Item | Requisito em falta |",
        "|---|---|",
    ]
    for label, detail in PENDING:
        lines.append(f"| {label} | {detail} |")
    lines += [
        "",
        "## Regenerar",
        "",
        "```bash",
        ".venv/bin/python tools/p7_differential_report.py",
        "```",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    rows = run_scenarios()
    safety = run_safety()
    publisher = run_publisher()
    out = ROOT / "docs" / "p7-differential-report.md"
    out.write_text(render(rows, safety, publisher), encoding="utf-8")
    failed = [r for r in rows + safety + publisher if r[3] == "Falha"]
    print(f"wrote {out}")
    for label, _name, _dims, status in rows + safety + publisher:
        print(f"  {status:10} {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
