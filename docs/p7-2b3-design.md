# P7.2b.3 — Budget Integrity, Windows Verification & Visual Hash Migration

Design and implementation note for the three findings raised by the independent
Gemini Argon audit of P7.2b.2 (`audit/p7-2b2-argon-audit.md`, decision
CONDITIONAL_PASS): V01 (PARTIALLY_RESOLVED), V05 (NOT_VERIFIED), V06
(REGRESSION_RISK).

Baseline: `9b05b37` + the P7.2b.2 working-tree changes the audit reviewed
(committed as `8826869` with only the audit artefacts). No P7.2c code was
modified and no P7.2c work was started.

---

## V01 — PAYG budget accounting model

### Semantics (precisely defined)

| Quantity            | Meaning                                                        | Where |
|---------------------|----------------------------------------------------------------|-------|
| `estimated_cost`    | Predicted cost before execution (`_reservation_amount`)         | router.py |
| `reserved`          | Reserved exposure: budget withheld for an ACTIVE reservation     | `PaidGrant.reserved` |
| `uncertain`         | Uncertain exposure: budget held after an ambiguous submission    | `PaidGrant.uncertain` |
| `spent`             | Confirmed spend: actual cost recorded on commit                  | `PaidGrant.spent` |
| `overrun`           | Cumulative confirmed spend beyond the reserved estimate          | `PaidGrant.overrun` |

`max_cost` is the ceiling on **confirmed spend + reserved exposure + uncertain
exposure**. Authorization is blocked when
`spent + reserved + uncertain >= max_cost`.

### Reservation lifecycle

`ACTIVE → HELD → COMMITTED` (ambiguous → confirmed) or
`ACTIVE → HELD → RELEASED` (ambiguous → manual reconciliation) or
`ACTIVE → COMMITTED` / `ACTIVE → RELEASED`.

* `reserve()` withholds budget (adds to `reserved`).
* `hold()` (called on an ambiguous submitted outcome) moves the amount from
  `reserved` to `uncertain`. It is **never** released silently.
* `commit(cost)` records the **actual** cost in `spent` (never truncated), and
  accumulates `overrun` when `actual > reserved amount`.
* `release()` returns the amount (from `reserved` or `uncertain`).
* `commit`/`release` are idempotent; `commit` after `release` raises
  `GrantAccountingError`.

### Compatibility

`Router.authorize()` is idempotent per candidate and attaches the reservation to
the candidate. `Router.charge()` consumes that attached reservation exactly once
(no double-spend). `RouteChain.invoke()` reserves, and on ambiguity calls
`hold()`.

### Supported execution boundary and crash window (explicit)

GrantStore is **process-local and in-memory**. It is not a multi-process budget
authority. Reservations are **not persisted**; a crash between `reserve` and
`commit`/`release` loses the in-flight reservation. The `CallLedger`
SUBMITTED/UNKNOWN records remain the durable evidence of a submitted request;
the in-memory reservation state is **not** reconstructed from the ledger on
resume. This crash window is a documented limitation, not a claimed guarantee.

## V05 — Windows native verification

The existing workflow already has `windows-latest` runners (the `integration`
matrix and a dedicated `windows` job) and runs `test_p72b2_*.py` there. P7.2b.3
adds native (non-simulated) filesystem tests in `test_p72b3_windows.py`
(successful atomic write, temp cleanup, real-platform `_replace_with_retry`) and
adds `test_p72b3_*.py` to every CI job.

**Status: NOT_VERIFIED.** No commit/push was authorized, so no native Windows CI
run exists. Simulated `platform="nt"` tests (in `test_p72b2_reliability.py`) are
explicitly **not** presented as native evidence.

## V06 — Visual artifact hash versioning and migration

### Problem

P7.2b.2 changed visual artifact hashes from normalized-text `artifact_hash()` to
raw-byte SHA-256 without a version marker, silently breaking sign-off freshness
for persisted legacy hashes.

### Design

Per-record version marker (no global schema bump):

* New records: `artifact_hashes[rel] = {"v": 2, "sha": <raw-byte sha256>}`.
* Legacy records: `artifact_hashes[rel] = "<hex>"` (plain string) remain and are
  interpreted with the old normalized-text `artifact_hash` semantics.

Shared helpers in `state.py`: `visual_hash_bytes`, `visual_artifact_hash`,
`visual_artifact_matches`. Both `visual.VisualService._artifact_hashes` and
`factory.Factory.step_visual` emit the versioned format; `signoff_blockers`
interprets by version. A legacy record whose artifact still reproduces the
legacy hash is valid; a changed, missing or unreadable artifact is fail-closed
(`visual desatualizado` blocker → requires re-audit). Unknown versions fail
closed.

