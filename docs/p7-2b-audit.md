# P7.2b repository audit

Baseline: `09a5e7d`.

Existing `engine_kind` distinguishes legacy/unified execution format, not the
adapter. The canonical ProjectState already has `extensions`, so P7.2b stores
`engine_identity` and `project_origin` there without a schema bump.

Before P7.2b, new unified runs used FactoryAdapter and legacy runs used `.factory`.
P7.2b centralizes adapter selection in `engine_selection.py`, builds NativeAdapter
for new projects by default, preserves persisted identities on resume, blocks
ambiguous unified states, and rejects conflicting overrides.

External adoption is implemented in `adoption.py` as a non-destructive
registration and baseline step. It does not infer migration from an old Crew
runtime and does not restructure the repository.
