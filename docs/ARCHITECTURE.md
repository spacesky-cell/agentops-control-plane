# Architecture

AgentPermit is intentionally a control layer, not a model provider
or IDE clone. Its job is to receive proposed tool actions from agents, decide
whether the action is safe, execute it in an isolated workspace, and keep a
complete audit trail.

## Components

```text
Scripted AgentAdapter or standard MCP client
        |
        v
RuntimeGateway
        |
        +--> PolicyEngine
        |
        +--> Approval Queue
        |
        +--> ToolExecutor
        |       |
        |       +--> Isolated Workspace
        |
        +--> AuditStore
        |
        +--> Snapshots / Reports / Dashboard
```

## RuntimeGateway

The gateway is the boundary that every tool call crosses. It records the
requested action, asks the policy engine for a decision, handles approvals,
executes approved actions, and stores the result.

The important design choice is that agent code does not call tools directly.
This makes the system portable across agent backends.

Runtime data is rooted at the CLI home directory. By default this is the
current working directory, and callers can pass `--home <path>` before the
subcommand to choose a different project home.

## AgentAdapter

Agent backends implement the `AgentAdapter` protocol. An adapter owns the
backend-specific planning loop, but every tool action still crosses the
`RuntimeGateway`. `ScriptedAgent` is the deterministic adapter used for demos,
tests, and resume-flow reference behavior.

`python -m agentpermit mcp --source <repo> --task <text>` exposes a standard
newline-delimited MCP JSON-RPC transport with `initialize`,
`notifications/initialized`, `tools/list`, `tools/call`, `resources/list`, and
`prompts/list`. The session lazily starts one governed run on its first
`tools/call`; clean EOF finalizes success while approval waits remain durable.

## PolicyEngine

The current policy engine is JSON-configured and deterministic. It supports:

- exact argv-prefix command allowlists
- exact argv-prefix command denylists, evaluated before allowlists
- protected file patterns
- approval requirements for writes and patches
- max command runtime and bounded, continuously drained output

This can later be extended with organization, repository, branch, user, or
environment-aware policies.

## WorkspaceManager

Every run gets a copied workspace under `.agentpermit/workspaces/<run_id>`.
Source repositories are never modified directly. The manager also creates
before/after zip snapshots for audit and recovery.

## ToolExecutor

The MVP toolset is deliberately small:

- `list_files`
- `read_file`
- `write_file`
- `patch_text`
- `run_command`

This is enough to demonstrate code-agent workflows while keeping risk and
testability under control.

Commands enter the system as structured `program` and `args` values and execute
with `shell=False`; command strings are not parsed or accepted. The executor uses
a minimal environment, continuously drains bounded output, and terminates the
whole process group when a command times out. Output is decoded as UTF-8 with
replacement characters for invalid byte sequences. The configured deadline
covers both process exit and pipe draining; tree termination and reader cleanup
receive at most one additional second of bounded cleanup grace.

The executor owns raw stdout/stderr pipe descriptors directly; reader threads
incrementally decode each stream and descriptor ownership is closed exactly once.
On Windows, executable paths are strictly canonicalized before launch, batch and
script files are rejected, and logical npm/pnpm commands use `node.exe` with known
JavaScript CLI entrypoints instead of executing command shims.

## AuditStore

The audit store is SQLite-backed and captures:

- runs
- policy decisions
- approval requests and decisions
- agent steps
- tool execution results
- snapshot paths
- schema version metadata

This creates the evidence trail needed for debugging, compliance, and evals.
Read-file and command-output payloads are summarized before they are written to
the audit database. The tool result returned to the caller remains complete.

## Approval Flow

When a policy requires approval:

1. A pending approval row is created for the request.
2. The run pauses atomically with status `waiting_for_approval` while that
   approval remains pending or approved.
3. A reviewer approves or rejects the row.
4. `resume-script` matches the approval to the pending request fingerprint and
   atomically resumes only a still-waiting run.
5. The approval is marked `consumed` when the pre-approved action is executed.
6. The run continues from the pending step.

The resume flow is implemented for the scripted agent adapter and serves as the
reference pattern for other agent backends.

## What This Is Not

This is not trying to be a stronger container sandbox than Docker, E2B, Modal,
Daytona, or OpenAI's sandbox integrations. It is the governance layer above
those execution environments: policy, approval, trace, replay, and reporting.

## Next Production Steps

- Add Docker and cloud sandbox backends.
- Add richer MCP resource and prompt providers.
- Add OpenAI Agents SDK adapter.
- Add additional standard MCP resources and prompts.
- Add OpenTelemetry export.
- Add multi-run evaluation dashboards.
- Add richer diff rendering and branch/PR integrations.

