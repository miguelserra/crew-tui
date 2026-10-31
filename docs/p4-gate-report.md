# P4 gate report — central Router, fallback, escalation and scoped PAYG

Reference:

- P4 code — `725ee11`
- `p4-gate` — tag on this report commit
- CI — `41148888924`: **success** (Linux 3.10/3.12/3.13, Windows 3.13,
  integration Linux+Windows)

Scope: P4 only. No critic/oracle/visual policy change (P5), no profiles/
scaffold change (P6), no legacy removal (P7).

## Router contracts

`router.py` provides:

- `RouteRequest` — role, complexity, required capabilities, context, billing
  policy, task/run id, producer, escalation flag;
- `RouteCandidate` — Registry identity + `ModelRef` + provider handle +
  reasons;
- `RouteResult` — ordered candidates/skips;
- `RouteChain` — candidate-by-candidate execution (fallback only).

The Router does not construct LangChain clients; `LLMPool._handle_for_identity`
is the provider layer, with handles cached per identity/tag/options.

## Fallback vs escalation

- **Fallback** (`RouteChain`) handles technical failure only: timeout (one
  retry), connection, 429 rate limit (Retry-After), quota exhaustion
  (separate state), 5xx, empty response, auth-disable.
- **Escalation** (`Orchestrator._escalate`) is called only after verification
  failure, uses a stronger chain and excludes the failed producer. A technical
  failure never triggers escalation directly.
- HTTP 400 does not fall back blindly; 401/403 disables the credential; quota
  is not confused with rate limiting.

## Circuit breaker

`CircuitBreaker` is keyed by **provider + endpoint + credential profile**:

- 3 consecutive rate limits within the window → 15-minute cooldown (or the
  larger `Retry-After`);
- quota exhaustion has a separate longer cooldown;
- auth failure disables the credential;
- after cooldown a controlled half-open probe is allowed; success resets,
  failure reopens;
- state persists in `logs/router_state.json`, so a restart does not erase a
  cooldown.

## PAYG with explicit scope

- `GrantStore` holds task-scoped or run-scoped grants. A TUI approval creates a
  **task** grant; `--allow-paid` creates an explicit **run** grant.
- `LLMPool._activate_paid` and its global mutation were removed. A task grant
  cannot authorize another task.
- Every candidate in fallback and escalation passes `Router.authorize`:
  Registry billing → grant check → callback; `unknown` remains blocked.
- Direct single-handle invocations use the same grant check (`_ensure_paid`).

## Complexity policy

`Task.complexity` prevails; the token heuristic is only an `auto` fallback.

| Complexity | Chain |
|---|---|
| tiny/local | Qwen local (if up) → normal free chain |
| normal coding | Gemini Flash → Flash alt → Pro → Nemotron → Argon |
| complex/architecture | Gemini Pro → Nemotron → Kimi/Argon → DeepSeek PAYG |
| fixer | Big Pickle (only `available=True`) → Gemini Pro → stronger authorized |

`team_llm`, `Factory._llm` and `_escalate` now use this Router. Hard tasks start
with the complex chain (the old “hard → Flash” path is removed).

## Mandatory P4 test mapping

| Test | Result |
|---|---|
| Flash 429 → next candidate | `test_429_falls_back_to_next_candidate` |
| Flash timeout → next | `test_timeout_retries_once`, `test_timeout_twice_falls_back_to_next` |
| 3 rate limits → 15 min cooldown | `BreakerTests.test_three_rate_limits_open_and_persist` |
| Quota ≠ rate limit | `test_rate_limit_vs_quota`, `test_quota_is_not_rate_limit` |
| Cooldown skips invocation | `test_cooldown_skips_invocation` |
| PAYG without approval | `test_payg_without_grant_is_blocked` |
| Task grant does not authorize all | `test_task_grant_does_not_authorize_other_task`, `test_task_grant_allows_only_that_task` |
| Hard task starts complex | P4 `test_hard_task_starts_complex`, P1 `test_hard_tasks_start_complex_chain` |
| Verification failure → escalation | `test_escalation_uses_stronger_chain_than_fallback`, `test_escalation_excludes_producer` |
| Text-only rejected for visual | `test_visual_requires_vision`, `test_text_only_registry_has_no_visual_candidate` |
| Multiple attempts | P1 attempt ledger tests; route retries test |
| Producer after fixer | P1/P2 attempt/delivered-by tests |

The four legacy expected failures were reviewed: the hard-task one is now a
normal test; the remaining **three are P5** (cross-family critic, critic
unavailable, advisory local test).

## Tests

| Suite | Result |
|---|---|
| P1 core | 79 tests, OK (12 integration skips without deps) |
| P2 core | 34 tests, OK (2 skips) |
| P3 core | 18 tests, OK (7 skips) |
| P4 core | 22 tests, OK |
| P1/P2/P3 integration | 21 tests, OK, 3 expected failures (P5) |
| power + budget | OK |
| `py_compile` / `git diff --check` | OK |

## Smoke (fake OpenAI-compatible server, no external keys)

A unified run completed through the Router: `SPEC → ARCHITECTURE gate →
SCAFFOLD → BUILD → INTEGRATE → SIGN-OFF`, task delivered, one attempt recorded,
telemetry showing subscription billing and identity snapshots. Fallback,
cooldown, quota, grants, hard-task and visual-filter behaviour are covered by
the dependency-free P4 tests.

## Deferred

P5 critic/oracle/visual policy, P6 profiles/deterministic scaffold and P7/P8
legacy removal remain untouched. The three P5 expected failures stay reserved.
