# P7.2c.2 — Agent Understanding & Context Retrieval Audit

> **Historical Phase-0 record (kept intentionally).** This document captures the
> gap analysis performed *before* implementation. It describes the starting
> state, not the final P7.2c.2 contract. The authoritative, implemented contract
> is `docs/p7-2c2-design.md`; where the two differ, the design document wins.
>
> Reconciliation (P7.2c.2.1):
> - The initial "missing" retrieval items (task retrieval, evidence-by-ID,
>   dotted sections, bounds) were **addressed** by `retrieve_context`,
>   `get_evidence`, `section()` dotted paths and strict bounds.
> - Freshness was **narrowed**: it is dossier-level and **last-recorded**, not
>   per-section live freshness. Per-section staleness and automatic refresh on
>   retrieval were **not implemented** (deferred by design).
> - Output bounds were later made **strict** (complete serialised response), and
>   limit inputs validate types (see the design document).
> - Agent tool/function-calling for mid-generation retrieval remains deferred.

**Baseline:** `2bfa7e3` (main), clean tree. This milestone focuses on context retrieval
for agents, building on the P7.2c.1 Discovery Core.

---

## Existing Capabilities (from P7.2c.1)

### Discovery Core (`discovery.py`)
- **`analyze(root, ...)`** — Full static analysis, returns complete dossier
- **`discover_project(state, store, refresh)`** — Orchestrates analysis, persists dossier
- **`freshness(state, root)`** — Checks dossier staleness via hash comparison
- **`section(state, name)`** — Returns a single dossier section by name
- **`summary_for_agent(state, max_chars=4000)`** — Returns concise agent summary
- **`section(state, name)`** — Returns single dossier section by name
- **`load_dossier(state)`** — Loads persisted dossier JSON
- **`load_dossier(state)`** — Returns parsed dossier dict

### Dossier Structure (p7-2c/2)
```json
{
  "schema": "p7-2c/2",
  "project": { "root", "name", "description" },
  "repository": { "root", "git_available", "head", "branch", "dirty", "dirty_paths" },
  "overview": { "project_name", "top_level_directories", "entry_points", "source_languages", "file_count" },
  "inventory": { ... },
  "technology_stack": { "languages", "frameworks", "package_managers", "build_systems", "test_frameworks", "dependencies" },
  "components": [...],
  "structure": { "modules", "classes", "functions", "interfaces", "unsupported_source_suffixes" },
  "dependencies": { "internal", "external_imports", "manifests", "declared_dependencies", "unresolved_formats" },
  "entry_points": [...],
  "execution_flows": [...],
  "external_interfaces": [...],
  "persistence": [...],
  "tests": { "files", "directories", "configuration", "frameworks", "execution_status" },
  "evidence": { "coverage", "classifications", "items" },
  "quality_signals": {...},
  "risks": [...],
  "preserve": [...],
  "unresolved_questions": [...],
  "conflicts": [...],
  "analysis_limitations": [...],
  "epistemic": { "OBSERVED", "INFERRED", "UNKNOWN", "CONFLICTING" },
  "diagrams": { "component", "dependency" },
  "provenance": { "tool", "version", "model": null, "mode": "static" },
  "chestertons_fence": { ... }
}
```

### Existing Retrieval Capabilities (Limited)
| Function | Purpose | Limitations |
|----------|---------|-------------|
| `summary_for_agent(state, max_chars=4000)` | Fixed 4k-char project summary | No parameterization, always returns full summary |
| `section(state, name)` | Returns one top-level section by name | Only top-level keys; no sub-section or query support |
| `section(state, name)` | Returns one dossier section | No filtering, no search, no relevance ranking |
| `discovery.analyze()` | Full static analysis | Returns entire dossier; no selective retrieval |
| `discovery.freshness()` | Checks dossier staleness | No granular staleness per section |
| `discovery.section(state, name)` | Returns one section by name | No filtering, no pagination |

### Current Agent Consumption (in `engine.py` and `main.py`)
| Consumer | Method | Usage |
|----------|--------|-------|
| `WorkflowEngine.discover_project()` | `discovery.discover_project()` | Full discovery on project init |
| `WorkflowEngine.understanding_summary()` | `discovery.summary_for_agent()` | Agent context on phase start |
| `WorkflowEngine.understanding_freshness()` | `discovery.freshness()` | Dossier staleness check |
| `WorkflowEngine.understanding_section()` | `discovery.section()` | CLI `/understanding <section>` |
| `discovery.summary_for_agent()` | Fixed 4k-char summary | Used by agents at phase start |
| `discovery.section()` | Single section by name | CLI `/understanding <section>` |
| `discovery.freshness()` | Dossier staleness check | Called on resume/phase transition |

---

## Gap Analysis (P7.2c.2 Requirements vs. Current State)

