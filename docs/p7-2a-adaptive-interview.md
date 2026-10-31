# P7.2a Adaptive Requirements Interview

Questions are generated from the brief's actual unresolved information. Every
question has a stable ID, category, priority, reason, target requirement, answer
type, options, optional suggested default and blocking flag.

## Priority

- BLOCKING: prevents a coherent architecture.
- IMPORTANT: affects design and may be explicitly deferred.
- OPTIONAL: useful detail.

Only BLOCKING open questions block approval. Optional/important questions may
be answered, deferred or marked `Not decided`. Answers and statuses are stored
in `ProjectState.extensions["p7_2a"]["interview"]`.

## Provenance and normalization

Brief fields and interview answers become requirement items with stable IDs,
type, status (`CONFIRMED`, `DEFERRED`, `UNRESOLVED`, etc.), priority, source
field or question ID, acceptance criteria and user-confirmation marker. The
canonical RequirementContract is regenerated from those items.

Agent defaults are never applied as user decisions. A question's suggested
default remains metadata until the user answers.

## Revision and conflicts

Changing an answer with `revise=True` updates the brief/contract and invalidates
an existing spec approval via the deterministic contract hash. A conflicting
answer without revision creates a BLOCKING conflict question instead of
silently overwriting the previous value.

## Approval and resume

`approve_spec()` refuses while brief blockers exist. Approval is hash-bound to
the normalized contract. Questions, answers, statuses, provenance and approval
survive state reload; answered questions are not repeated.

## TUI / CLI

TUI commands: `/brief {json}` or `/brief field=value` to start with a brief,
then `/brief`, `/brief show`, `/brief questions`, `/answer ID=value`, `/defer ID`
and `/approve-spec`.

CLI: `--brief FILE`, repeatable `--answer ID=VALUE`, repeatable `--defer ID`,
`--show-brief` and explicit `--approve-spec`.
