# Changelog

## 0.3.0 - 2026-07-15

### Features
- Rebrand the project as AgentPermit and ship a dependency-free npm launcher for Node.js 18+ and Python 3.10+.
- Expose a standard MCP stdio server for Claude Code, Codex, and compatible clients.
- Add structured command execution with argv-prefix policy, bounded output, timeouts, and process-tree cleanup.
- Add an operational dashboard with approval review, event filters, and bounded before/after snapshot evidence.

### Security
- Make approval decisions, consumption, run pause, tool execution, and terminalization atomic in SQLite.
- Redact protected payloads before durable storage and exclude protected paths from copies, tools, and snapshots.
- Bound MCP frames, tool arguments, individual files, copied source totals, command output, and dashboard inputs.
- Enforce loopback-only, local single-user deployment and document that copied workspaces are not OS sandboxes.

### Quality
- Test Python 3.10-3.14, Node.js 18/20/22, Windows and Linux launchers, and full Windows security behavior.
- Enforce combined Linux/Windows coverage above 90%, Ruff, mypy, package validation, deterministic evals, and clean installed-artifact MCP smoke tests.
- Add tag-gated npm provenance, release metadata validation, checksums, and retry-safe GitHub Release automation.

### Documentation
- Add English and Chinese adoption guides, MCP configuration, architecture and security boundaries, a dashboard screenshot, contribution guidance, and issue/PR templates.

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
