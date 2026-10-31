# D12 publisher crash-recovery audit

Audited at `cdba2b7` before D12 adapter-level crash tests.

## FactoryAdapter publication path

`FactoryAdapter.publish()` uses the shared `PublishManager` built from the
adapter's project root, isolated workspace, origin hashes, ignore rules,
approved deletions and optional publication fault seam. It rejects pending
recovery, validates the workspace manifest, calls `publish()`, and returns
recovered `COMMITTED` journals without republishing.

## NativeAdapter publication path

`NativeAdapter.publish()` uses the same shared manager and contract. Publication
state, journals and recovery are not adapter-specific; only the roots and the
WorkflowEngine sign-off gate differ.

## Shared PublishManager contract

Journal states: `PREPARED`, `PUBLISHING`, `ROLLING_BACK`, `RECOVERY_REQUIRED`,
`REVALIDATION_REQUIRED`, `ROLLED_BACK`, `COMMITTED`. Before commit, any failure
rolls back from backups; rollback failure becomes `RECOVERY_REQUIRED`. A
`COMMITTED` journal is terminal and must not be republished.

## Critical persistence and replacement boundaries

`PublishManager._checkpoint()` exposes: `after_run_dir`, `before_backup`,
`after_backup`, `before_generated`, `after_generated`, `before_journal`,
`after_journal`, `after_publishing`, `before_apply`, `after_apply`,
`before_commit`, `after_commit`, `before_rollback`, `after_rollback`,
`during_rollback`, `before_recovery_rollback` and `after_recovery_rollback`.

## Existing fault injection and real-process coverage

- `test_p061_recovery.py` covers publisher-level PostBackup/Prepared/Publishing/
  partial apply/all applied/commit/rollback/corrupt backup and one real
  `os._exit` after first apply.
- D10 covers workflow-level publication with RECOVERY_REQUIRED and COMMITTED.
- D11 covers adapter-level normal publication, conflicts, deletion,
  rollback, orphan and approval gates.
- No adapter-level real-process crash matrix existed before D12.

## Current recovery behaviour

A fresh `PublishManager.recover()` rolls back `PREPARED`, `PUBLISHING` and
`ROLLING_BACK` journals. It returns `COMMITTED` without rollback/republication.
Invalid journals and missing/corrupt backups raise `RecoveryRequired` and leave
a visible recovery marker. The adapter constructor performs this recovery.

## Missing adapter-level scenarios

- Before journal creation.
- After PREPARED.
- After PUBLISHING.
- During backup preparation.
- Partial replacement.
- All replacements before COMMITTED.
- Interrupted rollback.
- After COMMITTED.
- Rollback impossible.
- Normal publication comparison.

## Potential adapter differences

Publication logic is shared and identical by construction; the remaining
adapter-level risk is the WorkflowEngine sign-off gate, state persistence around
publication, and whether a fresh adapter auto-recovers before the engine can
observe the journal. D12 tests must inspect the journal before recovery and
compare post-recovery files, journal state, backup integrity, durability and
terminal workflow phase across both adapters.

## Resolution status

All D12 scenarios are implemented through a real subprocess worker
(`tests/d12_crash_worker.py`) using `os._exit`, then inspected and recovered with
fresh engine/adapter/manager objects. See the differential report for executed
evidence.


## Final D12 evidence

- Ten real subprocess crash boundaries per adapter, each restarted with fresh
  ProjectState, WorkflowEngine, adapter and PublishManager objects.
- Before journal / during backup: no journal, project unchanged.
- PREPARED / PUBLISHING / partial replacement / all replaced before commit:
  recovered to ROLLED_BACK with original hashes.
- Interrupted rollback resumed to ROLLED_BACK.
- COMMITTED recovered without a second publication; id and durability preserved.
- Missing backup produced RECOVERY_REQUIRED and blocked DONE.
- Normal publication reached COMMITTED with equal manifests, hashes and
  durability across adapters.
