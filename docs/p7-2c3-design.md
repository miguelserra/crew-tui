# P7.2c.3-A — critical context and recovery core

Baseline: `e921bc5428dde6348a53b88e3008b8a23c91c0e4`. Stage A implements the
critical protocol only. ARCHITECTURE/BUILD prompt integration is **not enabled**.
There is no new tool loop, model, tokenizer, embedding service or dependency.

## Established architectural decisions

* Explicit activation per run and phase; absent activation means disabled,
  including old runs. Activation is independent of SPEC/architecture approvals,
  `--yes`, unknown-billing approval and PAYG grants.
* All candidates receiving the same enriched prompt must appear by exact
  `identity_key` in the disclosure allowlist. Otherwise snapshot the unchanged
  base messages with `disclosure_denied`, without changing Router ordering.
* This authorizes only additional **structured dossier fields**. Existing
  workspace disclosure remains unchanged. Human activation must also explain
  local storage of the full request for recovery.
* The dossier remains canonical. A snapshot is an immutable execution artefact
  for one logical request, not a competing knowledge store.
* Snapshots contain the **complete ordered text messages**, not merely the
  injected block: existing workspace reads and prompt formatting also affect
  request identity. Reconstruction with a different base prompt is rejected.
* CONFIRMED managed results are consumed only after their application and an
  APPLIED marker have been atomically persisted in canonical ProjectState.
* Non-managed calls retain their existing attempt/consumption behaviour.

## Code evidence and the original recovery window

Native architecture builds messages in `architecture.py:59-108`; BUILD uses
`executor.py:99-135`. Factory uses `factory.py:259-293,328-339,482-513,585-618`.
They converge on `LLMPool.complete` (`main.py:1266-1324`) and RouteChain.

`invocation_fingerprint` (`calls.py`) hashes content and generation parameters,
not message roles. The core therefore checks **both** this unchanged fingerprint
and a stricter hash of ordered roles/text. Router computes the fingerprint before
dispatch, writes intent/SUBMITTED before provider invocation and persists result
before CONFIRMED. `WorkflowEngine.on_attempt` used to consume all confirmed
calls before the calling service committed application. It now delegates to
`_consume_legacy_call`: managed records are deferred, all others are unchanged.

## Public contracts (`workflow_context.py`)

Typed frozen dataclasses (their dict/list members are ordinary JSON containers;
durable immutability is enforced by hash validation and no-overwrite persistence):

* `ContextActivation(run_id, phases, providers, actor, enabled=True,
  policy_version=POLICY, phase_tokens={ARCHITECTURE:4096,BUILD:2048},
  model_tokens={}, max_chars=16000, safety_ratio=0.15,
  data_scope="structured_dossier_only", approved_at=utc_now())`.
  Exact non-wildcard identity keys; explicit actor/approval time. Invalid
  activation cannot authorize dispatch. `to_dict()` normalizes phase/provider
  lists for hashing.
* `ContextRequest(logical_id, run_id, phase, task_id, target_file, source_root,
  workspace_root, contract_sha256, query, sections, policy_version=POLICY)`.
* `ContextSelection(block, items, omissions, dossier_sha256, freshness, budget)`.
* `ContextSnapshot(payload, sha256)`; `to_dict()` returns a defensive copy;
  `render_messages(factory)` calls `factory(role, content)` for each stored
  system/human message. Payload includes request, route contract, candidate
  identity snapshots, consent digest, selection, base/messages hashes, complete
  messages, existing invocation fingerprint and creation timestamp.
* `ContextInjectionRecord(snapshot_sha256, state="PREPARED", call_ids=(),
  applied_at=None)`: canonical execution metadata under
  `ProjectState.extensions["workflow_context"]["records"][logical_id]`.
* `ManagedResult(text, call_id, served, replayed)`: durable text and effective
  producer identity; local recovery does not fabricate usage or spending.

All payloads are JSON. `canonical(value)` is UTF-8 JSON with sorted keys,
compact separators, `ensure_ascii=True`, `allow_nan=False`. `digest(value)` is
the complete SHA-256. Text/codepoints are not normalized. Filesystem root
strings are local identities, not portable repository-relative references.

## Exact caller lifecycle

