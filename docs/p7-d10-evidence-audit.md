# D10 evidence freshness audit

Audited at `60b94de` before D11. Question: can a persisted `PASS`
`VerificationRecord` without `artifact_sha256` be accepted as current evidence?

## Result

Yes. `ProjectState.verification_blockers()` and
`stale_verification_tasks()` only compared hashes when `artifact_sha256` was
truthy. A persisted/legacy/migrated `PASS` record without the hash therefore
passed the freshness check when an artifact root existed.

The schema does not require `artifact_sha256`; older v1/v2/v3 states and manual
or external state writers can contain a `PASS` record without it. It is not
impossible by construction.

## Correction

Implemented the smallest fail-closed policy:

- A `PASS` record for a build task with an artifact root and no
  `artifact_sha256` is blocked as `evidência sem artifact hash`.
- The same record is considered stale for integration resume, so integration
  revalidates it and writes a fresh hash-bound record.
- A `PASS` record with a matching hash is accepted.
- A `PASS` record with a mismatching hash remains `evidência desatualizada`.
- A build-task `N/A` record remains explicitly invalid (`verificação inválida`),
  separate from legacy `PASS` evidence.
- Reload and schema/migration round-trips preserve the blocking result; no
  automatic conversion to PASS occurs.
- FactoryAdapter and NativeAdapter share the same state policy and produce the
  same decision.

## Code paths capable of producing PASS

- `executor.py`: BUILD record includes `artifact_sha256`.
- `factory.py`: BUILD record includes `artifact_sha256`.
- `integration.py` / `factory.py`: integration revalidation includes
  `artifact_sha256`.
- Legacy/migrated/manual state can still contain PASS without it; this is the
  explicit legacy policy above.

## Regression tests

- `test_d10_05a_verify_resumes_persisted_evidence`: matching hash accepted.
- `test_d10_05b_verify_rejects_missing_and_stale_evidence`: mismatch rejected.
- `test_d10_05c_legacy_pass_without_artifact_hash_is_fail_closed`: missing hash
  blocks VERIFY, survives reload, and N/A remains separately invalid.
- `test_d10_05d_legacy_pass_without_hash_is_revalidated_on_integrate`:
  integration resume revalidates the legacy record and binds the current hash.
- Both adapters are exercised for every case.
