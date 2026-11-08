# P7.2c.3-A handoff — DeepSeek Flash integration stage

Base: `e921bc5`. Critical core implemented; changes intentionally uncommitted.
Read `docs/p7-2c3-design.md` and `workflow_context.py` before modifying callers.

## Interfaces (do not redesign)

```python
core = engine.context_core()                         # lazy WorkflowContext
core.activate(ContextActivation(
    run_id=state.run_id, phases=("ARCHITECTURE", "BUILD"),
    providers=(identity_key(model_identity), ...), actor="human",
    phase_tokens={"ARCHITECTURE": 4096, "BUILD": 2048},
    model_tokens={}, max_chars=16000, safety_ratio=0.15))

snapshot = core.prepare(
    logical_id, phase, route_result, existing_base_messages,
    target_file=task.target_file if task else "", query=query,
    sections=("structure", "dependencies", "components"))

if snapshot is None:
    # Original behaviour, no managed ledger marker and no modified prompt.
    text = pool.complete(route_result.chain, existing_base_messages, meta=existing_meta)
else:
    def message_factory(role, content):
        return SystemMessage(content) if role == "system" else HumanMessage(content)
    messages = snapshot.render_messages(message_factory)
    result = core.invoke(pool, route_result, logical_id, messages)
    validated = parse_and_validate(result.text)       # existing service validation
    def apply(state, result):
        # Apply already validated output to canonical state. BUILD file
        # installation must be idempotent and detect conflicting human edits.
        apply_validated_result(state, validated, producer=result.served)
    core.commit(logical_id, apply)                   # saves APPLIED, then consumes
```

Recovery entry points:

```python
snapshot = core.store.load(logical_id)       # exact immutable messages; no dossier scan
recovered = core.recover(logical_id)         # local text/ref or None; UNKNOWN raises
core.invoke(pool, route_result, logical_id, snapshot.render_messages(message_factory))
core.commit(logical_id, apply)               # APPLIED: consume only, never reapply
```

Exceptions: `ContextRecoveryRequired` means stop/reload/manual recovery, never
allocate a fresh ID to evade it; `CallOutcomeUnknown` preserves unresolved
ambiguity; `PublishBusy` from the reused lock means another managed operation
owns the run. Failed state/application/consume requires reloading state/core.

## Required integration work

1. Build an explicit consent UI/CLI path. No `--yes`/`allow_paid`/existing approval
   implies disclosure consent. Persist activation through core.activate.
2. Allocate/persist a stable logical generation slot before dispatch. Task ID,
   phase and attempt/revision distinguish operations. Resume must locate that
   slot before reading changed workspace content or building a new prompt.
3. ARCHITECTURE: hook Native `ArchitectureService.design/_ask` and Factory
   `step_architect/_ask`. Preserve requirements and direct workspace reads on
   first prepare. After CONFIRMED recovery, use the immutable request; do not
   regenerate tree/brief into a different provider request.
4. BUILD: hook Native `_build_task` and Factory `_build_one`; supply task, target,
   acceptance and dependencies as a deterministic non-empty lexical query.
   `task_context` alone is NOT ranked by the existing retrieval API.
5. Reuse already selected Router candidates for budget/disclosure. Do not select
   a different provider to bypass disclosure denial. The core sends unchanged
   base messages when any candidate is outside the allowlist.
6. Move parsing/validation and result installation into an application boundary
   appropriate to each existing service. Call core.commit only after validation.
   Its callback must be idempotent for filesystem writes and must not save state
   or call another model. It persists canonical result and APPLIED together.
7. A failed parse/validation leaves the result recoverable; do not auto-consume
   it or allocate a new paid request. Define the explicit service-level handling
   for a rejected completed generation before adding retries/escalations.
8. FIX, CRITIC, TEST, INTEGRATE, VISUAL and SIGNOFF remain their existing paths.
   Their consumption/routing is not managed automatically by this core.

## Producer and recovery details

Use `ManagedResult.text`, `.served`, `.call_id`, `.replayed`, not a guessed first
candidate. Live calls keep the normal LLMPool attempt sink. A local recovery
reconstructs a missing successful attempt through existing engine deduplication;
on_attempt cannot consume managed records. A raw core.recover is local result
access (the existing CallLedger may reconcile a surviving result artefact); it
never changes the dossier or submits to a provider. invoke/commit complete the
attempt/application lifecycle.

The ledger now has optional `context_snapshot_id` (old records load as empty).
This associates every technical fallback call with the same semantic request.
Managed local/subscription calls are durable too, without changing their billing.
Do not repurpose the ledger as a second budget authority.

## Parity and boundaries

* Engine owns the context core and the canonical ProjectState. Configure both
  adapters with the same core; Native services must not import Factory.
* Same dossier/query/budget/candidates/base inputs produce the same selection.
  Compare canonical block and evidence in enabled differential tests. Existing
  Native/Factory base prompts differ; record base hashes as allowed variance,
  never conceal a difference in actual disclosure or capacity.
* Disabled/missing activation must retain the entire existing path. Do not
  globally patch LLMPool, all prompts or provider handles.
* Managed BUILD verification failure must use the existing FIX path (NEW-01);
  only the accepted final artifact is applied/consumed. Transactional
  uncertainty remains fail-closed.
* FIX is not a separate managed context request: a crash after its response but
  before application may repeat FIX on resume. The confirmed BUILD is recovered
  locally, never re-submitted; ambiguous PAYG FIX remains fail-closed.
