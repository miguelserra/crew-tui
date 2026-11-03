# P7.2c.2 — Agent Understanding & Context Retrieval (design + implementation)

Baseline: `2bfa7e3`. This milestone adds a **bounded, deterministic, lexical**
retrieval layer over the already-persisted P7.2c dossier. It does **not** add
embeddings, vector search, a model, a second dossier, or a second workflow.

## What already existed (reused, not duplicated)

- `discovery.analyze()` / `discover_project()` — static dossier generation.
- `discovery.load_dossier()` — canonical persisted dossier (single source of truth).
- `discovery.freshness()` — on-demand staleness check.
- `discovery.summary_for_agent()`, `discovery.section()` — basic consumption.
- Dossier `evidence.items` — stable evidence IDs with classification.

## New API (all in `discovery.py`, additive)

| Function | Purpose |
|---|---|
| `retrieve_context(state, query, ...)` | Ranked, bounded retrieval of dossier items |
| `get_project_summary(state, ...)` | Structured, bounded project summary (dict) |
| `get_evidence(state, ids)` | Exact evidence records by stable ID |
| `get_file_context(state, path)` | Modules/symbols/edges for one file |
| `get_symbol_context(state, name, file_path=None)` | Classes/functions/interfaces by name |
| `get_component_context(state, component)` | Component entry + its symbols + edges |
| `section(state, name)` | Now also accepts dotted paths (`structure.classes`) |
| `summary_for_agent(state, ...)` | Extended options; backward compatible |

### `retrieve_context` signature

```python
retrieve_context(state, query, task_context="", sections=None, file_paths=None,
                 symbols=None, evidence_ids=None, max_results=10,
                 max_chars=8000, max_chars_per_result=2000,
                 epistemic_filter=None) -> dict
```

Result shape:

```json
{
  "results": [
    {"section": "structure.classes", "path": "pkg/core.py:1",
     "content": {...} | "... [truncated]",
     "evidence_ids": ["ev-0004"], "epistemic_status": "OBSERVED",
     "match_reason": "exact_symbol:AuthService;token_overlap:1",
     "relevance_score": 1.0, "truncated": false}
  ],
  "total_count": 2, "truncated": false, "evidence_ids": ["ev-0004"],
  "query": "AuthService", "sections_searched": ["structure.classes"],
  "freshness": {"status": "FRESH", "stale": false},
  "max_results": 10, "max_chars": 8000,
  "retrieval": "lexical", "model": null
}
```

## Ranking (deterministic lexical, explainable)

Signals in decreasing priority: exact symbol name (1.0) > exact path (1.0) >
symbol contains query (0.85) > path contains query (0.8) > section name
contains query (0.6) > token overlap (0.4 + 0.1·overlap, capped 0.9).
Tie-break: `(-score, section, path)` — fully deterministic. Every result carries
`match_reason`. This is **lexical**, not semantic: it does not establish
technical correctness.

## Bounds (strict, P7.2c.2.1)

| Parameter | Default | Accepted type | Clamp |
|---|---|---|---|
| `max_results` | 10 | int (not bool) | 1..50 |
| `max_chars` (total) | 8000 | int (not bool) | 200..50000 |
| `max_chars_per_result` | 2000 | int (not bool) | 100..8000 |

**Measurement:** `max_chars` bounds the **complete**
`json.dumps(response, ensure_ascii=False)` string — query, freshness, counts,
result metadata, `evidence_ids`, truncation markers and JSON escaping included.
`max_chars_per_result` bounds each complete serialised result entry.

- Oversized result content becomes a JSON-string preview ending
  `... [truncated]`; the marker is counted in the budget.
- A result whose mandatory provenance metadata cannot fit the remaining budget
  is omitted entirely and `truncated` becomes `true`; partial results never drop
  `evidence_ids` or `epistemic_status`.
- If even the empty-result envelope cannot fit, a documented minimal
  `{"error": "budget_too_small", ...}` response is returned (itself within the
  budget) rather than an oversized success.
- `total_count` is the number of matches *before* output truncation and
  `max_results` limiting.
- Invalid limit **types** raise `TypeError`; out-of-range **integers** are
  clamped (established behaviour). The same policy applies to
  `get_project_summary()` and `get_component_context()`.
