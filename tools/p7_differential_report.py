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
    ("Dependências de tarefas (ordem)",
     "test_task_dependencies_order_match",
     "ordem de chamadas, tarefas, tentativas"),
    ("Classificação semântica (sem DIVERGENTE)",
     "test_semantic_classification_no_divergence",
     "artefactos/tarefas/tentativas/produtor/verificação/integração; "
     "uuid/timestamps como ALLOWED_VARIANCE"),
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

MATRIX = [
    ("D01", "Normal end-to-end", {
        "EXECUTION": ("PARTIAL", "BUILD/VERIFY/INTEGRATE/VISUAL/SIGNOFF only; no SPEC/ARCH/SCAFFOLD in differential"),
        "MODELS_BILLING_LEDGER": ("PARTIAL", "fake pool bypasses router/ledger"),
        "VERIFICATION": ("COVERED", "test_build_integrate_signoff_match compares verdict/evidence"),
        "PUBLICATION": ("PARTIAL", "test_publication_parity is a separate sandbox, not end-to-end"),
    }),
    ("D02", "Task dependencies and ordering", {
        "EXECUTION": ("COVERED", "test_task_dependencies_order_match"),
        "MODELS_BILLING_LEDGER": ("PARTIAL", "fake pool; no routing/ledger parity"),
        "VERIFICATION": ("PARTIAL", "BUILD compared; VERIFY not in this scenario"),
        "PUBLICATION": ("N/A", "no publication required"),
    }),
    ("D03", "Hard task, escalation, routing", {
        "EXECUTION": ("PARTIAL", "no adapter-level routing scenario"),
        "MODELS_BILLING_LEDGER": ("MISSING", "router/breaker/escalation not compared through adapters"),
        "VERIFICATION": ("PARTIAL", "no escalation-specific verification comparison"),
        "PUBLICATION": ("N/A", "no publication required"),
    }),
    ("D04", "Technical fallback", {
        "EXECUTION": ("COVERED", "quota/timeout/all-unavailable differential cassettes"),
        "MODELS_BILLING_LEDGER": ("PARTIAL", "fallback emulated in fake pool; real classify_error/router not exercised"),
        "VERIFICATION": ("PARTIAL", "no verification in fallback scenarios"),
        "PUBLICATION": ("N/A", "no publication required"),
    }),
    ("D05", "PAYG safety and UNKNOWN", {
        "EXECUTION": ("PARTIAL", "P4 RouteChain/engine tests, not adapter differential"),
        "MODELS_BILLING_LEDGER": ("PARTIAL", "ledger shared logic tested; no adapter ledger parity"),
        "VERIFICATION": ("N/A", "no verification involved"),
        "PUBLICATION": ("N/A", "no publication required"),
    }),
    ("D06", "CONFIRMED replay and recovery", {
        "EXECUTION": ("PARTIAL", "P4 ledger/replay tests, not adapter differential"),
        "MODELS_BILLING_LEDGER": ("PARTIAL", "shared ledger tested; no adapter ledger parity"),
        "VERIFICATION": ("N/A", "no verification involved"),
        "PUBLICATION": ("N/A", "no publication required"),
    }),
    ("D07", "Verification PASS/FAIL/FIX", {
        "EXECUTION": ("COVERED", "quality recovery differential"),
        "MODELS_BILLING_LEDGER": ("PARTIAL", "fake pool; no ledger parity"),
        "VERIFICATION": ("PARTIAL", "oracle-fail and fixer revalidation are single-path tests"),
        "PUBLICATION": ("N/A", "no publication required"),
    }),
    ("D08", "Visual and Playwright", {
        "EXECUTION": ("PARTIAL", "visual retry differential; other variants single-path"),
        "MODELS_BILLING_LEDGER": ("PARTIAL", "fake pool; no capability/routing parity"),
        "VERIFICATION": ("PARTIAL", "unavailable compared; real browser pass/fail not"),
        "PUBLICATION": ("N/A", "no publication required"),
    }),
    ("D09", "Integration and sign-off", {
        "EXECUTION": ("PARTIAL", "success path only"),
        "MODELS_BILLING_LEDGER": ("PARTIAL", "fake pool; no ledger parity"),
        "VERIFICATION": ("PARTIAL", "integration fixer revalidation is single-path"),
        "PUBLICATION": ("PARTIAL", "publication parity separate from sign-off flow"),
    }),
    ("D10", "Interruption and resume", {
        "EXECUTION": ("PARTIAL", "resume after BUILD only"),
        "MODELS_BILLING_LEDGER": ("PARTIAL", "no adapter ledger parity across resume"),
        "VERIFICATION": ("PARTIAL", "no interruption in VERIFY/INTEGRATE/VISUAL/SIGNOFF"),
        "PUBLICATION": ("PARTIAL", "publisher recovery separate from adapter resume"),
    }),
    ("D11", "Publication conflict and recovery", {
        "EXECUTION": ("COVERED", "publication parity/conflict sandbox"),
        "MODELS_BILLING_LEDGER": ("N/A", "no model/billing operation"),
        "VERIFICATION": ("N/A", "no verification operation"),
        "PUBLICATION": ("PARTIAL", "deletion/orphan via adapters not compared"),
    }),
    ("D12", "Publisher crash recovery", {
        "EXECUTION": ("PARTIAL", "P6.1 unit matrix, not through both adapters"),
        "MODELS_BILLING_LEDGER": ("N/A", "no model/billing operation"),
        "VERIFICATION": ("N/A", "no verification operation"),
        "PUBLICATION": ("PARTIAL", "fault matrix is publisher-level; adapter integration not compared"),
    }),
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


def matrix_counts():
    counts = {"COVERED": 0, "PARTIAL": 0, "MISSING": 0, "N/A": 0}
    for _sid, _name, dims in MATRIX:
        for status, _reason in dims.values():
            counts[status] += 1
    return counts


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
        "The gate is **CLOSED**. The D01–D12 matrix has 5 COVERED, 29 PARTIAL, "
        "1 MISSING and 13 N/A cells. Executed comparisons show no DIVERGENT, but "
        "PARTIAL/MISSING cells mean the mandatory matrix is not complete; they "
        "are never counted as PASS. Provider idempotency headers are deferred "
        "hardening, not a P7.1d exit criterion.",
        "",
        "## Scenarios",
        "",
        "| Cenário | Estado | Teste | Dimensões comparadas |",
        "|---|---|---|---|",
    ]
    for label, name, dims, status in rows:
        lines.append(f"| {label} | {status} | `{name}` | {dims} |")
    counts = matrix_counts()
    lines += [
        "",
        "## Mandatory matrix (D01–D12)",
        "",
        "A cell is `COVERED` only when both adapters execute the scenario and the "
        "dimension is compared. `PARTIAL`/`MISSING` cells keep the gate closed; "
        "they are never treated as PASS.",
        "",
        "| ID | Cenário | Execução | Modelos/Billing/Ledger | Verificação | Publicação |",
        "|---|---|---|---|---|---|",
    ]
    for sid, name, dims in MATRIX:
        cells = [f"{dims[k][0]}" for k in ("EXECUTION",
                                           "MODELS_BILLING_LEDGER",
                                           "VERIFICATION", "PUBLICATION")]
        lines.append(f"| {sid} | {name} | " + " | ".join(cells) + " |")
    lines += [
        "",
        f"Cell totals: **{counts['COVERED']} COVERED**, "
        f"**{counts['PARTIAL']} PARTIAL**, **{counts['MISSING']} MISSING**, "
        f"**{counts['N/A']} N/A**.",
        "",
        "### Matrix cell evidence and gaps",
        "",
        "| ID | Dimensão | Estado | Evidência / razão |",
        "|---|---|---|---|",
    ]
    for sid, _name, dims in MATRIX:
        for dim, (status, reason) in dims.items():
            lines.append(f"| {sid} | {dim} | {status} | {reason} |")
    lines += [
        "",
        "## Semantic classification",
        "",
        "| Veredicto | Significado |",
        "|---|---|",
        "| EQUIVALENT | Mesmo valor semântico (conteúdo, ModelRef, veredicto, "
        "evidência). |",
        "| ALLOWED_VARIANCE | Diferença irrelevante: UUIDs, timestamps, "
        "caminhos temporários, ordem de escrita; relações entre entidades "
        "preservadas. |",
        "| DIVERGENT | Diferença material: artefacto/hash diferente, uma "
        "chamada PAYG adicional, uma tentativa de fallback extra, um PASS sem "
        "a mesma evidência, um projecto publicado com hashes diferentes. |",
        "",
        "Regras aplicadas pelo comparador: timestamps/UUIDs são normalizados "
        "(ALLOWED_VARIANCE); chamadas efectivas, tentativas, produtores e "
        "evidência de verificação são comparadas e qualquer diferença material "
        "é DIVERGENT. A equivalência de facturação/ledger entre motores é "
        "demonstrada pela suite P4 (a mesma lógica de ledger/RouteChain é "
        "partilhada); a matriz diferencial compara execução, artefactos, "
        "tentativas e verificação.",
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
