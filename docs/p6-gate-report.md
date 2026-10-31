# P6 gate report — profiles, isolated workspace and transactional publish

Reference:

- P6 code — `04f18f1`
- `p6-gate` — tag on this report commit
- CI — `41152025324`: **success** (Linux 3.10/3.12/3.13, Windows 3.13,
  integration Linux+Windows)

Scope: P6 applies to the unified engine. Legacy Factory order/compatibility is
kept for P7.

## Workflow profiles

`profiles.py` defines `review`, `script`, `library`, `web` and `auto`.

- `auto` inspects requirements/architecture/tasks (UI/browser hints or UI
  extensions → web; library/API/test hints → library; otherwise script) and
  chooses the more comprehensive profile when ambiguous.
- `profile_selection` is persisted with requested/resolved/reason/version in
  `ProjectState.extensions`; `--resume` never recomputes it.
- `profile` participates in the architecture approval hash; changing it
  invalidates the approval.
- `phases_for` skips VISUAL for script/library; web keeps the full pipeline.
- Review reports remain read-only in the unified engine and are never
  published; the Orchestrator review/Q&A path remains compatibility.

## Deterministic scaffold

`scaffold.py` creates directories and empty files from
`ArchitectureContract.modules/interfaces` + task targets:

- only after architecture approval;
- preserves existing content;
- case-insensitive/path/symlink validation;
- idempotent;
- records `ScaffoldManifest` (created/reused/ignored);
- zero LLM calls (verified by a pool that raises if called).

## Workspace isolation

`workspace.py` seeds `workspace_root` from `project_root` (excluding internal
logs, state, VCS, virtualenv, node_modules) and records original hashes.
For unified runs, `FactoryAdapter` runs BUILD/VERIFY/INTEGRATE/VISUAL/SIGN-OFF
entirely in the workspace. The project root is not a build destination.

## Transactional publish

`publish.py` runs only after SIGN-OFF and all gates:

1. plans changes with original/result hashes;
2. detects human edits/deletions since the snapshot and refuses to overwrite;
3. backs up replaced files;
4. writes a journal under `.crew/publish/<id>/`;
5. replaces each file atomically in the destination (per file, not a global
   multi-file transaction);
6. records progress and rolls back on failure;
7. recovers interrupted journals on the next start;
8. `DONE` is set only after a successful publish.

Files whose content Crew did not change are skipped, so unrelated human edits
are preserved. A human change to a file Crew wants to change/deletes blocks the
publish.

## VERIFY checkpoint

The unified `VERIFY` phase now checks task evidence before INTEGRATE: every
build task must have a verification record; FAIL blocks; UNVERIFIED requires
the explicit `ALLOW_UNVERIFIED`/`--allow-unverified` exception.

## Exit gate evidence

| Gate | Evidence |
|---|---|
| Auto profile coherent/persisted | `tests/test_p06_profiles.py`, `test_p06_engine.py` |
| Resume does not recompute | `test_resume_does_not_recompute_resolved_profile` |
| Profile change invalidates approval | `test_profile_change_invalidates_approval` |
| Scaffold idempotent, zero LLM | `tests/test_p06_scaffold.py`, P6 integration |
| Existing content preserved | scaffold + adapter tests |
| Build/Integration/Visual in workspace | isolated adapter + smoke |
| No project writes before publish | adapter integration tests |
| Critic/visual FAIL / UNVERIFIED no publish | engine publish only on SIGNOFF; P5 gate |
| Publish conflict detected | `test_human_change_blocks_publish` |
| Interrupted publish recovery | `test_recovery_reverts_interrupted_journal`, rollback test |
| DONE only after publish | `test_auto_resolves_once_and_skips_visual_for_script` (publish hook) |
| Linux + Windows | CI `41152025324`: success |

## Tests

| Suite | Result |
|---|---|
| P1 core | 80 tests, OK (13 skips) |
| P2 core | 34 tests, OK (2 skips) |
| P3 core | 18 tests, OK (7 skips) |
| P4 core | 23 tests, OK |
| P5 core | 14 tests, OK (4 integration skips) |
| P6 core | 26 tests, OK (4 integration skips) |
| Integration (P1/P2/P3/P5/P6) | 30 tests, OK |
| power + budget | OK |
| `py_compile` / `git diff --check` | OK |

## Smoke (fake OpenAI-compatible server)

Unified `auto` run resolved to `script`, scaffolded deterministically, built and
critic-gated in the isolated workspace, passed the VERIFY checkpoint,
integrated, signed off, and published exactly the final files to the project.
The state recorded `profile_selection`, `workspace_root` and the publish
journal; the project root contained only final files plus `.crew` metadata.

## Deferred to P7

Legacy Factory removal, removal of `FactoryState`/`TaskState` conversions,
full review/Q&A unification, `/factory` aliases and final TUI/CLI cleanup.
