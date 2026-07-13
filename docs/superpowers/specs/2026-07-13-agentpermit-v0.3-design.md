# AgentPermit v0.3 Design

## Goal

Turn the current Python portfolio MVP into AgentPermit: a local, single-user, npm-distributed MCP gateway that governs agent tool execution with enforceable policy, atomic approvals, bounded local execution, auditable evidence, and an operational dashboard.

## Product Boundary

- Product, npm package, Python module, CLI, repository, and runtime directory become `AgentPermit`, `agentpermit`, and `.agentpermit`.
- This is a clean pre-1.0 rename. No `agentops` CLI or `agentops_control_plane` import compatibility remains.
- The supported deployment is a loopback-only local developer tool. The copied workspace backend is not a container or security sandbox.
- Claude Code, Codex, and other agents integrate through standard MCP. The one-shot Claude JSON-plan adapter and private MCP lifecycle methods are removed.
- Docker isolation, multi-user authentication, remote deployment, and PyPI publishing are outside v0.3.0.

## Distribution

The unscoped npm package `agentpermit` is the primary distribution. It has no JavaScript runtime dependencies and no install scripts. `bin/agentpermit.js` locates Python in this order: `AGENTPERMIT_PYTHON`, Windows `py -3`, `python3`, then `python`; it requires Python 3.10+, prepends the bundled package root to `PYTHONPATH`, spawns `python -m agentpermit`, inherits stdio, forwards termination, and exits with the child status.

The npm tarball includes the Node launcher, Python package, README, and LICENSE. The Python wheel and sdist remain buildable and are attached to GitHub Releases, but v0.3.0 is not published to PyPI. Package and Python versions are checked for equality in CI and bumped together only in the release commit.

## Runtime And Policy

`run_command` accepts structured arguments:

```json
{"program": "python", "args": ["-m", "pytest", "-q"]}
```

Policy command allow and deny rules are argv-prefix arrays. Execution always uses `shell=False`, a minimal environment without inherited credential variables, bounded streaming output, and process-group termination on timeout. The local backend still has host filesystem and network capability, which is stated explicitly in security documentation.

`PolicyConfig.protected_globs` is the source of truth for paths excluded from source copy, workspace listing, snapshots, and file tools. Secret-shaped keys and known credential formats are recursively redacted from every durable event. Read, write-content, and command-output events retain metadata and hashes rather than raw content.

## Approval State Machine

Approvals store request fingerprints in a dedicated indexed column. For a request requiring approval, the gateway atomically follows this order:

1. A rejected matching approval returns denied.
2. An approved matching approval is changed to `consumed` with a conditional update; only the winner executes.
3. A pending matching approval returns the existing approval id.
4. Otherwise one pending approval is created; concurrent inserts converge on the same active row.

Auto-approval is a server-side execution mode only. It creates an auditable decision and consumes it before execution. MCP request parameters cannot enable it.

## Standard MCP Flow

The public command is:

```text
agentpermit --home <path> --policy <file> mcp --source <repo> --task <text> [--auto-approve]
```

The stdio server exposes standard initialize, notification, ping, tools, empty resources, and empty prompts methods. It lazily creates one run on the first `tools/call`; no custom `run.start`, `tool.call`, or `run.finish` methods remain. A pending approval is returned with a stable id. After a reviewer approves it through CLI or dashboard, retrying the identical tool call consumes the approval and executes it.

## Dashboard

The dashboard remains standard-library, server-rendered HTML and binds only to loopback. State-changing forms use a per-process CSRF token and capture reviewer/reason. The run view shows bounded real before/after diffs, summary metrics, filtered/collapsible events, and non-duplicated approval information. Large or binary files are reported as metadata rather than rendered inline.

## Release

Implementation occurs on `codex/agentpermit-v0.3`, is pushed as a PR, and must pass CI before squash merge. After merge the GitHub repository is renamed to `spacesky-cell/agentpermit`, the remote is updated, and a release commit bumps every version to `0.3.0` and updates the changelog. Tag `v0.3.0` publishes npm and creates a GitHub Release with npm tarball, wheel, sdist, and checksums.

The exposed npm token and GitHub PAT must be revoked. The first npm publish uses a new short-lived token stored only in the protected GitHub `npm` environment; it is revoked immediately after publication, then npm Trusted Publishing is configured for future releases.