`WorkflowEngine.context_core() -> WorkflowContext` lazily exposes:

```python
activate(activation: ContextActivation) -> None
contract_digest(phase: str, task_id: str) -> str
prepare(logical_id: str, phase: str, route: RouteResult,
        messages: Sequence[Any], *, target_file: str = "", query: str = "",
        sections: tuple[str, ...] = ()) -> ContextSnapshot | None
invoke(pool: Any, route: RouteResult, logical_id: str,
       messages: Sequence[Any]) -> ManagedResult
recover(logical_id: str) -> ManagedResult | None
commit(logical_id: str,
       apply: Callable[[ProjectState, ManagedResult], None]) -> None
```

1. `engine.apply_pool(pool)` supplies the existing durable CallLedger.
2. A trusted UI/orchestrator explicitly calls `activate`; there is no new CLI
   or automatic consent in this stage. Actor strings are audit metadata, not
   cryptographic proof of human identity.
3. The caller allocates a stable logical ID per semantic generation operation.
   Do not allocate another ID merely because a process restarted.
4. Fresh Router result + existing base messages enter `prepare`. Disabled phase
   returns None with no new run artefacts; use the original path in that case.
   Enabled requests persist even a blank selection, so recovery is uniform.
5. Restore messages from the snapshot; `invoke` validates exact roles/content,
   invocation fingerprint, contract, consent and candidate configuration before
   any new provider submission. Calls still pass through pool.complete/Router.
6. Validate/parse the response in the service. `commit` calls `apply`, persists
   the resulting ProjectState **with APPLIED**, then marks the ledger consumed.
   `apply` must not call models or save state itself.
7. Retrying commit after durable APPLIED only completes ledger consumption.
   It does not reapply the result. A new semantic operation needs a new ID.

## Selection, provenance and disclosure

The core calls the existing lexical `retrieve_context`, using the explicit query
and section filters. There is no semantic inference or automatic query creation
here. The integration stage supplies requirements-oriented ARCHITECTURE queries
and task/path/dependency-oriented BUILD queries.

Only OBSERVED/INFERRED records with complete resolvable evidence IDs and source
paths are eligible. The selected file hashes must match the current workspace,
stay inside its root, be regular bounded files, and pass IgnoreRules. Missing
hashes/references, truncated retrieval entries and divergent files are omitted.
No parse error or stale result is promoted to verified knowledge.

The additional prompt contains an explicit untrusted/historical boundary and
canonical JSON fields only: names, kind, file/path/line, imports/edges, public,
extraction method, target-mapping classification, epistemic labels and evidence
references. Arbitrary README prose, code, environment values and extra fields
are not copied. Identifiers and filenames remain untrusted; serialization and
an instruction boundary reduce injection exposure, not prove model immunity.

STALE/UNKNOWN/missing dossier, root mismatch, no evidence, disclosure denial or
zero capacity yield a blank block and recorded omission. Full final messages
remain identical to the original base in that case.

After PREPARED, dossier changes never cause reselection. The block is explicitly
historical/last-recorded; restoring an immutable request is not a claim that
the workspace is still unchanged. Reconstructing changed base messages is an
error. Current-file conflict detection at result installation remains the
service caller's responsibility (see handoff).

## Budgeting

`budget_for(activation, phase, route, messages)` takes the minimum of phase cap,
per-identity overrides and capacity of **every candidate**, less full base
message estimate, output reserve (`max_tokens`) and a 15% default window margin.
Unknown window or no recipients means zero additional context.

The estimate uses canonical-message UTF-8 byte length plus message overhead —
deliberately restrictive and **not exact tokenizer accounting**. The selected
block must fit max_chars and the incremental canonical-message byte estimate;
this includes delimiters, escaping, references and freshness metadata. It does
not truncate the existing prompt or promise that legacy base prompts fit a
provider. Phase/model limits are persisted in consent; changes invalidate new
submissions for a prepared request.

## Persistence, locking and recovery

Snapshots live at `.crew/runs/<run_id>/context_snapshots/<logical_id>.json`.
ProjectLock is reused for managed operations; a separate per-snapshot lock makes
publication no-overwrite across callers. `atomic_write_json` writes/fsyncs,
validates JSON and replaces atomically. Files and identifiers are bounded;
links/corrupt/unknown-version snapshots fail closed. Snapshots are not encrypted
and contain full prompts; `.crew` is excluded from workspace/publication. Keep
them until result application and recovery are no longer required; no automatic
garbage collector or claim of global retention limits is introduced.

