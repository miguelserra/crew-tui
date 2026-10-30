# P2 gate report — unified contracts and schema v3

Reference points:

- `p1-gate` — P1 foundation.
- P2 code — `48f933f` (contracts/schema) and `760320f` (DAG boundary).
- `p2-gate` — tag on this report commit.

Scope: P2 only. No P3 routing/registry, P4 escalation, P5 verification
policy, P6 profiles or P7 legacy removal was changed.

## Canonical model

`ProjectState` is the aggregate root and the only in-memory source of truth:

| Contract | Responsibility |
|---|---|
| `RequirementContract` | objective, scope, constraints, assumptions and acceptance criteria (`AcceptanceCriterion` with `task_ids`/`justification`) |
| `ArchitectureContract` | modules, interfaces, test plan; untyped legacy keys stay in `raw` |
| `Task` | kind, file, role/complexity, acceptance, dependencies, `requirement_ids`, `legacy_extra` |
| `Approval` | `spec` and `architecture` approvals bound to deterministic hashes |
| `VerificationRecord` | verdict, `criterion_ids`, oracle/critic/visual/integration history |

## The four P2 rules

### A. Legacy formats are compatibility views

- `.factory/state.json` is written as a compatibility view of `ProjectState`
  (`factory_compat_dict`) and read back through `from_legacy_factory`.
- New legacy runs and `.factory` resumes execute through `FactoryAdapter` with
  `ProjectState` in memory; the legacy `Factory` is only the step adapter.
- Unmapped fields are preserved in `ProjectState.extensions`, contract `raw`
  and `Task.legacy_extra`.
- The transitional Orchestrator still uses `SpecContract` for read-only
  review/questions until P7; build and `.factory` execution paths use the
  canonical model.

### B. Architecture approval covers the work plan

`ProjectState.architecture_hash()` is deterministic and covers:

- the architecture contract (approach/modules/interfaces/test plan);
- every task plan field: title/description, kind/team/role/complexity, target
  file, acceptance, dependencies and `requirement_ids`;
- the criterion → task traceability mapping and justifications.

Spec approval is also hash-bound to requirements. Progress (status/attempts)
does not invalidate approvals; any plan change does.

### C. Requirement → Architecture → Task → Verification

`link_traceability` fills mappings (explicit ones are preserved); unmatched
criteria get an explicit `auto: sem tarefa correspondente no plano`
justification. `validate_traceability` rejects unknown task/criterion ids and
asymmetric links. The task DAG rejects duplicate ids, missing dependencies,
self-dependencies and cycles at the canonical state boundary.

### D. Schema control

Canonical schema is **v3**:

- `--migrate-schema` converts `.crew` v2 → v3 explicitly, with a snapshot and
  transactional write; v2 is never upgraded silently on load.
- `--migrate-state` converts `.factory` → `.crew` v3, preserving the engine.
- `--migrate-engine` requires v3, validates the state, snapshots and only then
  flips `engine_kind`.
- Legacy `.factory` remains schema v1 as a compatibility view; canonical
  `.crew` is v3.

## Exit gate evidence

| Gate | Evidence |
|---|---|
| 1. lossless contract round-trips | `tests/test_p02_contracts.py` (criteria, tasks, attempts, approvals, verifications, extensions, `legacy_extra`) |
| 2. legacy states of all phases | `tests/test_p02_schema.py` (all phases v2→v3), `tests/test_p02_legacy.py` (all factory steps/session) |
| 3. task DAG | `validate_dependencies` in `ProjectState`; missing/cycle tests |
| 4. approval invalidation | `tests/test_p02_traceability.py` (modules, interfaces, test plan, targets, acceptance, deps, roles, criteria) |
| 5. acceptance traceability | link/validate tests; every criterion has task(s) or justification |
| 6. ProjectState canonical | build/unified/legacy `.factory` paths use `ProjectState`; `.factory` is a compatibility serialization |
| 7. Linux + Windows | CI `41114653709`: **success** (Linux 3.10/3.12/3.13, Windows 3.13, integration Linux+Windows) |
| 8. legacy execution | fake-server smoke: legacy full run, `.factory` resume, unified full run |

## Tests

| Suite | Result |
|---|---|
| `test_p01_*.py` core | 79 tests, OK (12 integration skips without deps) |
| `test_p02_*.py` core | 34 tests, OK (2 integration skips without deps) |
| `test_p0*_integration.py` (venv) | 14 tests, OK, 4 expected failures |
| power + budget | OK |
| `py_compile` / `git diff --check` | OK |

Expected failures remain intentionally deferred to P4/P5 (hard routing,
cross-family critic, critic unavailable, local test advisory).

## Manual smoke (local fake OpenAI-compatible server, no external keys)

- Unified: `.crew` v3, `DONE`, approvals `spec`+`architecture`,
  criterion `ac1` linked to `t1`, task `requirement_ids=[ac1]`,
  `delivered_by=fake-model`, 1 attempt.
- Legacy: `.factory` schema 1 compatibility view, same task link and
  approvals; no `.crew` created.
- `.factory` resume with `CREW_ENGINE=unified`: warning, persisted legacy
  engine used, `DONE`; `.factory` preserved.
- v2 `.crew`: resume refused with `--migrate-schema` instruction;
  `--migrate-schema` then `--migrate-engine` succeeded with snapshots.
- Approval/dependency mutation tests and corrupt-state preservation are green.

## Deferred / risks

- Orchestrator review and follow-up Q&A remain a compatibility path until P7;
  they do not write canonical project state.
- Auto-justified unmapped criteria are marked `auto:` and should be reviewed
  by the P5 verification policy.
- P3 registry/pricing, P4 routing/fallback-vs-escalation, P5 critic/oracle/
  visual, P6 profiles/deterministic scaffold and P7/P8 cleanup are untouched.
