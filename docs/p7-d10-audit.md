# D10 resume and recovery audit (P7.1d)

Audited at `b42424d` before D10 production changes. This document records the
persistence boundaries, resume behaviour and the gaps that require evidence.

## Canonical phase and state transition map

`WorkflowEngine.step()` owns the canonical phase loop:

`SPEC → ARCHITECTURE → SCAFFOLD → BUILD → VERIFY → INTEGRATE → VISUAL → SIGNOFF → DONE`

- `SPEC` persists `RequirementContract` and stops at `spec_questions` or
  `spec_approval`.
- `ARCHITECTURE` persists `ArchitectureContract` + tasks and stops at
  `architecture_approval`.
- `SCAFFOLD` is deterministic from the approved plan.
- `BUILD` writes tasks, attempts and `VerificationRecord`s.
- `VERIFY` is a checkpoint over persisted verification evidence.
- `INTEGRATE` runs the entrypoint, may fix files and must revalidate changes.
- `VISUAL` stores the audit/vision report.
- `SIGNOFF` writes project memory and now requires an exact evidence-hash
  approval before publication and DONE.
- `DONE` is a persisted terminal phase.

## Persistence boundaries

`WorkflowEngine.save()` is the only canonical persistence boundary. The adapter
is an in-memory view; `FactoryAdapter` uses an ephemeral Factory whose `save()`
is a no-op, and the engine persists `ProjectState` after each phase.

- SPEC: requirements are saved before the human pause. A crash before the pause
  is written leaves phase `SPEC` with persisted requirements.
- ARCHITECTURE: architecture/tasks are saved before the approval pause. A crash
  before the pause leaves phase `ARCHITECTURE` with no approval.
- SCAFFOLD: files are written by the adapter; `ProjectState.scaffold` is saved
  by the engine after the step. Re-running scaffold is deterministic/idempotent.
- BUILD: the provider result, ledger, `TaskAttempt` and verification are
  persisted within the step; the engine saves the phase transition after it.
- VERIFY: no external operation; persisted verification records are the source
  of truth.
- INTEGRATE: the workspace/subprocess/fixer side effects are not transactional
  with `ProjectState`; the integration report is saved only after the step.
- VISUAL: browser/screenshot side effects are not transactional; the report is
  saved after the step.
- SIGNOFF: project memory is written before the approval pause; approvals are
  persisted by `approve_signoff()`/`reject_signoff()`.
- PUBLICATION: publisher journals are durable independently of `ProjectState`;
  the publish result is stored in `extensions["publish"]` after publication.

## Existing resume behaviour by adapter

- Both adapters share `WorkflowEngine.save`, `resume()` and the call-ledger gate.
- SPEC and ARCHITECTURE have early fast-forward paths only when the human
  approval already exists.
- BUILD skips tasks already `done`/`verified`; D06 already proves CONFIRMED
  replay and attempt de-duplication.
- VERIFY checks verdict/missing evidence but not whether the persisted evidence
  still matches the current artifact content.
- INTEGRATE revalidates files touched by the current fixer call, but a crash
  after a previous fixer write and before revalidation leaves a changed
  workspace with old in-memory evidence; the next process has no fixer record.
- VISUAL stores a report but no artifact hash tying the visual verdict to the
  HTML/CSS/JS version.
- SIGNOFF approval/rejection is append-only and exact-hash bound (D09), but a
  restart after approval still re-enters `SignoffService`/Factory sign-off and
  regenerates project memory before publication.
- Publisher recovery is fail-closed on `RECOVERY_REQUIRED`; a recovered
  `COMMITTED` journal is not replayed, but the engine may replace its metadata
  with an empty no-op publish result.

## External operations and replay contracts

| Operation | Contract |
|---|---|
| Free provider generation | safe to repeat only when no durable attempt/result exists |
| PAYG provider generation | ledger intent/confirm; UNKNOWN blocks; CONFIRMED replays from result artifact |
| Oracle/critic | deterministic evidence; repeat is safe but must not overwrite newer artifact evidence |
| Integration subprocess | repeat is safe; file changes require revalidation |
| Integration fixer | provider call; file write must be treated as untrusted until revalidated |
| Visual browser | repeat is safe but evidence must be associated with the artifact version |
| Sign-off memory generation | safe to repeat, but approved evidence should allow resume without another call |
| Publication | journal-driven; COMMITTED is terminal and must not be replayed as a new publication |

## Existing fault injection mechanisms