The run lock covers provider invocation and application. Ledger is reloaded
under the operation lock; a stale canonical-state revision forces reload.
Engine-owned saves acknowledge the new revision. An established project_root
is required: a staging execution never defaults implicitly to the current
working directory. This protects managed
operations; it is not a new general multi-process budget/state service.

| Crash/window | Durable evidence | Allowed recovery |
|---|---|---|
| Before/during selection | No promoted snapshot, no call | Recompute; provider has not been invoked |
| Snapshot before state record | Valid orphan snapshot, no dispatch | Adopt only same base/route/contract/consent; do not overwrite |
| PREPARED before submission | Snapshot + canonical PREPARED | Restore exact messages; one submission if no call exists |
| PENDING/SUBMITTED/UNKNOWN | Snapshot-linked CallRecord | Raise CallOutcomeUnknown; never silently resubmit |
| Result file before CONFIRMED save | Result artefact + submitted record | Existing ledger reconciliation promotes; recover locally |
| CONFIRMED before attempt sink | Unconsumed result | Local recovery restores producer attempt through engine dedup |
| CONFIRMED after attempt sink, before apply | Unconsumed result + attempt | Local recovery; no provider; apply once durably committed |
| Application callback before state save | Unconsumed result | Reload; idempotent filesystem application is mandatory |
| APPLIED persisted before consume | Canonical result + APPLIED, unconsumed result | Commit completes consumption; callback must not run |
| Consumption/save error | APPLIED or PREPARED durable boundary | Poison in-memory core; reload, never trust mutated cache |
| Dossier changed | Immutable snapshot | Restore original data, no reselection |
| Base prompt/workspace reconstruction changed | Hash mismatch | Reject new dispatch; local recover remains available |
| Contract/generation params changed | Digest/fingerprint mismatch | Reject invocation/application for the old semantic request |
| Registry/consent changed before submission | Candidate/consent mismatch | Reject new submission, no hidden fallback or new ID |
| Registry/consent changed after CONFIRMED | Local durable result | Local recovery can proceed without provider; generation and contract still checked |
| Missing/corrupt snapshot | Record or ledger evidence survives | Block; no reconstruction/resubmission |

## Router/ledger delta and compatibility

Additive `context_snapshot_id` in RouteRequest and CallRecord; legacy default is
empty. Existing fingerprint formula is unchanged. Managed dispatch requires an
active core guard (even direct calls to RouteChain cannot bypass it). For managed
requests all providers, including local/subscription, write durable intent/result
records; ambiguous managed outcomes stop. This is execution recovery, not a new
financial charge; grants/charge/uncertainty accounting remain unchanged.

Legacy replay excludes managed records and vice versa. Failed-reference/fallback
policy is unchanged. Managed replay is locally recovered by core before provider
dispatch; structured/tool-call results remain non-replayable and block safely.

No service prompts, approvals, adapter identity or phase order changed. Factory
compatibility persistence uses the existing engine.save path. Native has no
Factory dependency. Enabled full-pipeline parity is a next-stage gate, not
claimed by Stage A. Existing no-context D06/D10/differential tests must remain green.

## Explicit limits of the guarantee

No exactly-once external execution, power-loss guarantee beyond existing atomic
storage, cross-file transaction for arbitrary filesystem callbacks, or durable
financial budget reconstruction is claimed. `apply` must install/check files
idempotently and reject conflicting human edits. Core guarantees durable result
availability, no silent managed resubmission, and state-before-consumption order.
Hosted Windows CI has not run for this uncommitted stage.

---

# P7.2c.3-B — Stage B workflow integration

Stage B connects the Stage A core to generation **without changing any core
contract**. The feature remains disabled unless explicit activation is persisted.

## Integration points

* `WorkflowEngine.apply_pool()` calls `adapter.attach_context(self)`:
  - `NativeAdapter.attach_context` gives the engine to `ArchitectureService` and
    `ExecutorService` only.
  - `FactoryAdapter.attach_context` gives the engine to `Factory` and installs
    the adapter-mediated apply callbacks below.
