# P3 gate report — Model Registry, billing and telemetry

Reference:

- P3 code — `6ea242c`
- `p3-gate` — tag on this report commit
- CI code run — `41118221510`: **success**

Scope: P3 only. No routing/fallback/escalation change (P4), no critic/oracle/
visual policy change (P5), no profiles/scaffold change (P6), no legacy removal
(P7).

## Model identity and registry

`models.toml` is the declarative registry:

- identity = `provider + endpoint + model_id + credential_profile`;
- `ModelIdentity` = billing, capabilities, pricing/provenance, quota semantics;
- `ModelRef` = effective model of one call + an identity snapshot;
- `TaskAttempt.model` carries that snapshot into history/telemetry.

The same id on two endpoints is two identities (for example
`opencode-go/deepseek-v4-pro` subscription vs `deepseek-api/deepseek-v4-pro`
PAYG).

## Five P3 requirements

### 1. Unambiguous identity

All active handles are built through `ModelRegistry.identity_for(...)`; the
display name is never the source of provider/billing/capabilities. The P3
integration test walks every role handle and fail-closes on any `undefined` or
`unknown` identity.

### 2. Rigorous billing

Five explicit classes: `local`, `free_quota`, `subscription`, `payg`,
`unknown`.

- `subscription`/`free_quota` record **quota consumption** (tokens, tier), not
  a fake zero-dollar PAYG cost.
- only `payg` is billable; its cost is the Decimal estimate.
- `unknown` is never coerced into a cheaper class and remains blocked by the
  dispatch guard without authorization.

### 3. Decimal pricing

`ModelPrice` uses `Decimal`, normalized per 1M tokens and currency, with
`source` + `updated`. `CostEstimate` returns Decimal input/output/total, basis,
billable and quota flags. JSON snapshots serialize prices as strings.

### 4. Explicit capabilities

`text`, `vision`, `json`, `tools` come from `models.toml`; no name heuristics.
Gemini entries declare vision; DeepSeek/Kimi/Argon/Nemotron declarations do
not. Unknown models have no capabilities.

### 5. Centralized telemetry/dashboard

Removed the name-based `model_tier` / `estimate_cost` / `PRICING` path.
`LLMPool._record_usage` reads the effective `ModelRef` snapshot and the
Registry cost estimate. Events now carry provider, endpoint, credential
profile, billing, capabilities, identity snapshot, Decimal cost/basis,
billable and quota flags. The dashboard groups by billing class and shows PAYG
cost and quota tokens separately.

## OpenRouter and Big Pickle

- OpenRouter `/models` pricing is parsed. A model is `free_quota` only when
  **both** prompt and completion are zero; a non-zero price downgrades an
  existing free classification to `payg`.
- Unregistered OpenRouter models stay `unknown` (blocked).
- `big-pickle` is `opencode-free/free_quota`; `_probe_senior` performs a real
  cheap invoke and records availability in `ModelRef.available`. The probe is
  guarded so an unknown senior model cannot bypass the registry.

## Exit gate evidence

| Gate | Evidence |
|---|---|
| 1 all active handles identified | `tests/test_p03_integration.py::test_active_handles_have_registered_identity` |
| 2 profiles distinguish endpoints | registry identity tests; two DeepSeek identities |
| 3 OpenRouter free only 0/0 | `test_free_only_when_both_prices_zero`; discovery override |
| 4 Big Pickle tested | `test_big_pickle_probe_records_availability` + fake-server smoke |
| 5 subscription vs PAYG | `test_subscription_is_not_zero_cost_payg`, `test_dashboard_separates_payg_cost_from_quota`, telemetry smoke |
| 6 unknown blocked | dispatch guard + `test_unknown_billing_blocks_before_invoke` |
| 7 dashboard/telemetry use Registry | `_record_usage` + dashboard tests; legacy tier mapping |
| 8 historical snapshots | `ModelRef.identity`; P2/P3 snapshot round-trips |
| 9 Linux/Windows, no credentials | CI `41118221510` success (Linux 3.10/3.12/3.13, Windows 3.13, integration Linux+Windows) |

## Tests

| Suite | Result |
|---|---|
| P1 core | 79 tests, OK (12 integration skips without deps) |
| P2 core | 34 tests, OK (2 skips) |
| P3 core | 18 tests, OK (7 integration skips without deps; TOML tests skipped on 3.10 without tomli) |
| P1/P2/P3 integration | 21 tests, OK, 4 expected failures (P4/P5) |
| power + budget | OK |
| `py_compile` / `git diff --check` | OK |

## Manual smoke (local fake OpenAI-compatible server, no external keys)

- Unified run used `opencode-free/big-pickle` (probe OK) and OpenCode Go
  subscription handles: telemetry showed `free_quota`/`subscription`, cost 0,
  quota consumed, and identity snapshots with source/date.
- A second run with a direct DeepSeek key produced `billing=payg`,
  provider `deepseek`, endpoint `direct-api`, credential profile
  `deepseek-api`, `cost_basis=payg`, `billable=true`, `quota_consumed=false`.
- Attempt snapshots retained the requested identity and price/capabilities.
- Dashboard separated PAYG cost from subscription/free quota tokens.

## Documentation cleanup

- Mermaid now shows `SPEC → ARCHITECTURE → SCAFFOLD → BUILD → VERIFY/FIX →
  INTEGRATE → VISUAL → SIGN-OFF`.
- `SpecContract` is described as a legacy compatibility format; the canonical
  contract is `RequirementContract`/`ProjectState`.
- Windows example uses the single positional entry, not `--factory`.
- Billing terminology is `local|free_quota|subscription|payg|unknown`.

## Deferred

P4 routing/fallback-vs-escalation, P5 critic/oracle/visual policy, P6 profiles/
deterministic scaffold and P7 legacy removal are untouched. Four P1/P2
expected failures remain reserved for P4/P5.
