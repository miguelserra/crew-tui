# P7.2c Project Discovery and Architecture Understanding

## Data flow

1. Reuse `adoption.inspect_repository()` for bounded inventory and hashes.
2. Statically parse eligible Python/JS/TS/config/documentation files.
3. Extract entry points, modules, dependencies, technology signals, tests, CI,
   markers, persistence/external-interface signals and evidence references.
4. Classify findings as OBSERVED, INFERRED, UNKNOWN or CONFLICTING.
5. Generate Mermaid component/dependency diagrams only from observed evidence.
6. Write `.crew/understanding/dossier.json` and `.md`.
7. Store compact summary, paths, HEAD and coverage metadata in
   `ProjectState.extensions["understanding"]`.
8. Freshness compares stored bounded hashes against current repository state.

## Safety

- Static analysis only; runtime execution and dependency installation are not
  performed.
- Symbols/links outside the repository are not followed.
- Resource limits and incomplete coverage are explicit.
- Discovery never advances workflow phase, approval or architecture state.
- Chesterton's Fence policy is recorded in the dossier.

## Agent consumption

`summary_for_agent()` returns a concise overview; `section()` and source
references allow on-demand detail. The full dossier is never injected by
default.
