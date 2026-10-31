# P7.0 inventory — execution dependency

Baseline: `p6.1-gate` (`86f2df1`).

## Active runtime references

| Symbol | Current owner | Runtime use |
|---|---|---|
| `Factory` | `factory.py` | instantiated lazily by `engine.FactoryAdapter` |
| `FactoryAdapter` | `engine.py` | unified/legacy workflow adapter |
| `FactoryState` / `TaskState` | `factory.py` | compatibility view for Factory steps |
| `EngineFacade` / `StateView` | `engine.py` | TUI facade over unified state |
| `Orchestrator` / `SpecContract` / `TaskSpec` | `main.py` | review, follow-up Q&A and legacy TUI path |
| `_run_factory_prepare` / `_run_factory_build` | `main.py` | TUI worker entrypoints |
| `resolve_engine` default `legacy` | `engine.py` | new-run engine selector |

## Business logic owned by Factory

- requirements prompt/refinement and answers
- architecture decomposition and interface/task contract
- deterministic scaffold was already extracted to `scaffold.py`
- build prompt, one-file enforcement, compile check, fix loop, critic gate and workspace install
- integration entrypoint/runtime fix loop
- Playwright audit and multimodal visual review/fix
- sign-off/project memory generation

## Target services

| Service | Responsibility | Status |
|---|---|---|
| `scaffold.py` | deterministic scaffold | extracted (P6) |
| `verify.py` | critic/gate policy | extracted (P5) |
| `workspace.py` / `publish.py` | isolation/publish | extracted (P6/P6.1) |
| `requirements.py` | SPEC + answers | P7.1 first extraction |
| `architecture.py` | architecture + tasks | P7.1 extraction (hardened) |
| `executor.py` | BUILD + fix loop | P7.1b extraction |
| `context_reader.py` | bounded project context | P7.1a extraction |
| `integration.py` | runtime/integration | P7.1c extraction |
| `visual.py` | multimodal | P7.1c extraction |
| `signoff.py` | final sign-off | P7.1c extraction |
| `native_adapter.py` | native composition | P7.1d extraction; not default |
| differential cassette | BUILD/VERIFY/INTEGRATE/VISUAL/SIGNOFF + quality recovery + UNVERIFIED + visual retry | first slices green; fallback/resume/publish pending |
| integration fixer | one call + affected task ids + producer | revalidates changed artefacts (oracle+critic), invalidates stale PASS |

## Guardrails

- No prompt/model/routing/verification policy changes.
- Legacy data remains readable; no silent conversion.
- Runtime unified path must not import `factory.py` after equivalent extraction.
- Each extraction lands with regression tests before old code is removed.
