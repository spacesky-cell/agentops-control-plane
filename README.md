# AgentOps Control Plane

[中文文档](README_CN.md)

AgentOps Control Plane is a small, vendor-neutral runtime gateway for AI agents.
It does not try to replace Codex, Claude Code, LangGraph, or the OpenAI Agents SDK.
It sits above agent backends and governs tool execution with policy, approval,
audit logs, isolated workspaces, snapshots, and run reports.

## Why This Exists

Most agent demos show an LLM calling tools. Production agent systems need a
control layer:

- Which commands can the agent run?
- Which files can it read or write?
- Which actions require approval?
- What exactly did the agent do?
- Can we replay or audit a run after something goes wrong?
- Can multiple agent backends use one shared governance layer?

This project is a portfolio-grade MVP for those concerns.

## Features

- Isolated per-run workspaces copied from a source directory.
- Policy engine for file, patch, and shell command tool calls.
- Human-approval queue with an auto-approval mode for demos.
- SQLite audit log for runs, events, decisions, approvals, and schema version.
- Workspace snapshots before and after execution.
- Scripted agent adapter for deterministic demos and tests.
- Local MCP-style tool-call plan adapter for exercising the same gateway contract.
- JSON-lines stdio transport with MCP-compatible initialization, `tools/list`, and `tools/call` methods.
- HTML/JSON run export.
- Small local web UI for browsing runs, traces, approvals, and patch diffs.
- Standard-library implementation; no runtime dependencies.

## Quick Start

```powershell
git clone https://github.com/spacesky-cell/agentops-control-plane.git
cd agentops-control-plane
python -m agentops_control_plane run-script `
  --plan examples\scripted_fix_agent.json `
  --source examples\sample_repo `
  --auto-approve
```

Runtime data is stored under `.agentops/` in the current directory by default.
Use `--home <path>` before the subcommand to store runs, workspaces, snapshots,
and the SQLite audit database under a different project home:

```powershell
python -m agentops_control_plane --home .demo run-script `
  --plan examples\scripted_fix_agent.json `
  --source examples\sample_repo `
  --auto-approve
```

Run a local MCP-style tool-call plan through the same governance gateway:

```powershell
python -m agentops_control_plane run-mcp-plan `
  --plan examples\mcp_tool_plan.json `
  --source examples\sample_repo `
  --auto-approve
```

MCP-style plans also support the same approval/resume flow:

```powershell
python -m agentops_control_plane approve <approval_id> --approver reviewer
python -m agentops_control_plane resume-mcp-plan <run_id> `
  --plan examples\mcp_tool_plan.json `
  --approver reviewer
```

Serve the thin JSON-lines stdio transport:

```powershell
python -m agentops_control_plane serve-mcp-stdio
```

It accepts newline-delimited JSON-RPC requests. Standard MCP-style clients
should call `initialize`, send `notifications/initialized`, then use
`tools/list` and `tools/call`. `ping` is available as a basic health check.
`tools/list` returns the available tool definitions and input schemas.
`tools/call` wraps tool output as MCP-style content blocks with `isError`.

The transport also supports local run lifecycle methods: `run.start`,
`run.finish`, and the older `tool.call` response shape for deterministic scripts
that do not need MCP initialization.

List runs:

```powershell
python -m agentops_control_plane runs
```

Show a run:

```powershell
python -m agentops_control_plane show <run_id>
```

Resume a run after approving a pending action:

```powershell
python -m agentops_control_plane approve <approval_id> --approver reviewer
python -m agentops_control_plane resume-script <run_id> `
  --plan examples\scripted_fix_agent.json `
  --approver reviewer
```

Export a report:

```powershell
python -m agentops_control_plane export <run_id> --format html --out report.html
```

Serve the local dashboard:

```powershell
python -m agentops_control_plane serve --port 8765
```

The dashboard lists runs, traces, approval requests, and patch diffs. Pending
approval requests can be approved or rejected from `/approvals` or from an
individual run detail page; decisions are written back to the same SQLite audit
store used by the CLI.

## Demo Scenario

The example agent fixes a bug in `examples/sample_repo/math_utils.py`.
It runs inside a copied workspace, not against the original source directory.
The write operation is a medium-risk action, so it requires approval unless
`--auto-approve` is used.

Approvals are bound to a fingerprint of the requested tool action. When a
pre-approved action is executed during resume, that approval is marked
`consumed` so it cannot be reused for a different pending action.

Read-file and command-output audit payloads are summarized with a content
preview and character count. Tool callers still receive the full output, but
the audit database avoids storing complete file contents or long command logs.

## Public Repository Hygiene

This repository intentionally includes source code, documentation, examples, and
the `tests/` suite. Test source files are kept because they make the project
verifiable for reviewers and recruiters.

Generated runtime data is not committed. `.agentops/`, `.pytest_cache/`,
`__pycache__/`, exported demo reports, local environment files, and logs are
ignored by `.gitignore`.

## Project Shape

```text
Agent backend -> Gateway -> Policy -> Tools -> Isolated workspace
                          -> Audit store
                          -> Approval queue
                          -> Snapshots/reports
```

## Resume-Ready Summary

Built a vendor-neutral AgentOps control plane for AI agents with isolated
workspaces, policy-based tool execution, approval gates, command/file audit
logs, snapshots, trace export, and deterministic evaluation demos.
