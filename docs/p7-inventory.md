# P7.0 inventory — execution dependency

Baseline: `p6.1-gate` (`86f2df1`).

## Active runtime references

| Symbol | Current owner | Runtime use |
|---|---|---|
| `Factory` | `factory.py` | instantiated lazily by `engine.FactoryAdapter` |
| `FactoryAdapter` | `engine.py` | unified/legacy workflow adapter |
| `FactoryState` / `TaskState` | `factory.py` | compatibility view for Factory steps |
| `EngineFacade` / `StateView` | `engine.py` | TUI facade over unified state |
| `Orchestrator` / `SpecContract` / `TaskSpec` | `main.py` | review, follow-up Q&A and legacy TUI path |
| `_run_factory_prepare` / `_run_factory_build` | `main.py` | TUI worker entrypoints |
| `resolve_engine` default `legacy` | `engine.py` | new-run engine selector |

## Business logic owned by Factory

- requirements prompt/refinement and answers
- architecture decomposition and interface/task contract
- deterministic scaffold was already extracted to `scaffold.py`
- build prompt, one-file enforcement, compile check, fix loop, critic gate and workspace install
- integration entrypoint/runtime fix loop
- Playwright audit and multimodal visual review/fix
- sign-off/project memory generation
- sign-off approval/rejection evidence and DONE gate (shared `ProjectState` contract)

## Target services

| Service | Responsibility | Status |
|---|---|---|
| `scaffold.py` | deterministic scaffold | extracted (P6) |
| `verify.py` | critic/gate policy | extracted (P5) |
| `workspace.py` / `publish.py` | isolation/publish | extracted (P6/P6.1) |
| `requirements.py` | SPEC + answers | P7.1 first extraction |
| `architecture.py` | architecture + tasks | P7.1 extraction (hardened) |
| `executor.py` | BUILD + fix loop | P7.1b extraction |
| `context_reader.py` | bounded project context | P7.1a extraction |
| `integration.py` | runtime/integration | P7.1c extraction |
| `visual.py` | multimodal | P7.1c extraction |
| `signoff.py` | final sign-off | P7.1c extraction |
| `native_adapter.py` | native composition | P7.1d extraction; default for new projects |
| differential cassette | normal, FAIL→FIX, UNVERIFIED, visual retry, fallback, Playwright, resume, publish parity/conflict, D09 integration/sign-off | D09 success/failure, fixer revalidation, approval/rejection and gate bypass green; PAYG ledger/recovery and publisher crash-recovery covered; idempotency header pending |
| differential report | generated matrix + uncovered items | `docs/p7-differential-report.md` |
| publisher recovery | fault injection at each journal/apply/rollback boundary + real os._exit subprocess | rollback-only; COMMITTED/ROLLED_BACK/RECOVERY_REQUIRED; never false COMMITTED; orphans detected/cleaned explicitly |
| P7.1d gate | D01–D12 matrix | **OPEN**: 36 COVERED / 0 PARTIAL / 0 MISSING / 12 N/A; no DIVERGENT in executed cells |
| full pipeline harness | real Router/RouteChain/CallLedger + fake transport | `tests/test_p7_pipeline.py`: 429 fallback, PAYG timeout→UNKNOWN, CONFIRMED replay; shared-component evidence, not adapter parity |
| adapter full pipeline | WorkflowEngine + both adapters + real Router/RouteChain/CallLedger | `tests/test_p7_pipeline_adapters.py`: D02–D08, D09, D10 phase/cross-phase resume, D11 publication and D12 real publisher crashes; `tests/d10_crash_worker.py` adds six real subprocess crash/restart boundaries |
| `calls.py` | durable PAYG call ledger + result artifacts | PENDING/SUBMITTED/CONFIRMED/UNKNOWN/FAILED; text-only replay with full invocation fingerprint; history dedup; no silent PAYG retry; resume gate |
| integration fixer | one call + affected task ids + producer | both adapters revalidate every changed artefact (oracle+critic), record artifact hashes, recover stale files on resume and invalidate stale PASS |

## Guardrails

- No prompt/model/routing/verification policy changes.
- Legacy data remains readable; no silent conversion.
- Runtime unified path must not import `factory.py` after equivalent extraction.
- Each extraction lands with regression tests before old code is removed.


