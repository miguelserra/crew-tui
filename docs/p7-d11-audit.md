# D11 publication differential audit

Audited at `f428f26` before D11 tests. D11 scope is adapter-level publication
behaviour: conflicts, explicit deletions, orphan handling, approval gates and
recovery decisions.

## FactoryAdapter publication path

`FactoryAdapter.publish()` uses the shared `PublishManager` with:

- `project_root` and isolated `workspace_root`;
- origin hashes from `project.extensions["workspace_origin"]`;
- project ignore rules;
- `project.architecture.deletions`.

It calls `recover()`, rejects `RECOVERY_REQUIRED`/`REVALIDATION_REQUIRED`, validates
the workspace manifest, rejects incomplete workspaces, and calls `publish()`.
Recovered `COMMITTED` journals are returned without republishing (D10 fix).

## NativeAdapter publication path

`NativeAdapter.publish()` uses the same shared manager, roots, origin, rules and
deletions, with the same recovery and manifest checks. The two adapter publish
paths are therefore intentionally identical and do not own separate publisher
logic.

## Shared and adapter-specific services

Shared: `PublishManager`, `workspace.plan_publish`,
`validate_workspace_integrity`, journaling, backup/rollback, conflict resolution,
orphan inspection/cleanup and durability reporting.

Adapter-specific: root selection, origin/rules/deletions injection and the
WorkflowEngine sign-off/approval gate before publication.

## Conflict handling contract

- Human/external modifications are detected by comparing current project hashes
  to origin hashes and planned `old_sha`.
- Conflicts raise `PublishConflict`; no silent overwrite occurs.
- The workflow records `publish_conflict` and does not advance to DONE.
- A failed apply rolls back already-applied entries; a rollback failure becomes
  `RECOVERY_REQUIRED`.

## Deletion authorization contract

- A deletion must come from an explicit `DeletionOperation` in the architecture.
- `target_file`, `expected_sha256` and an existing requirement id are validated.
- The workspace may omit the file only when an approved deletion covers it.
- A changed project file or an excluded/protected target blocks deletion.

## Orphan handling contract

- An orphan is a publication run directory without `journal.json`.
- `orphans()` is read-only.
- `clean_orphans()` is dry-run by default; without explicit ids and with
  `dry_run=False` it raises.
- Cleanup only removes explicitly selected ids and never valid journal dirs or
  backups.

## Rollback and journal contract

States: `PREPARED`, `PUBLISHING`, `ROLLING_BACK`, `RECOVERY_REQUIRED`,
`REVALIDATION_REQUIRED`, `ROLLED_BACK`, `COMMITTED`. A failure before commit
rolls back from backups; a rollback failure persists `RECOVERY_REQUIRED` and
blocks DONE.

## Existing coverage

Shared PublishManager coverage is strong in `test_p061_publish.py` and
`test_p061_recovery.py`. D10 covers normal publication, recovered COMMITTED,
ROLLED_BACK and RECOVERY_REQUIRED through the workflow. Adapter-level D11
coverage for conflict injection, authorized/unauthorized deletions, deletion
rollback, orphan discovery/cleanup and explicit resolution was missing.

## Adapter-level test seams

The adapters now expose their real publication manager for the existing
publication interfaces:

- `publication_orphans()`
- `clean_publication_orphans(ids=None, dry_run=True)`
- `publication_issues()`
- `resolve_publication(journal_id, rel, choice, manual_file=None)`

These delegate to the same `PublishManager` used by `publish()` and do not
create a second publisher implementation.

## Exact missing comparisons and resolution status

| Gap | Status |
|---|---|
| Normal manifest/hash/filesystem publication through Engine | Resolved by `test_d11_01` |
| External change after planning | Resolved by `test_d11_02` with real manager conflict |
| Explicit conflict resolution | Resolved by `test_d11_04` via adapter interface |
| Rollback after conflict | Resolved by `test_d11_03` and deletion rollback `test_d11_08` |
| Authorized deletion | Resolved by `test_d11_05` |
| Unauthorized deletion | Resolved by `test_d11_06` |
| Deletion target externally modified | Resolved by `test_d11_07` |
| Deletion rollback | Resolved by `test_d11_08` |
| Orphan discovery/default inspection/explicit cleanup/invalid id | Resolved by `test_d11_09..12`; invalid-id cleanup was a reproducible shared-publisher defect and is fixed |
| Publication without approval / stale approval / pending recovery | Resolved by `test_d11_13..15` |


## Final D11 evidence

- 15 adapter-level D11 tests; both adapters executed independently.
- Normal publication compared manifests, entries, hashes, durability and
  project filesystem.
- External modification after planning, hash/apply failure and deletion
  rollback used the real `PublishManager` and its rollback path.
- Authorized and unauthorized deletion, external deletion target change and
  deletion rollback were compared through both adapters.
- Orphan discovery, default dry-run, explicit-ID cleanup and invalid-ID safety
  were compared through both adapters.
- Publication without approval, stale approval and existing recovery were
  blocked.
- A real shared-publisher defect was reproduced: `clean_orphans(["missing"],
  dry_run=False)` attempted `rmtree` on a nonexistent path. The loop now skips
  ids not present in the orphan set; regression `test_d11_12`.