- **Minimum clamp is explicit:** a request below the minimum (e.g.
  `max_chars=50`) is clamped to **200**, so the effective budget is 200. APIs do
  not echo the invalid requested value and never exceed the effective budget.
  `get_component_context()` returns a bounded preview object while
  `get_project_summary()` returns a bounded preview/error object; the differing
  sizes reflect their different response formats, not different limits.

## Query semantics

- `retrieve_context()` uses a **non-empty** query for lexical ranking. An empty
  (or whitespace-only) query yields **no matches** (`total_count=0`); it is not
  a "list everything" request. The `symbols`/`file_paths`/`evidence_ids`/
  `sections` parameters *filter a non-empty query*, they do not replace it.
- For exact, query-free symbol lookup use `get_symbol_context(name, file=None)`
  (and `get_file_context()` for a file). These are the documented APIs for
  direct lookup, not `retrieve_context("")`.

## Freshness (last-recorded provenance, P7.2c.2.1/2.2)

Retrieval reads the **last recorded** assessment from
`ProjectState.extensions["understanding"]["last_freshness"]` and never re-scans
the repository nor refreshes the dossier during a read. The freshness object is:

```json
{"status": "FRESH", "stale": false, "source": "last_recorded", "live": false,
 "generated_at": "...Z", "checked_at": "...Z",
 "assessment_source": "generation | live_assessment | null", "reason": ""}
```

- `source`/`live` describe the **retrieval operation**: always
  `last_recorded` / `false`. They never claim a live check happened during
  retrieval.
- `status` is the result of the **stored** assessment and is preserved
  (including `FRESH` and `STALE`). A valid recorded `FRESH` is **not** downgraded
  merely because the stored assessment's own `source` is `live_assessment`.
- `assessment_source` preserves how the underlying assessment was originally
  produced (`generation` from `discover_project`, or `live_assessment` from
  `freshness()`), so provenance is not lost. `null` when unavailable.
- `generated_at` is the dossier generation timestamp; `checked_at` is when the
  last assessment ran. Retrieval never fabricates `checked_at`.
- Generation records an initial assessment: `FRESH` only if the generation pass
  had complete hash coverage, otherwise `UNKNOWN`.
- `freshness()` records `checked_at` and persists the updated metadata through
  the existing `ProjectState` path (engine/CLI callers save it).
- A missing/unreadable/invalid dossier, missing metadata, malformed metadata, or
  a `FRESH` record without a `checked_at` all yield `UNKNOWN` — never `FRESH`.
- `STALE` is preserved. Refresh remains an explicit
  `discover_project(refresh=True)` action.

## Epistemic integrity

Every item keeps its classification (`OBSERVED`/`INFERRED`/`UNKNOWN`/
`CONFLICTING`) and any stable evidence IDs (mapped from
`evidence.items` by source path). `epistemic_filter` restricts results to the
requested classifications.

## Agent integration (actual, and its limit)

`WorkflowEngine` gained additive methods: `retrieve_context()`,
`project_summary()`, `project_summary_structured()`, `component_context()`,
`evidence()`. They delegate to `discovery` and make **no** model call, network
call, or repository scan. Routing, `RouteChain`, `CallLedger`, PAYG billing and
adapter selection are untouched.

There is no LLM tool/function-calling interface in the codebase, so an agent
cannot yet *dynamically* request retrieval mid-generation; that is deferred
rather than faked. The compact summary is available for prompt initialisation and
the retrieval API is available to any orchestration code.

## Compatibility

- Dossier schema unchanged (`p7-2c/2`); no `DISCOVERY_VERSION` bump.
- `section()` keeps its original output for old callers; new parameters optional.
- `summary_for_agent()` keeps the legacy **default** behaviour: risks are
  included unless `include_risks=False` is passed explicitly.
- Minimal/legacy (`p7-2c/1`-style) dossiers are tolerated: missing sections
  return `None`/`[]`, never invented content.
- Unknown sections/symbols/evidence IDs return explicit empty results.

## Tests

`tests/test_p72c2_retrieval.py` — 51 tests covering summary/section/evidence/
lookup, exact-vs-partial ranking and deterministic tie-break, strict complete-
response and per-result bounds (incl. unicode/escaping and the 200-char floor),
limit-type validation and clamping, last-recorded freshness provenance
(including missing/malformed metadata and missing/invalid dossiers),
generation coverage honesty, no-rescan/no-mutation, legacy dossier tolerance,
and `WorkflowEngine` equivalence without model calls.
