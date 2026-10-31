# P7.2a Project Brief

The Project Brief is a structured, provenance-rich initial input stored in
`ProjectState.extensions["p7_2a"]["brief"]`. It feeds the canonical
`RequirementContract` and does not create a second requirements source of truth.

## Mandatory fields

- `project_name`
- `project_description`
- `main_objective`

## Optional fields

Application type, platforms, language, technologies, features, workflows,
business rules, data inputs/outputs/storage, integrations, interface,
non-functional requirements, deployment and additional constraints.

Optional fields may be empty or explicitly marked `Not decided`. Missing
optional information becomes an adaptive question only when it is material.
Only mandatory fields and BLOCKING interview decisions can block architecture.

## Partial submission

`WorkflowEngine.start_brief()` / `update_brief()` accept partial dictionaries.
`/brief field=value` (TUI) and `--brief FILE` (CLI) support partial data.
Saving and loading preserves all supplied values and undecided markers.
