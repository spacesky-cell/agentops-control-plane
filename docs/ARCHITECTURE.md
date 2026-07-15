# Architecture

AgentPermit is a local governance runtime, not an agent framework or model provider. Standard MCP is the public integration boundary. The gateway owns policy, approvals, execution, and audit truth; adapters only translate protocols or drive deterministic tests.

## Component boundaries

```text
MCP client                Scripted demo/eval adapter
    |                                |
    +---------- adapter layer -------+
                     |
              RuntimeGateway
          /         |          \
 PolicyEngine   AuditStore   WorkspaceManager
                     |          |
                 approvals   ToolExecutor
                     \          /
                  snapshots + dashboard
```

- `mcp_stdio.py` implements standard JSON-RPC/MCP session mechanics and maps tool calls to the gateway.
- `gateway.py` is the single owner of run lifecycle, policy decisions, approval resolution, execution records, and snapshots.
- `policy.py` evaluates deterministic structured requests.
- `audit.py` owns durable SQLite state and atomic approval and run-lifecycle transitions.
- `workspace.py` creates copied run workspaces, enforces protected paths, and produces snapshots.
- `tools.py` implements the bounded file and command tools.
- `web.py` renders the loopback-only operational dashboard from audit and snapshot truth.
- `agents.py` is a deterministic demo/evaluation adapter, not a second public runtime.

## Standard MCP lifecycle

Discovery does not create runtime state. The server validates `initialize`, accepts `notifications/initialized`, and answers `tools/list`, `resources/list`, and `prompts/list`. The first `tools/call` lazily creates one run and copied workspace for the session.

Every call then follows the same owner path:

1. Validate the structured tool request.
2. Evaluate policy and record the decision.
3. Resolve a matching approval by fingerprint when required.
4. Execute only after an allow or atomically consumed approval.
5. Record bounded result metadata and return an MCP content block.
6. On clean EOF, finish a running run as success; preserve waiting approvals durably.

Only the standard MCP methods described in `MCP_STDIO.md` are public.

## Approval state machine

For a request requiring approval, the gateway checks the same request fingerprint in this order:

1. Rejected match: deny the request.
2. Approved match: atomically transition it to `consumed`; only the winner executes.
3. Pending match: return the existing approval id.
4. No active match: create one pending approval; concurrent inserts converge.

Auto-approval is a server execution mode that creates and consumes an auditable decision. MCP request parameters cannot enable it.

Tool execution, approval consumption, and terminalization use the same SQLite write claim. If terminalization wins, later tool attempts and approval decisions reject and active approvals become `cancelled`. If a tool has already claimed the run, its approval transition, execution, and durable outcome finish before terminalization can take the final snapshot or write `run_finished`.

## Workspace and execution boundary

Each run receives a copied workspace under `<home>/.agentpermit/workspaces/<run_id>`. The source directory is not edited directly. Protected globs are excluded from copies, listings, snapshots, and file tools.

Commands are structured as `program` plus `args`, matched against exact argv prefixes, and launched with `shell=False`. AgentPermit uses a minimal environment, continuously drains bounded output, and terminates the process tree on timeout.

`max_mcp_frame_bytes` bounds a newline-delimited MCP frame before JSON parsing, and `max_tool_argument_bytes` applies again at the gateway for non-MCP callers. `max_file_bytes` bounds each file read by copy, file tools, patches, and snapshots. `max_source_bytes` bounds the aggregate regular-file content copied into a new workspace. The defaults are 1,048,576, 262,144, 1,048,576, and 16,777,216 bytes respectively.

This boundary is not OS isolation. Same-user processes can modify local files, and an allowed executable retains the host filesystem and network access granted by the operating system. Use a real container or sandbox when hostile-code isolation is required.

## Evidence and dashboard

SQLite stores runs, events, decisions, approvals, and snapshot references. Durable event payloads are recursively redacted; file contents and command logs are summarized or bounded. Before/after snapshots drive created, modified, and deleted evidence in the dashboard.

The dashboard binds only to loopback and reads the same SQLite source of truth as the CLI. State-changing approval forms require a per-process CSRF token, reviewer, and reason. Large or binary changes are reported as metadata instead of being rendered inline.

## Supported product boundary

AgentPermit targets a local, single-user developer workflow. Multi-user authentication, remote deployment, container orchestration, PyPI publication, and organization-wide policy management are outside the current product boundary.
