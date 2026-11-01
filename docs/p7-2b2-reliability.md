# P7.2b.2 Critical Reliability Hardening

Audited actual HEAD `9b05b37` (the reported `2b10c12` was no longer HEAD; the
existing P7.2c work was preserved untouched). Working tree started clean.

> P7.2b.3 follow-up: the V01 accounting model was extended (uncertain/overrun
> exposure, idempotent state transitions) and V06 visual hashes were versioned
> with legacy compatibility. See `docs/p7-2b3-design.md`.

## V01 — PAYG concurrency and budget integrity

### Concurrency model

- `GrantStore` is **process-local and in-memory**. It is not a multi-process
  budget authority and has no persisted shared budget.
- All grant mutations use a `threading.RLock`.
- Before a PAYG provider call, `Router.reserve()` atomically reserves budget for
  that candidate. The reservation holds the matching grant's remaining amount
  until commit or release.
- `RouteChain` uses the reservation. Concurrent requests competing for a
  max-cost grant cannot both reserve the same remaining budget.
- `commit()` converts the reservation into confirmed spend; `release()` releases
  a confirmed technical failure. Ambiguous failures deliberately leave the
  reservation held so no concurrent caller can reuse uncertain money.
- `GrantStore.charge()` remains lock-protected for compatibility callers.

### Budget invariant

For a grant with `max_cost`, `spent + reserved <= max_cost` is checked while the
store lock is held. Reservations are sized from confirmed model price,
`estimated_context` and `max_tokens`; if a confirmed estimate is unavailable,
the full remaining budget is reserved. This serializes that grant rather than
allowing concurrent authorization races.

### Limitation

The reservation is an estimate, not a provider-enforced token cap. Actual input
usage beyond `estimated_context` can exceed the reserved estimate after the
provider call. True multi-process budget safety is not provided. The historical
`authorize()` boolean API now creates and attaches a reservation to the
candidate, consumed by `charge()` or explicitly released; the execution path
uses `reserve()` directly.

## V02 — Playwright lifecycle

`playwright_audit()` now uses nested `try/finally` blocks:

- context closed if created;
- browser closed if launched;
- cleanup failures are caught and never mask the original navigation/screenshot
  error;
- context creation failure still closes the browser;
- local HTTP server shutdown/close is itself guarded.

Tests cover success, navigation failure, screenshot failure, context-creation
failure, close failure, and original-exception preservation.

## V04 — CircuitBreaker persistence degradation

`CircuitBreaker._save_locked()` keeps authoritative in-memory state and treats
persistence as secondary. `OSError` is caught, a structured warning is emitted
through `crew_tui.router`, `persistence_degraded` is set, and the temporary file
is removed. A later successful save clears the degraded flag and writes the
current in-memory state. Restart may lose updates that failed to persist.

## V05 — Windows atomic replacement retry

`atomic_write_json()` retains write-temp/fsync/validate/atomic-replace.
`_replace_with_retry()` retries only Windows `PermissionError` with up to four
attempts and bounded exponential backoff (0.05–0.25 s). Non-Windows platforms
retry zero times. Other exceptions propagate; temp files are cleaned after
failure.

## V06 — Byte-accurate visual artifact hashing

`VisualService._artifact_hashes()` and Factory visual evidence hash
`path.read_bytes()` with SHA-256. No UTF-8 decoding or replacement occurs.
`state.artifact_hash()` text/LF normalization semantics are unchanged; sign-off
visual freshness now compares raw-byte hashes. Tests cover UTF-8, invalid UTF-8,
binary, empty and byte-distinct inputs.

## Tests

New `tests/test_p72b2_reliability.py` covers 20 focused cases across all five
findings, including deterministic barriers and filesystem fault injection.
Full local suite: 470 tests passed.
