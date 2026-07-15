# Changelog

## 0.2.0 - 2026-07-09

### Features
- Add an optional Claude Code plan adapter that asks `claude -p --tools=` for JSON tool calls while keeping execution inside the AgentPermit control plane.
- Add `run-claude-code-plan` CLI support with configurable Claude Code command path and timeout.
- Reuse the existing runtime gateway so Claude Code generated plans still go through policy checks, approval gates, isolated workspaces, snapshots, and audit logs.

### Documentation
- Document Claude Code adapter usage, Windows executable path overrides, and Claude Code account/provider failure handling.

## 0.1.0 - 2026-07-09

### Features
- Initial AgentPermit MVP with isolated workspaces, policy-based tool execution, approval gates, audit logs, snapshots, report export, MCP-style plan execution, MCP stdio transport, deterministic evals, and CI.