* Native ARCHITECTURE: `ArchitectureService.design()` uses the core around its
  existing prompt. If no core is attached (standalone service/tests), the legacy
  `_ask()` path is retained.
* Native BUILD: `ExecutorService._build_task()` prepares/invokes the managed
  request, then verifies, then installs through `core.commit()`.
* Factory ARCHITECTURE/BUILD: `Factory.step_architect()` and `_build_one()` use
  `_generate()`. Without a core they delegate to the existing `_ask()` hook.
* Stable logical IDs use only `[A-Za-z0-9_.-]`:
  `architecture-<run>` and `build-<run>-<task>`. They are allocated per semantic
  operation, not per resume. A new operation needs a new ID.
* Factory apply is adapter-mediated because `core.commit()` persists the
  canonical `ProjectState`, while the Factory also owns a `FactoryState`
  compatibility view. `FactoryAdapter._managed_apply_arch/_build` update the
  Factory view and call the existing `sync_to_project()` before the core saves
  `APPLIED`; Native applies directly to `ProjectState`.
* Native and Factory base prompts differ by design. Enabled parity is verified
  by comparing the selected dossier items/block from snapshots, not by forcing
  identical legacy prompts.

## BUILD application, conflict safety and failure policy

`workspace.apply_generated_text()` installs generated text with:
current bytes == generated bytes → idempotent no-op; current bytes == workspace
origin hash (or an empty scaffold placeholder with no origin) → replace; any
other content → explicit conflict error. Installation uses temp-file, fsync and
atomic replace. The `apply` callback also records verification/status/producer;
the core persists this with `APPLIED` before consuming the ledger.

When context is enabled and a managed BUILD generation is rejected by the
existing oracle/critic, Stage B runs the **existing automatic FIX + verification**
(NEW-01 correction). The confirmed BUILD result is recovered locally (never
re-submitted to a provider) and only the finally accepted artifact is applied
and consumed. FIX uses the existing service path and retry limits; it preserves
producer identity and cross-family verification. If FIX is exhausted, the
provider outcome is ambiguous (e.g. PAYG UNKNOWN), a filesystem conflict occurs,
or snapshot/application state is uncertain, the result remains fail-closed and
recoverable rather than being consumed or bypassed. A new paid request is never
allocated to bypass these boundaries.

The FIX request remains on the existing legacy service path rather than receiving
its own managed context snapshot. A process crash after a FIX response but before
final application can therefore repeat the FIX request on resume; the already
CONFIRMED BUILD request is still recovered locally and is not re-submitted.
Ambiguous PAYG FIX outcomes remain fail-closed.

Only `CallOutcomeUnknown` originating from a managed dispatch is propagated;
legacy ambiguity/fallback behaviour is unchanged.

## Consent and CLI

`--context-consent` accepts a model id only when it resolves to one registry
identity, otherwise a full `provider|endpoint|credential_profile|model_id` key.
`--context-phases` defaults to `ARCHITECTURE,BUILD`. Activation is persisted in
`ProjectState.extensions["workflow_context"]["activation"]`; it is not implied
by `--yes`, `--allow-paid`, SPEC or architecture approvals. The CLI prints the
authorised phases/provider count and the local snapshot location, never content.

## Verified integration behaviour

`tests/test_p72c3_integration.py` covers Native/Factory enabled ARCHITECTURE and
BUILD prompt capture, selected-item parity, disabled prompt compatibility,
disclosure denial, installation, human-edit conflict detection, managed
verification failure retention, PAYG UNKNOWN non-resubmission, CLI consent, and
service-level resume after CONFIRMED before application (no second provider
call). Core subprocess crash boundaries remain in `tests/test_p72c3_core.py`.

## Remaining limitations

Advisory run locking plus stale state/ledger rejection coordinates managed
operations, but arbitrary independent legacy writers are not a distributed
system. There is no generic transaction spanning multiple files and
ProjectState. Snapshots store full prompts locally without encryption or a
global retention cap. Freshness is last-recorded; retrieval is lexical; the
context budget is a conservative byte heuristic, not exact tokenization. No
dynamic tool calling is added. Hosted CI/native Windows is unverified until an
authorised push.
