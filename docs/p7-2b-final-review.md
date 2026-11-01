# P7.2b final review

Reviewed baseline: `4bb0533` plus the review fixes documented below.
Evidence labels: **INSPECTED**, **TESTED**, **CI_VERIFIED**, **REPORTED**,
**NOT_VERIFIED**.

## Verdict

P7.2b remains **COMPLETE** after fixing two concrete defects found during this
review. P7.2c has **NOT_STARTED**.

## Engine identity review

- INSPECTED: persisted identity is read from `extensions["engine_identity"]`.
- INSPECTED: new projects default to `native`; legacy `.factory` states infer
  `factory`; unified states with no identity are ambiguous.
- FIXED: explicit resolution of an ambiguous identity previously selected an
  adapter but did not persist the resolution. `resolve_and_persist()` now
  persists the deliberate resolution, so subsequent saves/reloads/resumes are
  stable.
- TESTED: persisted-identity resume, conflict rejection, ambiguity blocking,
  explicit resolution persistence and repeated reload.
- INSPECTED: a persisted identity cannot be overridden by resume; conflicts
  raise an error.
- INSPECTED: FactoryAdapter remains explicitly selectable and is retained.

## External adoption review

- INSPECTED: adoption rejects `.crew/state.json` and `.factory/state.json`.
- INSPECTED: origin is `EXTERNAL_EXISTING_REPOSITORY`, identity is `native`,
  phase is SPEC.
- INSPECTED: adoption does not run builds, tests, package installs or scripts;
  test status is `not_run`.
- INSPECTED: no restructuring, dependency upgrade, scaffold overwrite, silent
  deletion or discard of uncommitted changes.
- TESTED: existing file bytes, configuration and Git HEAD are preserved;
  pre-existing dirty paths are recorded; re-adoption is rejected.

## Baseline integrity findings

- FIXED: the previous baseline could silently treat an unreadable/absent
  post-adoption hash as unchanged. Adoption now distinguishes changed,
  unverified and skipped hashes and never claims full integrity when the
  baseline is incomplete.
- Baseline now reports regular-file inventory total, hashed count, hash
  coverage, hash completeness, skipped hashes, unreadable files, symlinks,
  special files, LFS pointers, submodules and explicit limits.
- Default limits: 2000 files, 64 MiB total hashed bytes, 8 MiB per file.
- Hash verification is bounded to regular files successfully read within those
  limits. It never claims that excluded, unreadable, oversized or over-limit
  files were verified.
- Excluded from hash inventory: `.git`, `.crew`, `.factory`, `node_modules`,
  `__pycache__`, `.venv`, `venv`; the exclusion list is recorded.
- Symlinks are recorded with target and whether the target is inside the root;
  external targets are not followed or hashed.
- LFS pointers are identified and their pointer files are hashed, not the LFS
  object contents. Git submodule paths are recorded from `.gitmodules`.
- TESTED: bounded inventory, coverage ratio, hash-skipped reporting, symlink
  containment, LFS metadata and deterministic repeated inspection.
- NOT_VERIFIED: concurrent mutation during inspection is not locked. Adoption
  re-inspects after writing canonical state and fails on changed hashes, but a
  concurrent writer can still make a snapshot appear inconsistent.

## Filesystem and safety findings

- Root is canonicalised with `resolve()`.
- Directory symlinks are not followed; file symlinks are recorded, not hashed.
- Special files are recorded but not opened.
- Paths are stored project-relatively with POSIX separators for cross-platform
  determinism.
- No repository code is executed.
- Read errors and resource-limit exclusions are explicit, not silently treated
  as verified.
- Git history is not modified.

## Chesterton's Fence

- INSPECTED/TESTED: adoption metadata records no automatic restructure,
  dependency upgrade, scaffold overwrite, silent delete or discard of user
  changes.
- Adoption only registers baseline/state; significant changes remain outside
  adoption and require later rationale/verification.

## CLI, TUI and API consistency

- INSPECTED: CLI, TUI and programmatic paths use `engine_selection`.
- TESTED: `--engine`, `--status`, `--adopt`, and TUI `/engine`/`/adopt` share
  identity and adoption semantics.
- `--status` reports origin, persisted identity, effective adapter, selection
  reason and ambiguity errors.
- TESTED: CLI new-project selection/status and CLI external adoption.

## Regression and scope

- TESTED: P7.1d and P7.2a suites remain green in the local suite.
- INSPECTED: no routing, billing, ledger, replay, verification, signoff or
  publication contracts changed in this review.
- INSPECTED: FactoryAdapter is not removed; no automatic Factory→Native
  migration exists.
- P7.2c remains unstarted.
