# P7.2c implementation report

## Implemented

- `discovery.py`: bounded static analysis over the existing adoption inventory.
- Python AST imports/symbols/entrypoints; JS/TS import extraction.
- Language, framework, package-manager, build-system, entrypoint, component,
  dependency, persistence, external-interface and environment-variable signals.
- Tests/CI/lint/docs/TODO/FIXME quality signals.
- Evidence-backed component/dependency Mermaid diagrams.
- OBSERVED / INFERRED / UNKNOWN / CONFLICTING classification and source
  references.
- Dossier JSON + Markdown under `.crew/understanding/`.
- ProjectState compact summary/path/HEAD/coverage/tool metadata.
- Freshness checks with changed/new/removed files and HEAD movement.
- Agent summary and on-demand dossier sections.
- CLI `--discover`, `--understanding`, `--understanding-section`.
- TUI `/discover`, `/understanding [section]`.
- WorkflowEngine discover/summary/freshness/section methods.
- P7.2a requested functionality and blockers are referenced without creating
  confirmed requirements.

## Safety

Static analysis only: no builds, tests, installs, network or repository scripts.
Runtime execution and dependency installation are recorded as not performed.
The adoption inventory excludes `.git`, `.crew`, `.factory`, dependencies,
caches and virtualenvs; symlinks are recorded without following external
targets; limits and incomplete coverage are explicit.

## Limitations

- Runtime behaviour is not observed dynamically and is marked UNKNOWN/INFERRED.
- Import target mapping can be inferred from module names.
- Documentation/implementation semantic consistency is not fully verified.
- Resource limits can leave parts of the repository unanalysed.
- Python/JS/TS analysis is deeper than other languages.
- No model is used; provenance records `model: None`, `mode: static`.

## Tests

7 P7.2c tests cover evidence/provenance, source-reference validity, dossier
persistence and reload, source immutability, freshness, limits/symlinks,
conflicting classification and CLI discovery.
