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

## Second Re-review Fix

### Findings And Owner Corrections

- MCP input previously used `for line in input_stream`, which materialized an
  unbounded newline-delimited frame before checking `max_mcp_frame_bytes`.
  `serve_json_lines` now consumes only bounded `readline(size)` chunks. The frame
  scanner counts exact UTF-8 bytes, retains at most one allowed frame plus one
  bounded chunk, drains an oversized frame in bounded calls, and resumes at the
  next frame. Oversized terminated and unterminated frames return the existing
  structured JSON-RPC `-32001` error.
- Approval creation and `approval_requested` previously committed before the MCP
  adapter opened a second transaction to pause the run. `AuditStore` now pauses
  inside the gateway's existing lifecycle transaction before a pending result is
  committed. The same transaction owns approval creation/reuse, approval event,
  `run_paused`, and the pending state returned to the adapter.
- Successful terminalization is conditional on the run still being `running`.
  A clean-close attempt based on stale state cannot overwrite a committed
  `waiting_for_approval` row. Failure terminalization remains available for
  rejection, transport, and governed tool failures.
- MCP and scripted adapters no longer perform a second pause. Direct gateway
  callers retain stable pending retries: the lifecycle claim admits an approval
  wait, treats the same pause as idempotent, and atomically resumes an approved
  retry before consumption and execution.

### RED Evidence

```text
python -m pytest tests/test_final_review_fixes.py -q -k "frame_reader or pending_approval_wins or clean_finish_winner"
```

Initial result: **5 failed, 39 deselected in 1.23s**. Three failures raised from
forbidden unbounded stream iteration, one showed a pending MCP response while
the run had become terminal `success`, and one returned a top-level JSON-RPC
error instead of a terminal MCP tool error after the finish-first race.

### GREEN And Regression Evidence

The same focused command produced **5 passed, 39 deselected in 0.80s**. Coverage
includes bounded-read enforcement, an oversized unterminated frame, exact
multibyte UTF-8 byte accounting, oversized-frame drain followed by a valid frame,
and both pending/finish race directions with approval and event truth assertions.

An initial broad run exposed a live-stdio integration issue: bounded `read(n)` on
`TextIOWrapper` waited for `n` characters or EOF, blocking the MCP subprocess
persistence test. The pytest process and its MCP child were terminated, and no
AgentPermit test child was left running. The scanner was corrected to bounded
`readline(size)`, which returns at a newline while retaining the memory bound.
The complete MCP suite then produced **31 passed in 4.11s**.

Broad lifecycle/MCP/agent/security verification:

```text
python -m pytest tests/test_final_review_fixes.py tests/test_mcp_stdio.py tests/test_gateway.py tests/test_agents.py tests/test_approval_security.py -q -rs
```

Result: **151 passed, 10 skipped in 17.77s**.

Full verification:

```text
python -m pytest --cov=agentpermit --cov-report=term-missing --cov-fail-under=90 -q
npm test
python -m ruff format --check agentpermit tests scripts
python -m ruff check agentpermit tests scripts
python -m mypy --no-incremental agentpermit
python -m pytest tests/test_workflows.py -q
python -c "import yaml; [yaml.safe_load(open(p, encoding='utf-8')) for p in ['.github/workflows/ci.yml','.github/workflows/release.yml']]"
go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.9
git diff --check
```

Results: **328 passed, 10 skipped, 90.01% coverage**; npm **16 passed**;
Ruff format/check and mypy clean; workflow tests **6 passed**; workflow YAML,
actionlint, and diff checks exited zero. The existing ten platform skips and
pytest-asyncio loop-scope warning remain unchanged.
