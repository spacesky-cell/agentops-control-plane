# Architecture

AgentOps Control Plane is intentionally a control layer, not a model provider
or IDE clone. Its job is to receive proposed tool actions from agents, decide
whether the action is safe, execute it in an isolated workspace, and keep a
complete audit trail.

## Components

```text
Scripted/OpenAI/Claude/LangGraph AgentAdapter
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

`McpPlanAdapter` is a local MCP-style adapter skeleton. It reads a JSON plan of
tool calls shaped as `{name, arguments}` entries and sends them through the
same gateway. It is not a full MCP server implementation; it establishes the
adapter-side contract that a real MCP transport can later feed. It also follows
the same approval resume pattern as `ScriptedAgent`: the next pending tool call
must match an approved request fingerprint before it can execute.

`serve-mcp-stdio` exposes a newline-delimited JSON-RPC transport with MCP
`initialize`, `notifications/initialized`, `tools/list`, `tools/call`,
`resources/list`, and `prompts/list` methods. It is useful for exercising the
gateway through a process boundary while giving MCP-style clients the same
governed tool surface and argument schemas as the local adapters. The transport
also keeps `run.start`, `tool.call`, and `run.finish` compatibility methods for
deterministic scripts that do not need the MCP session lifecycle.

## PolicyEngine

The current policy engine is JSON-configured and deterministic. It supports:

- command allowlists
- dangerous command deny patterns
- shell control token denial before command execution
- protected file patterns
- approval requirements for writes and patches
- max command runtime and output size

This can later be extended with organization, repository, branch, user, or
environment-aware policies.

## WorkspaceManager

Every run gets a copied workspace under `.agentops/workspaces/<run_id>`.
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

Commands are parsed into argv and executed with `shell=False`. The policy layer
rejects shell control tokens such as `&&`, `|`, redirection, and newlines before
the executor runs the command.

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

1. The run pauses with status `waiting_for_approval`.
2. A pending approval row is created.
3. A reviewer approves or rejects it.
4. `resume-script` matches the approval to the pending request fingerprint.
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
- Add Claude Code/Codex CLI adapter.
- Add OpenTelemetry export.
- Add multi-run evaluation dashboards.
- Add richer diff rendering and branch/PR integrations.

