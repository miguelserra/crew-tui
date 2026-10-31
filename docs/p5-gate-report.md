# P5 gate report — verification engine and delivery gate

Reference:

- P4 security fix — `bd7dc1a` (PAYG grants keyed by full identity)
- P5 code — `588516c`, plus evidence retention `077c856`
- `p5-gate` — tag on this report commit
- CI — `41150644041`: **success** (Linux 3.10/3.12/3.13, Windows 3.13,
  integration Linux+Windows)

Scope: P5 only. No P6 profiles/scaffold change and no P7 legacy removal.

## Verification engine

`verify.py` defines `PASS`, `FAIL`, `UNVERIFIED` and `N/A`, critic validity and
the delivery policy. `main.Verifier` collects oracle/critic/visual/test
evidence; Orchestrator and Factory both call the same gate.

Evidence is stored in `VerificationRecord`:

- oracle result;
- critic identity, family, verdict and objections;
- visual verdict and vision identity (when applicable);
- advisory local test result;
- final verdict, reasons and timestamp.

## Critic independence

- Producer identity comes from `Task.delivered_by`, then the last generation
  attempt, then `LLMPool.last_attempt_model`. Verifier calls (`CRITIC`,
  `VISUAL`, `READER`, `ORACLE`, `TEST`) are explicitly excluded from task
  attempts/delivered_by.
- `LLMPool.critic_candidates` only returns identities whose family differs from
  the effective producer; the same rule is rechecked in `run_critic` for every
  candidate.
- A technical fallback of a critic stays cross-family; same-family candidates
  are never invoked as fallbacks.
- The fixer's effective identity (e.g. Big Pickle) drives the critic family.

## UNVERIFIED and delivery

- Critic unavailable/invalid, same-family critic, producer identity unknown and
  visual `UNVERIFIED` produce `UNVERIFIED`.
- A build is delivered only when the gate returns PASS, or when
  `ALLOW_UNVERIFIED=1`/`--allow-unverified` is explicitly set; the exception is
  recorded in the verification evidence and result (`unverified_override`).
- `UNVERIFIED` never triggers escalation; escalation is only for quality FAIL.
- A review report may be delivered with documented objections if the critic
  actually ran; the code verification verdict remains FAIL, not PASS.
- Factory `_build_one` now runs the critic gate *before* installing a file.
  Factory/Orchestrator visual review uses only Registry `vision` capabilities.

## N/A vs UNVERIFIED

- No HTML deliverable or visual check disabled → `N/A`.
- HTML present but no vision model/screenshot/valid visual verdict →
  `UNVERIFIED`.
- Text-only identities are filtered by the Router; images are never sent to
  them.

## Local Qwen test

`run_test_oracle` and the Factory unit test are marked `advisory=True`.
Failures can inform a fix but do not block delivery by themselves. Existing
deterministic evidence (oracle) and the critic remain mandatory.

## P4 correction

PAYG grants now use a full `identity_key`:
`provider|endpoint|credential_profile|model_id`. A grant for
`deepseek-v4-pro` on one endpoint cannot authorize the same model id on another
endpoint; a dedicated test covers this. The residual dead code in
`Router._chain_for` was removed.

## Exit gate evidence

| Gate | Evidence |
|---|---|
| No build delivered without checks | Orchestrator `_check_artifact`; Factory `_build_one` critic gate before `_install` |
| Critic always cross-family | `critic_candidates` + `run_critic`; P1/P5 integration tests |
| Effective producer identified | delivered_by/attempts/last generation attempt; verifier tags excluded |
| Critic unavailable ≠ PASS | `verify.decide_build`; P1 integration; P5 tests |
| Qwen cannot block alone | advisory test result; `decide_build` |
| Text-only never gets images | Router visual capability filter; `run_visual` |
| N/A vs UNVERIFIED | `verify` tests; `run_visual`; Factory visual |
| Review report with objections | `decide_review`; review path |
| Escalation only quality | `_execute` UNVERIFIED branch skips `_escalate` |
| Evidence preserved | `VerificationRecord` in TaskSpec/TaskState/ProjectState/results |
| Linux + Windows | CI `41150644041`: success |

## Tests

| Suite | Result |
|---|---|
| P1 core | 80 tests, OK (13 skips) |
| P2 core | 34 tests, OK (2 skips) |
| P3 core | 18 tests, OK (7 skips) |
| P4 core | 23 tests, OK |
| P5 core | 14 tests, OK (4 integration skips without deps) |
| Integration (P1/P2/P3/P5) | 26 tests, OK |
| power + budget | OK |
| `py_compile` / `git diff --check` | OK |

The three previous expected failures were promoted: cross-family critic,
critic-unavailable blocking and advisory local tests are now normal tests.

## Smoke (fake OpenAI-compatible server, no external keys)

Unified Factory run: `SPEC → ARCHITECTURE gate → SCAFFOLD → BUILD (critic
gate) → INTEGRATE → SIGN-OFF`. Evidence showed task `t1` PASS with criterion
`ac1`, producer family `gemini`, critic family `kimi`, and one generation
attempt (critic calls excluded from delivered_by).

## Deferred

P6 profiles/deterministic scaffold and P7/P8 legacy/TUI cleanup remain
untouched. The Factory still installs project files during BUILD before the
project-level VISUAL step; P6/P7 delivery restructuring is out of scope here.
