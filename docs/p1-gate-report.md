# P1 gate report — Crew canonical state and engine foundation

Reference points:

- `p1-foundation` — `b5d5a2e` (P0/P1 foundation before the four gate fixes)
- `p1-gate` — commit tagged after this report (code + report)

Scope: P0/P1 only. No routing, critic, visual or verification policy from
P3–P5 was changed.

## The four gate points

### 1. Single Crew entry

- CLI: `crew-tui "objetivo" [--project DIR]` is the entry.
- TUI: an ordinary instruction starts the workflow. `/factory` and `/fabrica`
  print a deprecation notice and forward to the same entry.
- `--factory` is a deprecated alias with a warning; it no longer describes a
  separate product mode.
- Questions/follow-ups and the transitional read-only review profile still use
  the legacy Orchestrator until P2/P6; build runs use the selected engine.

### 2. `--migrate-engine` is a real migration

`engine.migrate_engine` now:

1. validates `schema_version >= 2`, phase, task ids, build target files,
   dependency references and cycles, contracts and checkpoints;
2. normalizes a legacy state for the corrected order (missing requirements →
   `SPEC`; missing architecture → `ARCHITECTURE`; stale/missing architecture
   approval → gate closed);
3. writes a verifiable snapshot
   `.crew/history/engine-source-<sha256>.json`;
4. works on a copy, saves atomically, and only then exposes `engine_kind=unified`;
5. is idempotent once unified;
6. never touches `.factory/state.json` and leaves the source state intact if
   validation or the final save fails.

Format migration (`--migrate-state`) and engine migration are distinct
operations.

### 3. CI on both operating systems

- `41109522173` — P0/P1 foundation: **success**
  (Linux 3.10/3.12/3.13, Windows 3.13, integration Linux+Windows).
- `41110678201` — four gate fixes: **success**
  (same matrix).
- Final docs gate run: recorded on the `p1-gate` tag commit.

### 4. Symlink-safe target files

`validate_tasks` now resolves each project-relative target and requires the
resolved path to stay inside the project root (case-normalized prefix check).
A symlink that escapes the root is rejected. `review`/`prose` keep the option
of an empty target; build tasks remain unique case-insensitively.

## Tests (local, no external credentials)

| Suite | Result |
|---|---|
| `python -m unittest discover -s tests -p "test_p01_*.py"` | 79 tests, OK (12 integration skips without deps) |
| `python -m unittest tests.test_p01_integration` (venv deps) | 12 tests, OK, 4 expected failures |
| `python tests/test_power.py` | `POWER_TESTS_OK` |
| `python tests/test_budget.py` | `BUDGET_TESTS_OK` |
| `py_compile` all modules | OK |
| `git diff --check` | OK |

Expected failures intentionally reserved for P4/P5:

- hard tasks must select the escalation path;
- a Gemini producer must get a different-family critic;
- critic `UNAVAILABLE` must not become `PASS`;
- a local LLM test must be advisory, not the sole verdict.

## Manual smoke (fake OpenAI-compatible server, no keys)

A local server on `127.0.0.1:8765` answered the Factory prompts. Environment:
`OPENCODE_BASE_URL` pointed at it; Gemini/OpenRouter/DeepSeek/local keys were
empty; `SENIOR_PROBE=0`, `TEST_ORACLE=0`, `VISUAL_CHECK=0`.

1. **Unified full run** (`CREW_ENGINE=unified`):
   `SPEC → ARCHITECTURE gate → SCAFFOLD → BUILD → INTEGRATE → SIGN-OFF`.
   Result: `.crew/state.json` `engine_kind=unified`, `engine_version=1.0`,
   `schema_version=2`, `phase=DONE`, approvals `spec` + `architecture`,
   `app.py` delivered, task `delivered_by=fake-model`, 1 recorded attempt.
2. **Legacy full run** (`CREW_ENGINE=legacy`): `.factory/state.json` with
   `engine_kind=legacy`, `engine_version=legacy`, `schema_version=1`,
   persisted `run_id`, task `delivered_by=fake-model` and 1 attempt.
3. **Format migration + resume**: `--migrate-state` produced `.crew` with
   `engine_kind=legacy`, `schema_version=2`; `--resume` with
   `CREW_ENGINE=unified` printed a warning, used the persisted legacy engine,
   re-opened the architecture gate, and finished `DONE`. `.factory` was
   preserved.
4. **Engine migration validation**: idempotent; missing dependencies/cycles
   rejected; stale approval re-opens the gate; snapshot exists; a forced save
   failure leaves the source byte-identical.
5. **Corruption and symlink**: corrupt `.crew` raises and preserves bytes;
   a symlink escaping the project is rejected.

## Reproduction

```bash
# core (no deps)
python -m unittest discover -s tests -p "test_p01_*.py"
# integration (venv)
.venv/bin/python -m unittest tests.test_p01_integration
# migration
python main.py --migrate-state --project /path/to/project
python main.py --migrate-engine --project /path/to/project
# resume uses persisted engine_kind; CREW_ENGINE only affects new runs
python main.py --resume --project /path/to/project --approve-architecture
```

## Deferred to P2+

- unify `SpecContract`/`TaskSpec`/`FactoryState`/`TaskState` into the single
  `ProjectState` contracts;
- remove the transitional Orchestrator paths (questions/review);
- P3 registry/pricing, P4 router/fallback-vs-escalation, P5 mandatory
  cross-family critic and verification policy, P6 profiles/deterministic
  scaffold, P7/P8 UI and legacy removal.