## P7.2a progress

- Project Brief, adaptive interview, provenance, hash-bound SPEC approval and
  TUI/CLI support implemented.
- 16 P7.2a tests cover minimal/partial brief, adaptive and non-redundant
  questions, blocking/deferred decisions, provenance, revision invalidation,
  approval, resume, legacy compatibility, CLI flags/flow, no fabricated
  requirements and conflicts.
- P7.1d remains OPEN/COMPLETE.
- P7.2b and P7.2c were implemented in later milestones (see the sections below).


## P7.2b.2 reliability hardening

- V01 PAYG reservation/concurrency, V02 Playwright teardown, V04 breaker
  persistence degradation, V05 Windows atomic retry and V06 byte-accurate visual
  hashing implemented with 20 focused tests.
- Rejected findings were not modified; P7.2c discovery was not changed; no
  P7.2c/REA work was started in this milestone.

## P7.2b.3 budget integrity / visual migration

- V01 accounting model made explicit (estimated/reserved/uncertain/confirmed/
  overrun), idempotent commit/release, commit-after-release rejected, actual
  cost never truncated, ambiguous outcomes held as uncertain exposure.
- V06 visual hashes versioned per record (`{"v": 2, "sha": ...}`) with legacy
  normalized-text interpretation and fail-closed re-audit; no global schema
  bump.
- V05 native filesystem tests added to every CI job; native Windows CI still
  NOT_VERIFIED (no push authorized).
- See `docs/p7-2b3-design.md`. 28 new focused tests; full Linux suite 498 green.

### P7.2b.3.1 Big Pickle remediation

- F1: HELD (ambiguous) exposure is only released on an explicit resolution, not
  by a later definitive failure.
- F2: repeated compatibility `charge()` is idempotent and cannot double-count.
- F4: malformed visual-hash version metadata fails closed (no leaked exceptions).
- F7: a present-but-empty/null/malformed visual hash blocks sign-off.
- F8: real Router + real CallLedger ambiguous-outcome integration test added.
- Budget lifetime is explicitly classified PROCESS_SCOPED; `max_cost` is a
  library-only ceiling (production passes `max_cost=None`) and is not
  reconstructed across resume. See `docs/p7-2b3-design.md`.
- Full Linux suite 517 green; native Windows CI still NOT_VERIFIED.

## P7.2c.1 Discovery Core completion

- `discovery.py` already provided a full static P7.2c implementation; the core
  was audited and the P7.2c.1 gaps were filled additively without removing the
  pre-existing dossier/freshness/CLI/TUI/engine integration.
- Added structured `project` (name provenance + description), enriched
  `repository` (git_available/dirty/dirty_paths), `structure`
  (modules/classes/functions/interfaces, plus `unsupported_source_suffixes` for
  inventory-recognised source languages without a structural extractor), `tests`,
  dependency `manifests/declared_dependencies/unresolved_formats`, and `evidence`
  (coverage + item index).
- Fixed TOML/requirements manifest parsing and framework evidence (no empty or
  substring-based evidence).
- Discovery remains static/bounded and reuses `adoption.inspect_repository`;
  no execution, installation, network or cloud calls.
- See `docs/p7-2c1-core-audit.md`. `tests/test_p72c_discovery.py` now has 27
  tests (7 pre-existing + 13 P7.2c.1 core + 7 P7.2c.1.1 F-1 classification).
- P7.2c.2+ (dossier/agent integration refinements) remain future work unless
  already present; no new milestone started here.

## P7.2c.2 agent understanding / context retrieval

- Added a bounded, deterministic, **lexical** retrieval layer over the existing
  `p7-2c/2` dossier (no embeddings, no vector store, no model, no network):
  `retrieve_context`, `get_project_summary`, `get_evidence`, `get_file_context`,
  `get_symbol_context`, `get_component_context`, plus dotted-path `section()`
  and an extended (backward-compatible) `summary_for_agent()`.
- Results preserve evidence IDs and epistemic classification
  (OBSERVED/INFERRED/UNKNOWN/CONFLICTING); `epistemic_filter` is supported.
