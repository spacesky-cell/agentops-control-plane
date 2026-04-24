# Architecture

AgentOps Control Plane is intentionally a control layer, not a model provider
or IDE clone. Its job is to receive proposed tool actions from agents, decide
whether the action is safe, execute it in an isolated workspace, and keep a
complete audit trail.

## Components

```text
Scripted/OpenAI/Claude/LangGraph Agent
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

## PolicyEngine

The current policy engine is JSON-configured and deterministic. It supports:

- command allowlists
- dangerous command deny patterns
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

## AuditStore

The audit store is SQLite-backed and captures:

- runs
- policy decisions
- approval requests and decisions
- agent steps
- tool execution results
- snapshot paths

This creates the evidence trail needed for debugging, compliance, and evals.

## Approval Flow

When a policy requires approval:

1. The run pauses with status `waiting_for_approval`.
2. A pending approval row is created.
3. A reviewer approves or rejects it.
4. `resume-script` continues the run from the pending step.

The resume flow is implemented for the scripted agent adapter and serves as the
reference pattern for other agent backends.

## What This Is Not

This is not trying to be a stronger container sandbox than Docker, E2B, Modal,
Daytona, or OpenAI's sandbox integrations. It is the governance layer above
those execution environments: policy, approval, trace, replay, and reporting.

## Next Production Steps

- Add Docker and cloud sandbox backends.
- Add MCP tool adapter.
- Add OpenAI Agents SDK adapter.
- Add Claude Code/Codex CLI adapter.
- Add OpenTelemetry export.
- Add multi-run evaluation dashboards.
- Add richer diff rendering and branch/PR integrations.

