# P7.2a repository audit

Baseline: `29568c8`. P7.1d matrix is complete (36 COVERED / 0 PARTIAL / 0
MISSING / 12 N/A).

## Current SPEC flow

`WorkflowEngine.spec_step()` calls the adapter's requirements service, persists
`RequirementContract`, then pauses at `spec_questions` or `spec_approval`.
`approve_spec()` stores an `Approval(kind="spec")` bound to the contract content
hash and advances to ARCHITECTURE.

## Existing persistence

`ProjectState` v3 stores requirements, approvals, verifications, history and
`extensions`. The new P7.2a data is stored in the existing `extensions` map, so
old states remain loadable without a schema bump or silent migration. The
canonical `RequirementContract` remains the normalized source of truth.

## Integration decisions

- `brief.py` owns ProjectBrief, interview questions, normalization and
  provenance.
- `RequirementContract.content_hash()` includes `raw["p7_2a"]` when present, so
  revisions invalidate spec approval.
- `WorkflowEngine` gains `start_brief`, `update_brief`,
  `answer_brief_question`, `brief_summary`, `brief_blockers` and
  `brief_questions`.
- Brief mode bypasses the model requirements prompt and uses normalized brief
  data. Legacy projects without a brief keep the previous SPEC path unchanged.
- Blocking questions prevent both approval and the ARCHITECTURE transition.
- TUI supports command-driven brief/interview; CLI supports JSON brief,
  repeatable answers/defer and explicit approval.

## Compatibility

No schema version change; old states deserialize with no `p7_2a` extension.
FactoryAdapter remains default; no routing, billing, verification or
publication contract was changed.