* Snapshots contain full exact prompt text on local disk, not only new dossier
  context. Consent UX must explain storage, scopes and provider identities.
* Fields with last-recorded FRESH are not live verification; source evidence is
  checked against workspace bytes at selection and labelled historical. A loaded
  snapshot is reused without a new scan or fabricated freshness timestamp.
* Unknown model windows or insufficient space mean zero additional context.
  Byte-based accounting is conservative, not exact tokens. Existing base prompts
  are not silently shortened by the core.

## Gate for integration completion

Add enabled Native/Factory prompt capture tests plus actual service application
crashes before declaring full integration complete. Reuse
`tests/test_p7_pipeline_adapters.py`, D06/D10 workers and the new context fixture.
Test workspace conflicts after response/installation, stable logical slots across
resume, no extra PAYG submission, effective producer, stale/unknown omission,
provider fallback consent and phase/model budgets. Keep all existing tests.

Stage A's tests use real Router, CallLedger, WorkflowEngine and StateManager with
fake transport. Six real subprocess exits cover prepared, submitted, result-file,
confirmed, attempt and APPLIED boundaries. They do not prove complete service
integration (which is deliberately not implemented).

## Files in Stage A

* `workflow_context.py`: contracts, activation, selection, budget, immutable store,
  run locking, request validation, recovery and application commit.
* `calls.py`: optional snapshot link and replay namespace isolation.
* `router.py`: managed guard, link into intents, durable managed non-PAYG calls.
* `engine.py`: lazy core, deferred managed consumption, state-revision handshake.
* `tests/test_p72c3_core.py`, `tests/context_crash_worker.py`: deterministic tests.
* `.github/workflows/ci.yml`: execute `test_p72c3_*.py` in existing jobs.
* This handoff and `docs/p7-2c3-design.md`.

No discovery service, approval hashes, adapter selection, provider price/grant
accounting, publication or legacy schema migration was modified.

## Known limitations

One canonical engine writer is still required; advisory locks coordinate managed
operations and stale state/ledger caches are rejected/refreshed. They do not make
arbitrary legacy writers a distributed system. Snapshot hashes are integrity
checks, not authentication against a process that can rewrite both state and
artefacts. Snapshot size is bounded per request, not total run storage. There is
no encryption or garbage collector. Filesystem installation is not atomically
transacted with ProjectState and must remain idempotent/conflict-aware.

Do not add tool calling, embeddings, parsers, new billing semantics, provider
filtering, automatic freshness refresh, or broad service refactoring. Do not
commit/push without new authorization. Do not ask the next stage to redesign
this protocol; report a failing invariant before extending it.

## Executed validation

Exact commands and final results are appended after the final Stage A regression
run below; hosted CI/Windows execution is NOT_VERIFIED until an authorized push.

### Final local results (Stage A)

All commands below exited 0 on Linux with the existing dependencies:

* `.venv/bin/python -m unittest tests.test_p72c3_core`: **35 tests, OK**.
  The process-crash test includes **six real os._exit subprocess boundaries**:
  PREPARED, SUBMITTED, result-file persisted, CONFIRMED, attempt persisted,
  APPLIED persisted before consumption.
* `.venv/bin/python -m unittest tests.test_p04_calls tests.test_p04_router
  tests.test_p72b2_reliability tests.test_p72b3_budget
  tests.test_p72b3_router_ledger tests.test_p72c_discovery
  tests.test_p72c2_retrieval`: **157 tests, OK** (intermediate focused regression).
* `.venv/bin/python -m unittest tests.test_p7_pipeline_adapters`:
  **128 tests, OK**, including existing D06/D10.
* `.venv/bin/python -m py_compile workflow_context.py calls.py router.py
  engine.py tests/test_p72c3_core.py tests/context_crash_worker.py`: OK.
* `.venv/bin/python tools/check_test_imports.py`: OK (Linux).
* `git diff --check`: clean.
* Final platform-aware broad run:

```sh
.venv/bin/python -c 'import importlib,pathlib,sys,unittest; loader=unittest.TestLoader(); paths=sorted(pathlib.Path("tests").glob("test_*.py")); excluded=[p.name for p in paths if p.name=="test_power_win.py"]; suite=unittest.TestSuite(loader.loadTestsFromModule(importlib.import_module("tests."+p.stem)) for p in paths if p.name not in excluded); result=unittest.TextTestRunner(verbosity=1).run(suite); print("RUN",result.testsRun,"FAIL",len(result.failures),"ERR",len(result.errors),"SKIP",len(result.skipped),"EXCLUDED",excluded); sys.exit(not result.wasSuccessful())'
```

Result: **623 run, 0 failures, 0 errors, 0 skips**. One module intentionally
excluded: `test_power_win.py` (Windows-only ctypes.windll backend, as in Linux
CI). The core crash tests themselves were executed, not merely collected.
Hosted CI/native Windows remain NOT_VERIFIED for these uncommitted changes.

Status: **Stage B integration implemented, uncommitted.**
See the Stage B section of `docs/p7-2c3-design.md`. Enabled ARCHITECTURE/BUILD is
wired for NativeAdapter and FactoryAdapter; disabled execution keeps the legacy
path. `tests/test_p72c3_integration.py` covers prompt injection, parity, consent,
conflict safety, UNKNOWN and service-level resume. This is not a committed
P7.2c.3 release, and hosted CI/native Windows remain unverified.