- Bounds are **strict** (P7.2c.2.1): `max_chars` bounds the complete
  `json.dumps(response, ensure_ascii=False)` including metadata/escaping/markers;
  `max_chars_per_result` bounds each result entry; a minimum-envelope overflow
  returns a documented `budget_too_small` error within budget. Limit types are
  validated (`TypeError` for non-int/bool/None), out-of-range ints clamp.
- Freshness is **last-recorded provenance** (P7.2c.2.1): retrieval returns
  `source="last_recorded"`, `live=false`, `generated_at`, `checked_at`; it never
  re-scans the repository and never refreshes the dossier. Missing/invalid
  dossier or missing/malformed metadata yields `UNKNOWN`, never `FRESH`; `STALE`
  is preserved. Generation records an initial assessment honouring hash coverage.
- `summary_for_agent()` restores the legacy risks-by-default behaviour
  (`include_risks=False` opts out).
- `WorkflowEngine` gained additive delegation methods; routing, RouteChain,
  CallLedger, PAYG billing and adapter selection are unchanged.
- Deferred: agent tool/function-calling for mid-generation retrieval (no such
  interface exists); semantic search; per-section live freshness. Retrieval
  contract: empty query returns no matches even with `symbols`; nonempty
  `symbols` is an exact case-insensitive name filter, while `task_context` is
  echoed but does not affect ranking. See `docs/p7-2c2-design.md`.
- See `docs/p7-2c2-design.md` and `docs/p7-2c2-audit.md` (the latter is a
  historical Phase-0 snapshot). `tests/test_p72c2_retrieval.py` has 55 tests,
  including process-reload persistence coverage for freshness provenance.


## P7.2c.3 deterministic workflow context (Stage B)

- Opt-in `workflow_context` core from Stage A is now wired into generation:
  native `ArchitectureService.design` / `ExecutorService._build_task` and Factory
  `step_architect` / `_build_one`, through `WorkflowEngine.apply_pool` attachment.
- Explicit human consent is persisted with `ContextActivation`; CLI
  `--context-consent` (model id or full identity key) plus `--context-phases`
  (default ARCHITECTURE,BUILD). `--yes`, `--allow-paid` and existing approvals
  never imply disclosure consent. Activation is disabled by default.
- Disclosure is limited to provider identities in the allowlist for the whole
  candidate chain; otherwise the exact unchanged base prompt is used and the
  omission (`disclosure_denied`) is recorded in the snapshot.
- Enabled prompt blocks are bounded, structured, evidence-linked and labelled as
  historical untrusted data. Existing direct workspace reads remain authoritative.
- BUILD installs generated text idempotently and refuses to overwrite
  human/other-writer edits (`workspace.apply_generated_text`). Application and
  the `APPLIED` marker are persisted before the managed call is consumed.
- Managed BUILD with a definitive verification failure now runs the existing
  automatic FIX + re-verification (NEW-01). The confirmed BUILD result is
  recovered locally and never re-submitted to a provider; only the accepted
  final artifact is applied/consumed. Exhausted FIX, ambiguous provider
  outcomes, filesystem conflicts and other transactional uncertainty remain
  fail-closed/recoverable. Disabled mode keeps the legacy flow unchanged.
- Tests: `tests/test_p72c3_core.py` (35), `tests/test_p72c3_integration.py` (20),
  plus `tests/context_crash_worker.py` real-process boundaries. CI runs
  `test_p72c3_*.py` on Linux, Integration Linux/Windows and Windows.
- Limitations: lexical retrieval, conservative byte-based context budget (not a
  tokenizer), last-recorded freshness, no dynamic tool calling, and no generic
  atomic transaction across multiple workspace files plus ProjectState.
- Release hardening: NEW-02 documents the intentionally different tiny-budget
  payload priorities of component and project-summary APIs; NEW-03 documents
  symbol-filter semantics; NEW-04 verifies freshness status/provenance across a
  fresh Python process; NEW-05 verifies `task_context` is metadata only, not a
  ranking input.
- Latest release-hardening Linux regression: 647 tests, 0 failures/errors,
  12 skipped (the Windows-only `test_power_win.py` module remains excluded).
  Hosted CI for these uncommitted changes is NOT_VERIFIED until an authorized
  push.
