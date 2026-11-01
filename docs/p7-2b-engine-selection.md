# P7.2b engine selection

Adapter identity is stored in `ProjectState.extensions["engine_identity"]`
(`native` or `factory`) and project origin in `extensions["project_origin"]`.

## Selection precedence

1. Persisted project identity.
2. Explicit new/adoption selection (`--engine` / TUI `/engine`).
3. NativeAdapter default for new projects.

Existing `.factory` legacy states without an identity infer FactoryAdapter.
Unified states without identity are ambiguous and block resume unless the user
resolves them explicitly with `--engine native|factory`. A conflicting override
is rejected.

Explicit resolution of an ambiguous identity is persisted by
`resolve_and_persist()`, so it survives save/reload/repeated resume.

New projects use NativeAdapter by default. FactoryAdapter remains available via
explicit selection and for existing factory projects. No automatic migration or
engine switch is performed.

`--status` and the TUI status line report origin, persisted identity, effective
adapter and selection reason.
