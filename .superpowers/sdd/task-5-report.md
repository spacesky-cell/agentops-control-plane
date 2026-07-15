# Task 5 Report: Local Dashboard Evidence And Request Safety

## Outcome

Implemented the local AgentPermit dashboard safety and evidence workflow.

- Dashboard binding now validates that every resolved address is loopback-only and rejects wildcard/public/unresolvable hosts before binding.
- Dashboard state changes use a per-process cryptographic CSRF token, POST-only approval routes, constant-time token comparison, bounded form bodies, required reviewer/reason fields, and escaped persisted review data.
- Responses include no-store, CSP, frame, referrer, and content-type hardening headers.
- Snapshot evidence is computed from the archive paths recorded in `run_started` and `run_finished` audit events. The bounded comparison helper reports deterministic created/modified/deleted/unchanged counts, text unified diffs, binary/oversized metadata, and structured unavailable reasons for missing/corrupt/unsafe/oversized archives.
- Run pages provide compact summary metrics, non-duplicated approval details, event type filtering, and native `<details>` payload disclosure. Unknown resources return 404.
- The layout is server-rendered standard-library HTML with responsive overflow containment and no animation or gradients.

## TDD Evidence

The new web tests first failed at collection because `agentpermit.snapshot_diff` did not exist. After adding only importable API skeletons, the behavior RED run was 27 failures / 2 baseline passes. Implementation was then driven to 29 focused passing tests.

## Verification

- `python -m pytest tests/test_web.py -q`: 29 passed
- `python -m pytest -q`: 218 passed, 10 skipped (existing Windows symlink privilege skips)
- `python -m compileall -q agentpermit tests`: passed
- `python -m ruff check agentpermit tests`: passed
- `npm test`: 13 passed
- `python -m build`: wheel and sdist built successfully
- `git diff --check`: passed
- Playwright desktop smoke: real run page, snapshot created/modified/deleted evidence, collapsible events, 0 console errors/warnings
- Playwright 390px smoke: `document.documentElement.scrollWidth` 375 against 390px viewport, no page-level horizontal overflow, 0 console errors/warnings
- Playwright approval flow: filled reviewer/reason and approved a pending request; persisted reviewer/reason rendered escaped on return
- Playwright event filter: `event_type=policy_decision` displayed only the selected event type

Temporary browser screenshots and the local server were removed after verification.

## Remaining Risk

The dashboard remains a local single-user interface. The copied workspace backend is not a container or security sandbox; this task does not add remote authentication or multi-user authorization.

## Fix Review

### RED

- `python -m pytest tests/test_web.py -q`: collection failed with `ImportError: cannot import name 'DashboardHTTPServer'`, confirming the bounded-server regression test had no implementation.

### GREEN

- `python -m pytest tests/test_web.py -q`: `36 passed`.
- `python -m pytest -q`: `225 passed, 10 skipped`.
- `python -m ruff check agentpermit tests`: `All checks passed!`.
- `git diff --check`: passed (only Git line-ending normalization warnings).

The fixes validate Host and Origin against the concrete loopback bind authority, resolve hostnames once before binding, return 404 for unknown approvals before form validation, scope submitted values to the matching approval row, render newline-only snapshot changes as explicit metadata, and use a bounded timeout-protected dashboard server.

### Re-review Empty Snapshot Fix

#### RED

- `python -m pytest tests/test_web.py -q`: `2 failed, 36 passed`; both new public `compare_snapshot_archives` regressions raised `TypeError` in `_newline_style` for `None` (created empty file and deleted empty file).

#### GREEN

- `python -m pytest tests/test_web.py -q`: `38 passed`.
- `python -m pytest -q`: `227 passed, 10 skipped`.
- `python -m ruff check agentpermit tests`: `All checks passed!`.
- `git diff --check`: passed (only Git line-ending normalization warnings).
