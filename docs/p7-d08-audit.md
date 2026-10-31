# D08 visual and browser differential audit

Audited at `83da39e` before D08 changes.

## Factory visual execution path

`FactoryAdapter.step("VISUAL")` calls `Factory.step_visual()`, which uses
`Factory.playwright_audit()` (Playwright headless Chromium), captures
`logs/ui_screenshot.png`, calls the vision route, may run one visual fix and
re-audit, stores `artifact_hashes`, persists `state.visual`, and returns a
boolean gate.

## Native visual execution path

`NativeAdapter.step("VISUAL")` calls `VisualService.step_visual()`, which uses
`visual.playwright_audit()` with the same browser lifecycle and vision route,
stores `artifact_hashes` and `project.visual`, and returns the same gate.
Both routes use `RouteRequest(role="visual", required_capabilities={"vision"})`.

## Shared VisualService contracts

- Browser: Playwright headless Chromium, viewport 1024x768, wait 1500ms.
- Local static server on 127.0.0.1; no external network.
- Screenshot under the adapter's isolated artifact/log directory.
- Audit fails on page errors, console errors, failed requests, HTTP errors or
  DOM contract mismatches.
- Vision is mandatory when a screenshot exists: unavailable vision is
  UNVERIFIED, never PASS.
- Visual FAIL may trigger one fix and re-audit/review.
- Visual evidence includes artifact hashes for web files.

## Vision model routing and billing contract

The Router selects `VISUAL_CHAIN` for role/capability `vision`; candidate
identities must declare `vision`. Text-only models are skipped. Billing policy
is unchanged; the test registry marks Gemini Flash/Pro as vision-capable and
non-PAYG.

## Required vs optional visual profiles

`web` includes VISUAL. Missing `index.html` currently yields N/A; Playwright
unavailable yields UNVERIFIED and blocks unless explicitly allowed; a failed
audit blocks. The D08 harness must verify required-profile blocking and
adapter equivalence.

## Existing failure and recovery behaviour

- D10 has adapter-level visual artifact-hash binding and interruption tests.
- Visual service unit tests cover unavailable vision, visual retry and N/A.
- There was no adapter-level real-browser success/failure matrix, no real
  Playwright screenshot comparison and no vision-capability routing comparison
  through both adapters.

## Missing adapter-level comparisons

- Real browser launch, local app serving, screenshot and vision PASS.
- Screenshot dimensions/non-empty evidence and artifact hash association.
- Persisted visual evidence reload with no extra vision call.
- Playwright unavailable, browser launch failure, application/page errors and
  navigation failure.
- Vision-capable selection, vision billing/call count, no-vision UNVERIFIED.
- Visual FAIL -> fix -> re-audit -> PASS.
- Vision timeout/error and stale visual evidence.

## Resolution status

Implemented in `D08VisualBrowserTests` with real Playwright Chromium when
available, deterministic local HTML, fake provider transport only, and both
adapters through `WorkflowEngine`.


## Final D08 evidence

- Real Playwright Chromium success through both adapters, with a local static
  application, non-empty screenshots and identical screenshot bytes/dimensions.
- Vision routing selected `gemini-3.5-flash`, a vision-capable free/subscription
  identity; exactly one vision call on success.
- Visual evidence and artifact hashes survived reload; an approved resume
  reached DONE without another vision call.
- Playwright unavailable, launch failure, application page error, navigation
  failure, no vision model and vision timeout all blocked the visual gate.
- Visual FAIL -> fix -> re-audit -> PASS preserved the retry evidence and final
  artifact hash.
- Changed visual artifacts invalidated approval and blocked sign-off.
