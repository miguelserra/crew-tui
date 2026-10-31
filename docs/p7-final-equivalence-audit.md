# Final equivalence audit (P7.1d residual cells and D01)

Audited at `533638a` before closing the final cells.

## Baseline

Matrix: **30 COVERED / 6 PARTIAL / 0 MISSING / 12 N/A**.
Remaining PARTIAL: D01 EXECUTION, D01 MODELS_BILLING_LEDGER, D01
PUBLICATION, D02 VERIFICATION, D04 VERIFICATION, D07
MODELS_BILLING_LEDGER.

## D02 verification gap

Existing D02 tests covered graph rejection, ordering, failed/skipped
dependencies and partial resume, but did not compare verification records,
artifact hashes and oracle/critic outcomes for dependency chains. Added:
verified a→b chain, oracle failure blocks dependent, critic failure blocks
dependent. The oracle-failure test exposed a Factory static-repair extra chief
attempt versus Native; Factory static repair was aligned to one attempt (the
existing Native contract), with regression coverage.

## D04 verification gap

Existing D04 tests covered transport fallback and PAYG ambiguity, but not the
verification of the fallback-produced artifact or fallback + critic failure.
Added fallback artifact verification and fallback → critic FAIL → FIX → PASS
comparisons across both adapters.

## D07 model/billing gap

Existing D07 tests compared transport and final outcomes, but not full
ModelRef/billing/ledger parity for PAYG Oracle FAIL, PAYG UNKNOWN during the
fixer chain, and CONFIRMED replay. Added explicit ledger/attempt/artifact
comparisons through both adapters.

## D01 end-to-end harness

The D01 harness runs a complete isolated lifecycle through `WorkflowEngine`:

SPEC → ARCHITECTURE → SCAFFOLD → BUILD → VERIFY → INTEGRATE → VISUAL →
SIGNOFF → PUBLICATION → DONE.

It uses real Router/RouteChain/CallLedger, real Playwright Chromium for
integration/visual, real vision routing with fake transport, real approval
and real PublishManager. Separate workspaces, states, browser sessions,
screenshots and publication journals are used. The normal lifecycle and a
controlled initial critic failure followed by FIX, persisted resume, approval
and publication are compared.

## Reusable fixtures

D08 real-browser fixture and vision responder; D09 sign-off approval; D10
resume/evidence freshness; D11/D12 publisher and journal fixtures; the existing
PipelinePool and deterministic provider transport.

## Implementation sequence

1. Stage A: run and commit D02/D04/D07 closure tests.
2. Verify those three matrix cells are COVERED and CI is green.
3. Stage B: D01 normal and failure/recovery lifecycle through both adapters.
4. Commit D01, update the final matrix, run full CI and report the gate.


## Stage A result

D02 VERIFICATION, D04 VERIFICATION and D07 MODELS_BILLING_LEDGER are now
COVERED with adapter-level tests. Factory static repair was aligned to one
fixer attempt with a regression test, removing an extra chief attempt that
Native did not perform. Global matrix after Stage A: 33 COVERED / 3 PARTIAL /
0 MISSING / 12 N/A. D01 remains the only open scenario set.
