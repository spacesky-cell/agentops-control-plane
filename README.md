# AgentPermit

AgentPermit is a local, single-user governance gateway for AI agent tool execution. It sits between a standard MCP client and the filesystem or commands an agent wants to use, adding policy decisions, human approvals, bounded execution, snapshots, and an auditable dashboard.

It is designed for a developer workstation. It is not a container, an operating-system sandbox, a multi-user service, or a hosted control plane.

## Features

- Standard MCP stdio integration for Claude Code, Codex, and compatible clients.
- Per-run copied workspaces so the source checkout is not edited directly.
- Structured file and command tools with argv-prefix policy rules and bounded output.
- Atomic approval records with stable request fingerprints and one-time consumption.
- SQLite evidence for runs, decisions, approvals, tool results, and snapshots.
- Before/after dashboard diffs that report created, modified, deleted, binary, and oversized files.
- Loopback-only HTML dashboard with reviewer, reason, and CSRF-protected approval forms.
- A deterministic scripted agent kept for demos and evaluation tests; it is not the public integration path.

![Completed AgentPermit run with created, modified, and deleted snapshot evidence](https://raw.githubusercontent.com/spacesky-cell/agentpermit/main/docs/assets/dashboard-completed-run.png)

## Install

AgentPermit is currently prepared for adoption and is not published to npm yet. From a checkout, build and install the exact npm artifact locally:

```powershell
npm pack
npm install --ignore-scripts .\agentpermit-0.2.0.tgz
npx --no-install agentpermit --help
```

The launcher requires Node.js 18+ and Python 3.10+. The package version remains `0.2.0` until the release task.

## Three-minute quick start

Run the deterministic example from the repository checkout through the installed npm launcher:

```powershell
npx --no-install agentpermit --home .demo run-script `
  --plan examples\scripted_fix_agent.json `
  --source examples\sample_repo `
  --auto-approve
npx --no-install agentpermit --home .demo runs
npx --no-install agentpermit --home .demo serve --port 8765
```

Open <http://127.0.0.1:8765>. The dashboard shows the completed run, policy trace, approval decision, and bounded snapshot evidence. The original `examples\sample_repo` remains unchanged.

For a real MCP session, start the server against a source directory and task:

```powershell
npx --no-install agentpermit --home .demo mcp `
  --source examples\sample_repo `
  --task "Inspect the repository"
```

The first `tools/call` creates the governed run. A policy-gated call returns a stable pending approval id; approve it in the dashboard or with `npx --no-install agentpermit --home .demo approve`, then retry the identical MCP call.

## Standard MCP configuration

The public integration is the standard MCP stdio server. Use the exact command shape supported by each client.

Claude Code, project scope:

```powershell
claude mcp add --scope project agentpermit -- npx --no-install agentpermit --home . mcp --source . --task "Govern this workspace"
```

Codex project configuration in `.codex/config.toml`:

```toml
[mcp_servers.agentpermit]
command = "npx"
args = ["--no-install", "agentpermit", "--home", ".", "mcp", "--source", ".", "--task", "Govern this workspace"]
```

Use an absolute path to the npm executable when the client cannot resolve `npx`. Keep `--auto-approve` out of client configuration; it is a server-process option for a deliberately trusted local demo only.

The wire sequence is `initialize`, `notifications/initialized`, `tools/list`, then `tools/call`. See [docs/MCP_STDIO.md](docs/MCP_STDIO.md).

## Approval and dashboard workflow

Policy decisions are made by the gateway before a tool executes. Writes and patches require approval by default. The dashboard at `http://127.0.0.1:8765` provides:

1. Run status and task metadata.
2. Approval request, exact redacted arguments, reviewer, and reason.
3. Event filters for policy, approval, and tool execution records.
4. Snapshot counts and bounded diffs for created, modified, and deleted files.

CLI alternatives use the same `.demo` home as the run:

```powershell
npx --no-install agentpermit --home .demo approvals --run-id <run_id>
npx --no-install agentpermit --home .demo approve <approval_id> --approver reviewer --reason "Reviewed exact request"
npx --no-install agentpermit --home .demo reject <approval_id> --approver reviewer --reason "Rejected exact request"
npx --no-install agentpermit --home .demo show <run_id>
npx --no-install agentpermit --home .demo export <run_id> --format html --out report.html
```

## Architecture

```text
Claude Code / Codex / MCP client
              |
       standard MCP stdio
              v
        RuntimeGateway
       /       |       \
   Policy   Approval   AuditStore
      |        |          |
  ToolExecutor ---- WorkspaceManager
              |
       snapshots + dashboard
```

The gateway owns governance semantics. The MCP server and scripted agent are adapters only. Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for boundaries and lifecycle details.

## Security limits

AgentPermit binds its dashboard to loopback and is intended for one local user. A copied workspace is an organizational boundary, not a container or OS sandbox. Same-user processes can tamper with local state; allowed commands can access the host filesystem and network according to OS permissions. Redaction and protected globs reduce accidental persistence but are defense in depth, not DLP. Review [SECURITY.md](SECURITY.md) before using it with sensitive repositories.

## Development

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q
python -m build
npm test
npm pack --dry-run
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for changes, tests, and disclosure expectations. The project is MIT licensed; see [LICENSE](LICENSE).

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Standard MCP stdio](docs/MCP_STDIO.md)
- [Deterministic demo](docs/DEMO_SCRIPT.md)
- [Security policy](SECURITY.md)
- [Contributing](CONTRIBUTING.md)
