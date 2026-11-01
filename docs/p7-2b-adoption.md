# P7.2b external repository adoption

`adoption.adopt_repository()` registers an existing external repository without
restructuring it or changing its files/git history.

Baseline recorded before changes:

- file list and bounded SHA-256 map;
- language counts, configuration files and test files;
- git HEAD, branch and dirty paths;
- pre-existing uncommitted changes;
- test execution status (`not_run` unless explicitly checked later).

Adoption creates `.crew/state.json` with origin
`EXTERNAL_EXISTING_REPOSITORY`, engine identity `native`, phase SPEC and Chesterton
Fence policy flags: no automatic restructure, dependency upgrade, scaffold
overwrite, silent delete or discard of uncommitted changes. Re-adoption of
managed `.crew`/`.factory` state is rejected.

CLI: `--adopt DIR`, then `--status --project DIR` or `--resume`.
TUI: `/adopt DIR`; `/engine native|factory` sets explicit selection for a new
project/resume.


## Baseline scope and limits

The hash baseline is bounded (default 2000 files, 64 MiB total, 8 MiB per file).
Reports distinguish inventory totals, hashed count, coverage, skipped hashes,
unreadable files, symlinks, special files and excluded roots. Incomplete
coverage is never reported as full integrity. Symlinks are recorded without
following external targets; LFS pointer files are identified; submodule paths
are recorded from `.gitmodules`. Adoption re-inspects after state creation and
fails on changed hashes, though it does not lock against concurrent writers.
