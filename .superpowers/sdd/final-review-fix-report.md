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

## CI Fix

### Hosted RED Evidence

GitHub Actions run `29392747696` reported two Linux-specific failure groups:

- `mypy` produced **26 `attr-defined` errors** for Windows-only APIs exposed by
  Linux typeshed: `ctypes.WinDLL`, `ctypes.WinError`,
  `ctypes.get_last_error`, `subprocess.CREATE_NEW_PROCESS_GROUP`, and
  `msvcrt.get_osfhandle`.
- Linux pytest produced **7 failures** covering cross-platform colon path
  semantics, earlier fail-closed source identity detection, the stronger
  workspace-root integrity message during POSIX cleanup, and the intentionally
  fd-backed POSIX `HOME` value.

The typing RED was reproduced locally with:

```text
python -m mypy --no-incremental --platform linux agentpermit
```

Result before the fix: **26 errors in 2 files** (`workspace.py` and `tools.py`),
matching the hosted job.

### Corrections

- Module-local `Any` aliases now isolate only the Windows ctypes/subprocess/
  msvcrt API boundaries. Runtime `os.name` branches and the global strict mypy
  configuration are unchanged.
- `_relative_parts` rejects a colon in any component on every OS. The existing
  ADS/stream regression was renamed to state its cross-platform contract and
  continues to cover protected and ordinary filenames.
- The source ancestor replacement test accepts only the earlier
  `WorkspaceIntegrityError` fail-closed path or the leased successful copy, and
  always proves that replacement secret content was not copied.
- The nested POSIX creation cleanup test asserts the common
  `changed during access` integrity invariant, preserving both moved-root and
  replacement-root cleanup assertions.
- The minimal-environment subprocess test proves `HOME` resolves to and is the
  same file as the command cwd. Windows retains the literal workspace-path
  assertion; POSIX retains the intentional `/proc/self/fd` or `/dev/fd` lease
  path. Credential non-inheritance assertions are unchanged.

### Local GREEN Evidence

```text
python -m mypy --no-incremental --platform linux agentpermit
python -m mypy --no-incremental agentpermit
```

Both commands report **Success: no issues found in 17 source files**.

Focused locally runnable checks:

```text
python -m pytest tests/test_approval_security.py -q -k "colon_components or source_copy_holds_ancestor_lease or nested_create_removes" -rs
python -m pytest tests/test_command_execution.py::test_run_command_uses_minimal_environment_without_credentials -q
```

Result: **6 passed, 1 POSIX-only skipped, 51 deselected**. The skipped nested
descriptor cleanup and Linux-specific source replacement ordering are left to
the hosted Linux rerun, as required; their assertions were checked against the
current owner code paths without using WSL or Docker.

Full local verification:

```text
python -m pytest --cov=agentpermit --cov-report=term-missing --cov-fail-under=90 -q
npm test
python -m ruff format --check agentpermit tests scripts
python -m ruff check agentpermit tests scripts
python -m mypy --no-incremental --platform linux agentpermit
python -m mypy --no-incremental agentpermit
python -m pytest tests/test_workflows.py -q
python -c "import yaml; [yaml.safe_load(open(p, encoding='utf-8')) for p in ['.github/workflows/ci.yml','.github/workflows/release.yml']]"
go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.9
git diff --check
```

Results: **328 passed, 10 skipped, 90.02% coverage**; npm **16 passed**;
Ruff format/check clean; both mypy platforms clean; workflow tests **6 passed**;
workflow YAML and actionlint exited zero. The final diff check is rerun after
this report update and before commit.

### Remaining CI Concern

Hosted Linux is still the authoritative execution evidence for the two
POSIX-only filesystem race/cleanup assertions. No push or workflow rerun was
performed in this fix wave.

## Cross-Platform Coverage CI Fix

### Hosted RED Evidence

The follow-up hosted Linux run completed the full behavior suite with **328
passed, 10 skipped**, including the POSIX-only assertions, but failed its
Linux-only coverage gate at **87.05%**. The same full suite reports **90.02%**
on Windows because the platform-specific Windows runtime paths are legitimately
unreachable on Linux. Lowering the threshold or excluding platform code would
weaken the repository-wide coverage contract instead of measuring it correctly.

### Corrections

- Coverage now records source paths relative to the repository through
  `[tool.coverage.run] relative_files = true`.
- The Ubuntu `quality` job retains Ruff and mypy ownership, then runs the full
  behavior suite with `coverage run --parallel-mode`. The Windows security job
  runs the same full behavior suite through the same coverage command.
- Each platform uploads its hidden `.coverage.*` file under a unique artifact
  name using the reviewed immutable `upload-artifact` commit, explicit hidden
  file inclusion, missing-file failure, and one-day retention.
- A dedicated `coverage` job depends on both platform producers, downloads both
  artifacts with the reviewed immutable `download-artifact` commit and
  `pattern`/`merge-multiple`, combines them, and exclusively enforces
  `coverage report --fail-under=90`.
- The release workflow still runs the complete Python behavior suite, but no
  longer applies an invalid Linux-only repository coverage threshold. Required
  CI owns the cross-platform threshold before release.
- CI pushes are limited to `main`; pull requests and manual dispatch remain.
  This removes duplicate feature-branch push and pull-request matrices.
- Workflow contract tests cover producer/consumer topology, immutable hidden
  artifact transfer, relative source paths, the retained 90% threshold,
  release delegation, and trigger scope.

### RED/GREEN And Local Verification

The new workflow tests initially produced **4 failures, 5 passes** for the
missing producer commands, aggregation job, relative path setting, release
delegation, and trigger restriction. After the workflow changes:

```text
python -m pytest tests/test_workflows.py -q
```

Result: **9 passed**.

The exact producer/combine/report command path was simulated locally with one
full Windows parallel coverage data file copied into two artifact-shaped hidden
files. `coverage combine coverage-data` combined the first and safely detected
the duplicate second file; `coverage report --fail-under=90` then passed at
**90.02%**. The full producer run reported **331 passed, 10 skipped**.

Additional verification:

```text
npm test
python -m ruff format --check agentpermit tests scripts
python -m ruff check agentpermit tests scripts
python -m mypy --no-incremental agentpermit
python -m mypy --no-incremental --platform linux agentpermit
python -c "import yaml; [yaml.safe_load(open(p, encoding='utf-8')) for p in ['.github/workflows/ci.yml','.github/workflows/release.yml']]"
go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.9
git diff --check
```

Results: npm **16 passed**; Ruff format/check clean; both mypy platform models
report no issues in 17 source files; workflow YAML and actionlint exit zero.
The final diff check is rerun after this report update and before commit.

### Remaining CI Concern

The local combine simulation validates artifact discovery and coverage command
compatibility, but hosted CI remains authoritative for the genuine Linux and
Windows data merge and the resulting repository-wide percentage. No workflow
run was triggered and no push, merge, or publication was performed.