### Migration

* **Read-time interpretation** (`signoff_blockers`): additive, read-only,
  idempotent, crash-safe by construction.
* **Re-audit migration** (`step_visual`): the next visual step rewrites the
  hashes as versioned records; atomic write; idempotent; interrupted runs leave
  the previous state intact.

No legacy hash is silently deleted or reinterpreted as raw bytes. No silent
approval; sign-off remains fail-closed.

## Tests

* `tests/test_p72b3_budget.py` — 10 tests (V01 accounting).
* `tests/test_p72b3_visual.py` — 15 tests (V06 versioning/migration/signoff).
* `tests/test_p72b3_windows.py` — 3 native filesystem tests (V05).
* Existing `test_p72b2_reliability.py` and `test_p7_pipeline_adapters.py`
  updated for the versioned visual-hash format.

Full local Linux suite: **498 tests, 0 failures**.

---

## P7.2b.3.1 — Big Pickle audit remediation

Narrow corrections to the findings raised by the independent Big Pickle audit.

### F1 — HELD exposure released by a later definitive failure

`RouteChain.invoke()` now releases a reservation on a definitive failure **only
when it is still `ACTIVE`**. A reservation that was moved to `HELD` by an earlier
ambiguous submission stays as `uncertain` exposure; a later failure of a retry
does not prove the earlier submission was free. `GrantStore.release()` still
releases `HELD` when called explicitly (manual reconciliation).

### F2 — repeated compatibility `charge()` double-count

`Router.charge()` no longer clears `candidate._reservation`. A repeated
`charge()` therefore re-enters `GrantStore.commit()`, which is idempotent
(`COMMITTED` → no-op), instead of falling through to the unreserved
`GrantStore.charge()` path. A legitimate standalone `charge()` (no attached
authorization) still uses `GrantStore.charge()`.

### F4 — malformed visual hash version

`state._visual_hash_version()` parses the version fail-closed: missing defaults
to legacy, booleans/non-numeric strings/floats/other types are rejected, and
`visual_artifact_matches()` returns `False` instead of leaking
`ValueError`/`TypeError`.

### F7 — present-but-empty visual evidence

`signoff_blockers()` now distinguishes an **absent** entry (`target_file not in
visual_hashes` → skip, previous behaviour) from a **present** entry whose value
is empty, null, malformed or invalid (→ `visual desatualizado` blocker). Valid
legacy and v2 records are unchanged.

### F8 — Router + CallLedger coverage

`tests/test_p72b3_router_ledger.py` wires the real `Router` and real
`CallLedger`: ambiguous timeout → exactly one submission, `UNKNOWN` ledger
record, reservation `ACTIVE → HELD`, `CallOutcomeUnknown`, no fabricated charge,
held exposure blocks re-authorization; plus confirmed-replay-no-double-charge.

### Budget lifetime across resume — classification: PROCESS_SCOPED

Evidence:

* `GrantStore` is in-memory, created in `LLMPool.__init__` (main.py:743); no
  serialization into `ProjectState` (`state.py`/`store.py` contain no grant
  state).
* `engine.apply_pool()` rebuilds a durable `CallLedger` from
  `.crew/calls.json` (engine.py:143-155) but nothing reconstructs
  `GrantStore.spent`/`uncertain` from it.
* `max_cost` is never set by production code: `GrantStore(allow_paid=...)` adds
  a run grant with `max_cost=None`; `approve_task(...)` is called without a
  budget (main.py:1340). The `max_cost` ceiling is therefore a library-only
  capability, not a configured production budget.
* README (lines 206-209) documents *scoped grants* (task/run, no persistent
  global switch), not a workflow-lifetime monetary ceiling.

Risk: a restart/resume gets a fresh `GrantStore` (spent=0, uncertain=0). If a
caller configured `max_cost`, cumulative confirmed PAYG across resumes could
exceed it. Because production passes `max_cost=None`, no monetary ceiling is
claimed or enforced today.

Recommendation: no code change in this task. If a workflow-lifetime ceiling is
wanted, it requires durable grant accounting reconstructed from `CallLedger`
(a new feature), to be explicitly authorized. Until then `max_cost` remains
process-scoped and is not reconstructed after restart.

### Tests

`test_p72b3_budget.py` (+6 F1/F2), `test_p72b3_visual.py` (+10 F4/F7),
`test_p72b3_router_ledger.py` (3 F8). Full local Linux suite: **517 tests, 0
failures**. Native Windows CI still NOT_VERIFIED (no commit/push).
