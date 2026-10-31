# P7 full-pipeline differential audit

## 1. Adapter execution paths

- **FactoryAdapter** (`engine.py`): drives the legacy `Factory` steps over a
  `ProjectState`. `Factory._ask()` -> `_llm()` -> `pool.router.route(request)`
  -> `pool.complete(chain, messages, meta)`.
- **NativeAdapter** (`native_adapter.py`): drives native services. The executor
  calls `pool.team_llm(task, run_id)` -> `pool.router.route(request)` ->
  `pool.complete(chain, messages, meta)`.
- Both adapters converge on `LLMPool.complete()`, which invokes the real
  `RouteChain` returned by the real `Router`.

## 2. Components bypassed by the existing `FakePool` (adapter isolation harness)

| Component | Bypassed? | Reason |
|---|---|---|
| `ModelRegistry` | yes | models are plain `ModelRef("fake")` |
| `Router` / `RouteChain` | yes | `FakePool.complete` returns a canned string; no routing/classification |
| `LLMPool.complete` attempt/ledger wiring | yes | `FakePool` hand-rolls a single attempt event |
| `CallLedger` | yes | no ledger attached; billing is `local` |
| `RouteRequest` fingerprint | no | not exercised (no ledger) |
| provider transport | yes | response is a hard-coded string |

Consequence: the existing 12 differential tests prove **adapter execution
behaviour** (task order, artifacts, attempts, verification) but are **not**
evidence of routing, provider classification, or PAYG ledger parity.

## 3. Real component interfaces and dependencies

- `Router(registry, handle_provider, breaker, grants, availability, request_grant, allow_unknown)`
  is fully injectable: `handle_provider(identity, tag, max_tokens, thinking)`
  returns the transport handle. This is the external provider boundary.
- `RouteChain.invoke(messages)` iterates `RouteCandidate.handle.invoke()`,
  classifies errors via `classify_error()`, writes the `CallLedger`
  (`router.call_ledger`), and raises `CallOutcomeUnknown` on ambiguous PAYG
  outcomes.
- `CallLedger(path)` persists to `.crew/calls.json` and
  `.crew/call_results/<call_id>.json`.
- `WorkflowEngine.apply_pool()` loads the ledger, sets `pool.router.call_ledger`,
  `pool.dispatch_guard`, and `pool.attempt_sink`; `step()` pauses on unresolved
  or unrecoverable calls.

## 4. Available deterministic provider injection points

- `Router.handle_provider` -> fake `handle.invoke(messages)` (returns content or
  raises 429/auth/bad-request/timeout/connection/server).
- `Router.availability` -> constant `True` (no endpoint probes).
- `ModelRegistry(models=[...])` -> in-memory identities, no `models.toml`.

## 5. Current D03–D06 coverage

| Scenario | Coverage | Evidence |
|---|---|---|
| D03 routing/escalation/breaker | MISSING | no adapter-level routing test |
| D04 technical fallback | PARTIAL | `FakePool` fallback_script emulates outcomes; real `classify_error` not exercised |
| D05 PAYG ledger parity | PARTIAL | `tests/test_p04_calls.py` (shared RouteChain/ledger), not through both adapters |
| D06 confirmed replay/recovery | PARTIAL | same shared-component tests, not adapter parity |

## 6. Exact coverage gaps

- No test runs the real `Router`+`RouteChain`+`CallLedger` while both adapters
  execute and compare provider invocation counts, ledger transitions, replay
  eligibility and pause state.
- No transport-level 429/auth/bad-request/timeout/connection/server script.
- No adapter-level replay/resume with a real ledger.

## 7. Proposed minimum changes

1. Add a **full-pipeline harness** (`tests/test_p7_pipeline.py`) that keeps the
   real `Router`, `RouteChain` and `CallLedger`, injects a fake
   `handle_provider`, and runs both adapters through `WorkflowEngine`.
2. Simulate the transport only: `handle.invoke()` returns/raises per a script.
3. A thin pool shim reproduces `LLMPool.complete()`'s attempt/context/ledger
   wiring (the heavyweight `LLMPool` cannot start headless in CI).
4. Add D03–D06 variants; classify every dimension; keep the gate CLOSED until
   all mandatory cells are COVERED.

## 8. Resolution status

- [x] D02 dependency ordering (already fixed and covered in `9e51b4b`).
- [ ] D03 – outstanding (no adapter-level routing test).
- [~] D04 – partially resolved: adapter-level 429 fallback now runs through
  both adapters (`test_p7_pipeline_adapters`); auth/bad-request/connection/
  server variants are still outstanding.
- [~] D05 – partially resolved: adapter-level PAYG timeout -> UNKNOWN and
  resume blocking (no retry) now run through both adapters; other ledger
  variants are still outstanding. This also exposed and fixed a real defect:
  `FactoryAdapter` did not propagate `run_id` to `Factory`, bypassing the
  ledger on the Factory path.
- [~] D06 – partially resolved at shared-routechain level; adapter-level
  replay/resume is still outstanding.

Full-pipeline tests live in `tests/test_p7_pipeline.py` and use the real
`Router`, `RouteChain` and `CallLedger` with a scripted `handle_provider` as
the transport boundary. They are shared-component evidence, not adapter
parity; the matrix keeps those cells PARTIAL until both adapters execute the
same scenarios through `WorkflowEngine`.