- `SimulatedCrash` for in-process ledger boundaries after CONFIRMED and before
  attempt/consumption (`tests/test_p7_pipeline_adapters.py`).
- Publisher checkpoint callbacks and real `os._exit` subprocess tests in the
  publisher suite.
- Deterministic transport and verifier scripts for Factory/Native.
- No workflow-level real-process crash harness exists for D10 phase boundaries.

## Missing interruption scenarios

- SPEC persisted but approval pause not written.
- ARCHITECTURE persisted but approval pause not written.
- SCAFFOLD crash before/during/after deterministic creation.
- BUILD with real process exit after CONFIRMED before attempt, and after attempt
  before `mark_consumed`.
- VERIFY with stale artifact evidence and with missing evidence.
- INTEGRATE after a fixer file write but before revalidation.
- VISUAL crash before/after report persistence and artifact change after report.
- SIGNOFF crash after approval/rejection persistence.
- Publication boundary with RECOVERY_REQUIRED, COMMITTED and ROLLED_BACK
  journals.
- Repeated interruption/resume, multi-phase completion and dependency ordering.

## Known differences between FactoryAdapter and NativeAdapter

- Both now revalidate integration-fixer changes in-process (D09), but only the
  Native service records the fixer call's shared attempt before revalidation;
  the recovery path after a process crash has not been compared.
- Both VERIFY checkpoints only inspect verdicts; neither detects stale artifact
  hashes.
- Both SIGNOFF paths re-run their memory writer on a resume after approval.
- Publisher recovery uses the same shared service but the workflow-level resume
  behaviour has not been compared.

## Exact files requiring changes

- `engine.py`: resume fast-forward without duplicate SPEC/ARCHITECTURE work;
  avoid duplicate SIGNOFF generation after approval; preserve recovered
  publication metadata.
- `state.py`: shared stale-verification / visual evidence checks.
- `integration.py`, `factory.py`: detect and revalidate stale changed files
  before integration after a crash.
- `visual.py`, `factory.py`: bind visual evidence to the artifact hash.
- `tests/test_p7_pipeline_adapters.py` or a new D10 module: phase/resume,
  cross-phase and real-process crash tests.
- `tests/d10_crash_worker.py` (new): subprocess crash worker for mandatory
  real-process boundaries.
- `tools/p7_differential_report.py`, `docs/p7-differential-report.md`,
  `docs/p7-inventory.md`: D10 evidence and matrix synchronization.

## Resolution status

| Gap | Status |
|---|---|
| SPEC persisted without pause can repeat requirement generation | Resolved: persisted requirements pause at the human gate without another model call; `test_d10_01a/b/c` |
| ARCHITECTURE persisted without approval can repeat design | Resolved: persisted plan pauses at approval without redesign; `test_d10_02a` |
| VERIFY does not reject stale artifact hashes | Resolved: shared `verification_blockers` checks artifact hashes; `test_d10_05a/b` |
| INTEGRATE crash after fixer write can accept stale evidence | Resolved: both adapters detect/revalidate stale files and record recovery; `test_d10_06a`, `test_d10_11c` (real process) |
| VISUAL evidence is not bound to artifact hash | Resolved: visual reports carry artifact hashes and stale visual evidence blocks sign-off; `test_d10_07a` |
| SIGNOFF after approval repeats sign-off generation | Resolved: approved resume skips the sign-off writer and publishes directly; `test_d10_08a`, `test_d10_11d` (real process) |
| Recovered COMMITTED publication metadata may be lost | Resolved: recovered COMMITTED journals are returned without a second publish; `test_d10_09a` |
| No workflow-level real-process crash tests | Resolved: six real subprocess crash/restart tests cover the mandatory boundaries; `test_d10_11a-f` |

## Final D10 evidence

- 24 D10 tests through `WorkflowEngine` with both adapters.
- Real subprocess termination/restart: BUILD after CONFIRMED before attempt, BUILD
  after attempt before consumption, INTEGRATE after fixer write before
  revalidation, SIGNOFF after approval, SIGNOFF after rejection, and
  publication with pending recovery.
- PAYG UNKNOWN blocks with one invocation; CONFIRMED is replayed without a new
  provider call; completed tasks are not repeated.
- Stale verification and visual evidence are rejected; integration changes are
  revalidated; approvals/rejections survive reload.
- COMMITTED publication is recovered without duplication; ROLLED_BACK is not
  treated as success; RECOVERY_REQUIRED blocks DONE.
