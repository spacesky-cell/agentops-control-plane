# MCP Stdio Transport

`serve-mcp-stdio` exposes AgentOps Control Plane through newline-delimited
JSON-RPC over stdin/stdout. It is intended for local MCP-style clients that
need governed tool execution, audit logs, approval gates, and isolated
workspaces without embedding AgentOps Python APIs directly.

## Start The Server

```powershell
python -m agentops_control_plane serve-mcp-stdio
```

Use the global `--home` option before the subcommand to choose where runtime
data is stored:

```powershell
python -m agentops_control_plane --home .demo serve-mcp-stdio
```

The server reads one JSON object per line from stdin and writes one JSON-RPC
response per line to stdout. Notifications do not produce responses.

## MCP Session Order

MCP clients should use this order:

1. Send `initialize`.
2. Send `notifications/initialized`.
3. Call `tools/list`, `resources/list`, `prompts/list`, or `tools/call`.

`ping` is available before initialization. Standard MCP methods other than
`initialize`, `ping`, and notifications require the initialized session state.

## Initialize

Request:

```json
{"jsonrpc":"2.0","id":"init","method":"initialize","params":{"protocolVersion":"2025-06-18"}}
```

Response shape:

```json
{
  "jsonrpc": "2.0",
  "id": "init",
  "result": {
    "protocolVersion": "2025-06-18",
    "capabilities": {
      "tools": {"listChanged": false},
      "resources": {"listChanged": false},
      "prompts": {"listChanged": false}
    },
    "serverInfo": {
      "name": "agentops-control-plane",
      "version": "0.1.0"
    }
  }
}
```

The server currently supports protocol version `2025-06-18`. If a client asks
for a different version, the response falls back to `2025-06-18`.

Send the initialized notification after receiving the initialize response:

```json
{"jsonrpc":"2.0","method":"notifications/initialized"}
```

## Supported Methods

| Method | Requires initialized MCP session | Response |
| --- | --- | --- |
| `initialize` | No | Protocol version, capabilities, server info |
| `notifications/initialized` | No | Notification, no response |
| `notifications/...` | No | Unknown notifications are ignored |
| `ping` | No | `{}` |
| `tools/list` | Yes | Tool definitions with `inputSchema` |
| `tools/call` | Yes | MCP content blocks with `isError` |
| `resources/list` | Yes | `{"resources": []}` |
| `prompts/list` | Yes | `{"prompts": []}` |
| `run.start` | No | Local compatibility run lifecycle |
| `tool.call` | No | Local compatibility tool-call shape |
| `run.finish` | No | Local compatibility run lifecycle |

The local `run.start`, `tool.call`, and `run.finish` methods are retained for
deterministic scripts and tests that use the same process boundary without the
MCP session lifecycle. New MCP clients should prefer `tools/list` and
`tools/call`.

## Tools

`tools/list` returns the current governed tool surface:

- `list_files`
- `read_file`
- `write_file`
- `patch_text`
- `run_command`

Each tool definition includes an `inputSchema`. `tools/call` validates
`arguments` against the same schema before sending the request to the gateway:

- required fields must be present
- unknown fields are rejected when `additionalProperties` is `false`
- declared string fields must be strings
- unknown tool names are rejected before policy evaluation

Validation errors are returned as MCP tool results with `isError: true`, not as
transport-level JSON-RPC internal errors.

## Tool Call Example

One manual JSON-lines session can look like this:

```jsonl
{"jsonrpc":"2.0","id":"init","method":"initialize","params":{"protocolVersion":"2025-06-18"}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":"start","method":"run.start","params":{"task":"MCP stdio read","agent_name":"mcp-client","source":"examples/sample_repo"}}
{"jsonrpc":"2.0","id":"read","method":"tools/call","params":{"name":"read_file","arguments":{"path":"math_utils.py"}}}
```

`run.start` creates the governed workspace:

```json
{"jsonrpc":"2.0","id":"start","method":"run.start","params":{"task":"MCP stdio read","agent_name":"mcp-client","source":"examples/sample_repo"}}
```

Then call a standard MCP tool after initialization:

```json
{"jsonrpc":"2.0","id":"read","method":"tools/call","params":{"name":"read_file","arguments":{"path":"math_utils.py"}}}
```

Successful `tools/call` responses use MCP content blocks:

```json
{
  "jsonrpc": "2.0",
  "id": "read",
  "result": {
    "content": [
      {
        "type": "text",
        "text": "def add(a, b):\n    return a - b\n\n"
      }
    ],
    "isError": false
  }
}
```

Policy denials, approval requirements, tool failures, and argument validation
errors are also returned as `tools/call` results, but with `isError: true`.

## JSON-RPC Validation

The transport validates the request envelope before method dispatch:

- the request must be a JSON object
- `jsonrpc` must be `"2.0"`
- request methods that expect a response must include a string or integer `id`
- `params`, when present, must be an object
- notifications must use the `notifications/` method namespace

Invalid request envelopes return JSON-RPC error code `-32600`. Malformed JSON
returns `-32700`. Unknown request methods return `-32601`.

MCP methods that require the initialized session state return `-32002` when
called too early.

## Approval Semantics

`tools/call` still crosses the normal `RuntimeGateway` boundary:

```text
MCP client -> stdio transport -> RuntimeGateway -> PolicyEngine -> ToolExecutor
```

Write and patch operations can pause with `pending_approval` unless the run is
executed through a path that supplies auto-approval. Approval rows, policy
decisions, tool results, and snapshots are recorded in the same SQLite audit
store used by the CLI and dashboard.
