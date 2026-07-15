# AgentPermit v0.3.0 Final Review Fix Report

## Status

DONE_WITH_CONCERNS. Every Important and Minor finding in
`final-review-findings.md` is implemented and locally verified. The remaining
concerns are unchanged platform skips and the existing pytest-asyncio warning,
listed below.

## Owner Design

- `AuditStore`/SQLite is the lifecycle truth source. A `BEGIN IMMEDIATE` claim
  spans terminal-state validation, approval resolution/consumption, actual tool
  execution, and durable tool outcome. Every write in that scope reuses the same
  SQLite connection.
- Terminalization acquires the same SQLite claim before creating its final
  snapshot. If a tool already owns the claim, terminalization waits for its
  durable result. If terminalization owns the claim, later tool attempts and
  approval decisions reject after the terminal commit.
- Terminalization atomically changes pending or approved approvals to
  `cancelled`. Direct `AuditStore.resolve_approval()` and `decide_approval()`
  reject terminal owners.
- `PolicyConfig` owns validated MCP frame, tool argument, per-file, and copied
  source aggregate limits. Gateway, MCP, and WorkspaceManager remain adapters or
  enforcement owners at their existing boundaries.
- Command executor output remains bounded. Gateway classifies timeout or a
  non-zero exit as `tool_failed`/`ToolStatus.FAILED`, retains exit/timeout/
  truncation metadata in redacted durable evidence, and returns the bounded
  command result through the MCP error payload.

## TDD RED Evidence

The first test collection attempt failed because the wished-for
`ResourceLimitError` symbol did not exist. The test harness was corrected to
assert the public `ValueError` behavior before recording RED; no production code
was changed for that collection error.

Valid initial RED command:

```text
python -m pytest tests/test_final_review_fixes.py tests/test_workflows.py -q
```

Result: **36 failed, 5 passed in 4.64s**. Expected failures covered terminal
execution/resume, decisions on terminal approvals, cancellation, both lifecycle
race directions, unsuccessful command classification, MCP command errors, all
four missing policy limits, MCP frames/arguments, file operations, source copy,
snapshots, and the missing Windows Python CI job.

An owner API review found one additional case after the initial GREEN: direct
approval resolution could create a new approval for a terminal run.

```text
python -m pytest tests/test_final_review_fixes.py::test_audit_store_cannot_create_or_consume_approval_for_terminal_run -q
```

Result: **1 failed** with `DID NOT RAISE ApprovalStateConflictError`. After the
owner-layer check was added, the same command produced **1 passed**.

## GREEN Evidence

Initial focused GREEN:

```text
python -m pytest tests/test_final_review_fixes.py tests/test_workflows.py -q
```

Result: **41 passed in 2.23s**.

The first broad compatibility run found three stale expectations: the old
non-pending message, a post-rejection gateway retry that now must reject the
terminal run, and a same-thread nested writer race harness incompatible with the
approved SQLite claim. The tests were updated to the stronger winner semantics.
The rerun was **191 passed, 10 skipped**.

Final focused behavior command:

```text
python -m pytest tests/test_final_review_fixes.py tests/test_gateway.py tests/test_approval_security.py tests/test_mcp_stdio.py tests/test_command_execution.py tests/test_policy.py -q -rs
```

Result: **195 passed, 10 skipped in 23.96s**. The new focused module itself is
**39 passed**, including real gateway non-zero and MCP timeout subprocess cases.
No concurrency test deadlocked or timed out.

## Full Verification

Full Python coverage:

```text
python -m pytest --cov=agentpermit --cov-report=term-missing --cov-fail-under=90 -q
```

The first full behavior run was **320 passed, 10 skipped**, but correctly failed
the coverage gate at **89.77%**. Focused tests were added for the newly introduced
lifecycle branches; the gate was not lowered. Final result: **323 passed,
10 skipped in 30.96s**, **90.10%** statement coverage.

Node tests:

```text
npm test
```

Result: **16 passed, 0 failed**.

Formatting, lint, and typing:

```text
python -m ruff format --check agentpermit tests scripts
python -m ruff check agentpermit tests scripts
python -m mypy --no-incremental agentpermit
```

Results: **32 files already formatted**, **All checks passed**, and **Success: no
issues found in 17 source files**.

Workflow verification:

```text
python -m pytest tests/test_workflows.py -q
python -c "import yaml; [yaml.safe_load(open(p, encoding='utf-8')) for p in ['.github/workflows/ci.yml','.github/workflows/release.yml']]"
go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.9
```

Results: **6 passed**, both workflow files parsed, and actionlint exited zero with
no findings. CI now runs the complete Python suite on `windows-latest`.

Configuration/document checks:

```text
python -m json.tool examples/policy.json
git diff --check
```

Results: example policy parsed successfully and `git diff --check` exited zero.

## Documentation And Minor Findings

- Both README approval prose examples now include `<approval_id>`.
- README, README_CN, SECURITY, architecture, MCP, and the example policy document
  the four bounded input keys and defaults.
- `task-7-report.md` headline totals now say 283 Python passed / 10 skipped and
  16 npm tests. It states that npm publication has only `id-token: write`, with
  unspecified permissions disabled, and includes the exact actionlint command.
- `.github/workflows/ci.yml` includes the required Windows Python job.

## Remaining Concerns

- Ten tests are skipped locally: four require symlink privileges unavailable to
  this Windows account and six exercise POSIX descriptor/promotion paths. Existing
  Linux CI remains the platform evidence for those paths.
- pytest emits the pre-existing warning that
  `asyncio_default_fixture_loop_scope` is unset. It does not affect these tests.
- The lifecycle claim intentionally serializes SQLite writers while a bounded
  governed tool executes. This is the conservative correctness tradeoff for the
  supported local, single-user product boundary.
