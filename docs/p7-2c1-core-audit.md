# P7.2c.1 — Project Discovery Core audit and completion

Baseline: `2829995` (main), clean tree. This milestone is the static Discovery
Core; it deliberately does not add runtime analysis, REA or cloud agents.

## Repository baseline

The repository already contained a committed P7.2c implementation (`discovery.py`,
`tests/test_p72c_discovery.py`, `docs/p7-2c-report.md`, commit `9b05b37`) that
goes **beyond** P7.2c.1 (dossier persistence, ProjectState extension, CLI/TUI and
WorkflowEngine wiring). Per Chesterton's Fence those capabilities were preserved,
not removed or redesigned. This task audited the core against the P7.2c.1 model
and filled only genuine gaps.

## Existing implementation (reused, unchanged)

- `discovery.analyze()` — deterministic static analysis over
  `adoption.inspect_repository()` (the single bounded inventory; no duplicate
  traversal).
- Python AST imports/symbols/`__main__` entry points; JS/TS regex imports.
- Language counts, package managers, build systems, CI, lint, docs.
- Evidence-backed findings, OBSERVED/INFERRED/UNKNOWN/CONFLICTING counts,
  Mermaid diagrams, Chesterton's Fence metadata.
- `discover_project()`, `freshness()`, `section()`, `summary_for_agent()`, CLI,
  TUI and engine wiring (preserved; outside P7.2c.1's minimal scope).

## Design-to-implementation gap matrix (P7.2c.1)

| Requirement | Before | After |
|---|---|---|
| Project root/name with provenance | PARTIAL (name = dir only) | IMPLEMENTED (`project.name.value/source/status`) |
| Project description (OBSERVED or UNKNOWN) | MISSING | IMPLEMENTED (README paragraph / package.json; else UNKNOWN) |
| Git available / HEAD / branch / dirty / dirty_paths | PARTIAL (dirty was raw lines) | IMPLEMENTED (`git_available`, boolean `dirty`, `dirty_paths`) |
| File inventory / directories / languages / configs | IMPLEMENTED | unchanged (reuses adoption) |
| Framework detection from manifest/import evidence | PARTIAL (empty evidence, substring risk) | IMPLEMENTED (exact top-level import or declared dependency, always evidence) |
| Package managers / build tools / CI / test frameworks | PARTIAL | IMPLEMENTED (adds `technology_stack.test_frameworks`) |
| Dependency manifests / declared deps / unresolved formats | PARTIAL (no unresolved; pyproject mis-parsed) | IMPLEMENTED (`dependencies.manifests/declared_dependencies/unresolved_formats`; TOML/JSON/requirements/go.mod; honest unresolved) |
| Modules / classes / functions / interfaces | PARTIAL (symbols only in `preserve`) | IMPLEMENTED (`structure.*`, Python AST, extraction method recorded) |
| Unsupported source languages | PARTIAL (inventory source suffixes omitted) | IMPLEMENTED (`structure.unsupported_source_suffixes` derived from `adoption.LANG_BY_SUFFIX` minus markup/style; F-1) |
| Test files / directories / configuration / execution status | PARTIAL | IMPLEMENTED (`tests.*`, `execution_status: NOT_RUN`) |
| Evidence references + confidence classification | PARTIAL (inline only) | IMPLEMENTED (`evidence.items` with id/classification/subject/claim/source_path/line_start/line_end/extraction_method/confidence/limitations) |
| Coverage and limitations | PARTIAL | IMPLEMENTED (`evidence.coverage` with explicit denominator) |
| Determinism | not asserted | TESTED (reproducible modulo `generated_at`) |

## Implementation delta

- Added `project`, enriched `repository`, added `structure`, `tests`,
  `dependencies.manifests|declared_dependencies|unresolved_formats`, and
  `evidence.coverage|items`.
- `_parse_manifest` interprets package.json, requirements.txt,
  pyproject.toml/Cargo.toml/Pipfile (via stdlib `tomllib` when present;
  otherwise reported unresolved), and go.mod; recognised-but-uninterpreted
  formats and truncated manifests are listed under `unresolved_formats` (never
  guessed).
- Fixed framework detection: no empty evidence and no substring matching over
  arbitrary text; only declared dependencies or exact top-level imports.
- F-1 (P7.2c.1.1): `structure.unsupported_source_suffixes` now derives from the
  inventory language map (`adoption.LANG_BY_SUFFIX`) plus the explicit
  source-like set, minus markup/style (`.html`, `.css`) and minus
  `STRUCTURE_METHODS`. Source languages recognised by the inventory but lacking
  a structural extractor (`.go`, `.rs`, `.java`, `.rb`, `.php`, `.cs`, `.c`,
  `.h`, `.cpp`, `.sh`, ...) are therefore reported; documentation, data,
  configuration and markup suffixes are not. Only suffixes actually present are
  listed, normalised to lowercase, unique and sorted.
- `DISCOVERY_VERSION` bumped `p7-2c/1` → `p7-2c/2` (additive model). Existing
  dossier JSON remains loadable; `summary_for_agent`/`freshness` keys unchanged.

## Safety and bounds

Static only: no execution of repository code/tests/hooks/build files, no package
installation, no network, no cloud models. Reuses adoption's limits
(`max_files=2000`, `max_total_bytes=64 MiB`, `max_file_bytes=8 MiB`), exclusions
(`.git`, `.crew`, `.factory`, `node_modules`, `__pycache__`, `.venv`, `venv`),
and symlink/special-file recording without following external targets.
`_read_text` rejects outside-root, symlink, binary and non-UTF-8 content; one bad
file is recorded locally and does not abort the run.

## Tests

`tests/test_p72c_discovery.py`: 7 pre-existing + 13 P7.2c.1 core + 7 F-1
classification = 27 tests covering
project provenance/description, structure, tests section, dependency manifests
and unresolved formats, TOML parsing (version-aware), framework evidence,
evidence coverage/items, determinism, parse-error isolation, porcelain dirty
parsing, non-git repositories and unsupported source-language classification.

## Deferred (not part of P7.2c.1, not implemented here)

Runtime behaviour analysis, execution, incremental/stale-dossier redesign,
requirement/architecture auto-generation, agent prompt injection, REA, cloud
analysis. The pre-existing dossier/freshness/CLI/TUI/engine integration is
retained as already-implemented behaviour.