| Requirement (from Phase 1 Design) | Current State | Gap |
|-----------------------------------|---------------|-----|
| **R01** Compact project summary | `summary_for_agent()` exists, fixed 4k chars | Need parameterizable length, optional sections |
| **R02** Section retrieval | `section()` exists, only top-level | No sub-sections, no filtering, no pagination |
| **R03** File/symbol retrieval | Dossier has `structure.*` but no API | No `get_symbol()`, `get_file()`, `search_symbols()` |
| **R04** Evidence retrieval | Evidence exists in `evidence.items` | No `get_evidence(ids)` API |
| **R05** Task-oriented retrieval | **MISSING** | No `retrieve_context(query, task_context, ...)` |
| **R05** Task-oriented retrieval with task context | **MISSING** | No way to pass task context for relevance |
| **R06** Bounded output | `summary_for_agent(max_chars)` | No per-result limits, no total budget |
| **R07** Freshness awareness | `freshness()` exists | No per-section staleness, no automatic stale marking |
| **R08** Epistemic integrity | Preserved in dossier | No explicit filtering by epistemic status in retrieval |
| **R09** Determinism | Dossier is deterministic | No explicit guarantee for retrieval order |
| **R10** Agent integration | `summary_for_agent`, `section()` | No on-demand retrieval API for agents |
| **R11** No model calls | All static | ✅ Satisfied |
| **R12** Compatibility | Existing APIs work | Need additive extension only |

---

## Missing Capabilities (P7.2c.2 Scope)

### 1. **Retrieval API** (Core Gap)
- `retrieve_context(query, task_context, max_results, max_chars, sections, file_paths, symbols, evidence_ids)`
- `get_project_summary(params)` with configurable sections/length
- `get_component_context(component, max_chars)`
- `get_evidence(evidence_ids)`

### 2. **Structured Retrieval Results**
- Return type with: `results: [RetrievedItem]`, `total_count`, `truncated: bool`, `evidence_ids: []`
- Each `RetrievedItem`: `section`, `path`, `content`, `evidence_ids`, `epistemic_status`, `relevance_score`, `match_reason`

### 3. **Ranking & Scoring** (deterministic, lexical)
- Exact match > prefix match > token overlap
- Deterministic tie-breaking (lexicographic)
- Explicit match reason in results

### 4. **Output Bounds**
- Per-request `max_results` and `max_chars` (with defaults)
- Per-result `max_chars` truncation with ellipsis
- Total output budget enforcement

### 3. **Freshness Awareness**
- Per-section staleness flags in results
- Stale sections marked in results
- Optional auto-refresh trigger (explicit, not automatic)

### 5. **Epistemic Filtering**
- Filter by `OBSERVED`, `INFERRED`, `UNKNOWN`, `CONFLICTING`
- Preserve epistemic status in results

### 6. **Agent Integration**
- `WorkflowEngine` methods to get summary/retrieve on demand
- Agent tool/function for on-demand retrieval (if tool interface exists)
- Compact summary injection at phase start (already exists)

---

## Existing Components to Reuse

| Component | Reuse Strategy |
|-----------|----------------|
| `discovery.analyze()` | Already produces full dossier — reuse for data source |
| `discovery.section()` | Extend to support sub-sections, filtering |
| `discovery.summary_for_agent()` | Extend with parameters, reuse formatting logic |
| `discovery.section()` | Extend with filtering, pagination |
| `discovery.freshness()` | Extend to return per-section freshness |
| `dossier["evidence"]["items"]` | Use for `get_evidence()` |
| `dossier["structure"]` | Symbol/file lookup |
| `dossier["evidence"]["items"]` | Evidence retrieval by ID |
| `dossier["structure"]` | Symbol/component lookup |
| `discovery.freshness()` | Extend for per-section staleness |

---

## Files to Modify / Create

### Modify (Extend)
- `discovery.py` — Add retrieval API functions
- `engine.py` — Add `retrieve_context()` to `WorkflowEngine`
- `native_adapter.py` — Add `retrieve_context()` to `NativeAdapter` (optional)
- `main.py` — CLI `/retrieve` command (optional)

### Create (New)
- `docs/p7-2c2-design.md` — Design document
- `tests/test_p72c2_retrieval.py` — New test file
- `docs/p7-2c2-audit.md` — This file

---

## Compatibility Requirements

| Requirement | Approach |
|-------------|----------|
| Existing p7-2c/1 dossiers | Load without error; new fields optional |
| Existing p7-2c/2 dossiers | Full compatibility |
| `summary_for_agent()` | Extend with optional params, backward compatible |
| `section()` | Extend with optional params, backward compatible |
| `discovery.analyze()` | Unchanged |
| `discovery.section()` | Extend with optional params, backward compatible |
| `discovery.freshness()` | Add per-section freshness, backward compatible |
| `WorkflowEngine` methods | Additive only, no signature changes |
| Dossier schema | Additive fields only (optional new fields) |

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Breaking existing CLI/API | Low | High | Additive changes only, extensive tests |
| Performance on large repos | Medium | Medium | Bounded limits, lazy loading |
| Breaking existing CLI commands | Low | High | Extend, don't replace |
| Agent integration complexity | Medium | Medium | Start with `WorkflowEngine` methods only |
| Determinism of ranking | Low | Medium | Explicit deterministic rules, tests |

---

## Phase 0 Deliverable

This audit establishes the baseline for P7.2c.2 implementation. The next phase is **Phase 1: Retrieval Design** where we define the exact API contracts.

---

*Generated: 2026-11-02 | Baseline: 2bfa7e3 | P7.2c.2 Phase 0 Complete*