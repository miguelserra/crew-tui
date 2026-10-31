# D09 integration and sign-off audit (P7.1d)

Audited at `3d3c4ab` before D09 production changes. This document records the
initial contract, the reproducible gaps and their final status.

## Existing Factory integration/sign-off behaviour

- `Factory.step_integrate()` runs the detected entrypoint, loops up to
  `max_verify_loops + 1`, asks a fixer/chief for `### FILE:` blocks and re-runs
  the entrypoint.
- `Factory._integration_fix()` writes every returned file and sets the matching
  task to `done` immediately. It does **not** run the oracle or critic again,
  does not invalidate the previous `task.verification`, and does not record a
  per-file revalidation result. This is a verification bypass for changed files.
- Integration evidence is the last `_run_entry` report only. There is no
  `fix_records` history comparable to the native service.
- `Factory.step_signoff()` writes `project_memory.json` and returns `True`
  unconditionally; it does not consult any approval.
- `FactoryAdapter` stores `recovery_error` from `PublishManager.recover()` but
  never checks it before phases, unlike `NativeAdapter._ensure_ready()`.

## Existing Native integration/sign-off behaviour

- `IntegrationService.integrate()` loops similarly, calls `_fix()` and re-runs
  the entrypoint.
- `IntegrationService._fix()` parses multi-file blocks, attributes the shared
  call to every touched task, re-runs `_revalidate()` for every touched task,
  stores a `VerificationRecord`, updates `task.status`/`delivered_by`, and
  appends a `fix_records` entry with revalidation outcomes.
- `_revalidate()` runs oracle + critic + optional advisory tests and uses the
  same `verify.decide_build` gate as BUILD. A failed revalidation blocks
  integration and leaves the task `failed`.
- `SignoffService.signoff()` writes `project_memory.json`; it also has no
  human approval or rejection contract.

## Shared contracts

- Canonical state is `ProjectState`; `Approval` already has
  `kind/artifact_hash/decision/actor/timestamp/detail`.
- `VerificationRecord` carries oracle, critic, visual, tests, reason and
  `authorized_unverified`.
- `verify.decide_build` owns PASS/FAIL/UNVERIFIED policy.
- The architecture approval already uses a deterministic `sha256_json` hash and
  exact-hash matching; there is no canonical sign-off approval yet.
- `WorkflowEngine.step("SIGNOFF")` calls the adapter, optionally publishes, then
  moves to `DONE`. It never checks a sign-off approval.

## Differences in state transitions

- Successful integration: both adapters set `project.integration` and may reach
  the next phase, but Factory can mark changed tasks `done` without fresh
  evidence.
- Integration failure: both return `False`; engine pauses with
  `integrate_failed`. Neither is sign-off eligible.
- Sign-off: both write the memory file and return success; neither can represent
  approval, rejection or missing approval.
- Recovery pending: Native refuses the step; Factory ignores the recorded
  recovery error.

## Evidence persistence and identity rules

- Native revalidation overwrites `project.verifications[task.id]` and
  `task.verification`, preserving the prior failed attempt in `fix_records` and
  `task.attempts`.
- Factory currently leaves stale verification attached to a changed file.
- No sign-off evidence hash exists; approval cannot be associated with the
  artifact/verification state.
- Timestamps, UUIDs and temporary paths are allowed variance; verdicts,
  artifact hashes, task identities, rejection/approval events and model calls
  are not.

## Approval and rejection contracts (as found)

- Existing approvals cover `spec`, `architecture` and `unknown_billing`.
- Decisions are append-only in `ProjectState.approvals`; approved and rejected
  decisions can coexist.
- There is no `signoff` approval kind, no sign-off rejection path, no
  sign-off approval hash, and no engine gate for either.

## Current DONE transition requirements (as found)

`WorkflowEngine.step()` reaches `DONE` when the SIGNOFF adapter step succeeds,
optional publication succeeds, and the phase order advances. Missing approval
does not block `DONE`. This violates the D09 gate-bypass requirement.

## Missing differential scenarios

- D09-01A/B: integration success/failure through both adapters and WorkflowEngine.
- D09-02A/B/C/D: fixer changes one/multiple files; revalidation failure; fixer
  exhaustion. Factory is currently divergent.
- D09-03A/B/C: valid sign-off approval, evidence completeness, artifact change
  invalidation; no sign-off contract exists.
- D09-04A/B/C: rejection, rejection followed by approval, technical inability;
  no rejection contract exists.
- D09-05A-F: oracle FAIL, critic FAIL, PAYG UNKNOWN, publisher recovery,
  incomplete integration and missing approval must block sign-off/DONE.
- History comparison currently stops at BUILD and does not compare rejection or
  approval events.

## Exact files requiring changes

- `factory.py`: revalidate every integration-fixed file; record fix evidence;
  attribute the fixer model; preserve failure.
- `engine.py`: enforce recovery readiness in `FactoryAdapter`; add sign-off
  approval/rejection methods and the SIGNOFF→DONE gate; preserve rejection
  history.
- `state.py`: add deterministic sign-off evidence hash, eligibility blockers,
  approval/rejection helpers using the existing `Approval` contract.
- `tests/test_p7_pipeline_adapters.py` or a new D09 test module: differential
  scenarios via `WorkflowEngine` and both adapters.
- `tools/p7_differential_report.py`, `docs/p7-differential-report.md`,
  `docs/p7-inventory.md`: synchronize D09 evidence and matrix counts.

## Resolution status

| Gap | Status |
|---|---|
| Factory marked changed files done without revalidation | Resolved: per-file oracle+critic revalidation, artifact hash and `fix_records`; covered by `test_d09_02a/b/c/d` |
| Factory integration fixer model not attributed | Resolved: fixer call uses `__integration_fix__`, records the shared `TaskAttempt` and `ModelRef`; covered by the D09 fixer tests |
| No sign-off approval/rejection contract | Resolved: canonical `signoff` approvals use the existing `Approval.artifact_hash` contract over deterministic evidence + artifact hashes; covered by `test_d09_03a/b/c` and `test_d09_04a/b/c` |
| SIGNOFF→DONE did not require approval | Resolved: `WorkflowEngine` pauses at `signoff_approval`/`signoff_rejected` until an exact-hash approval exists; covered by `test_d09_01a`, `test_d09_04*`, `test_d09_05f` |
| Factory ignored publisher recovery error | Resolved: `FactoryAdapter._ensure_ready()` now blocks like `NativeAdapter`; covered by `test_d09_05d` |
| D09 differential scenarios missing | Resolved: 18 D09 scenarios through `WorkflowEngine` and both adapters |
| D09 report cells PARTIAL | Resolved: all four D09 dimensions marked COVERED from executed tests; global matrix 19 COVERED / 16 PARTIAL / 0 MISSING / 13 N/A |


## Final D09 evidence

- Integration success/failure, fixer single/multi-file revalidation, revalidation
  failure and fixer exhaustion are compared through both adapters.
- Sign-off approvals are bound to the deterministic evidence hash; artifact
  changes invalidate the previous approval and stale verification blocks a new
  approval.
- Rejections are append-only; a later approval is a separate decision and does
  not overwrite the rejection reason.
- Oracle/critic FAIL, PAYG UNKNOWN, publisher recovery pending, incomplete
  integration and missing approval all block sign-off/DONE.
- No publication occurs before sign-off approval.
