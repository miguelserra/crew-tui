# P7.2c repository and roadmap audit

Baseline: `2b10c12`, clean tree. P7.1d is OPEN/COMPLETE, P7.2a and P7.2b are
complete; P7.2c was not started.

## Existing capabilities found

- `adoption.inspect_repository()` already provides bounded inventory, hashes,
  language counts, configuration/test files, Git metadata, symlink/special-file
  records and coverage limits. P7.2c reuses it instead of duplicating inventory.
- `context_reader.read_project_context()` provides bounded, ignore-aware text
  reading for model context; P7.2c reuses the same limits ideas for static
  analysis.
- `architecture.ArchitectureService` proposes a new ArchitectureContract from a
  model. It is not evidence-based analysis of existing code and must not be
  confused with discovery.
- P7.2a ProjectBrief/interview and RequirementContract remain the canonical
  requirements path. Discovery findings must not become confirmed requirements.
- P7.2b stores project origin and engine identity in ProjectState extensions.

## Roadmap conflict check

No existing P7.2c definition or discovery subsystem was found. No conflict with
P7.2a/P7.2b contracts. The smallest extension is a read-only static discovery
service plus a persisted dossier in `ProjectState.extensions`, with CLI/TUI
entry points and freshness checks.

## Architecture decision

- New `discovery.py` performs static, non-destructive analysis.
- The full dossier is written under `.crew/understanding/dossier.json` and
  `.md`; ProjectState stores compact metadata and paths.
- No source files are modified, no builds/tests/scripts run and no packages are
  installed.
- Runtime behaviour claims are marked UNKNOWN unless actually executed.
- Findings are OBSERVED/INFERRED/UNKNOWN/CONFLICTING and reference source files.
- Freshness compares the analysed bounded file hashes with the current state and
  reports changed/new/removed files.
- Existing P7.2a/P7.2b models are unchanged.
