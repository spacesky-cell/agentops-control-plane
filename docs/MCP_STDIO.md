# Standard MCP stdio

AgentPermit exposes a standard newline-delimited MCP JSON-RPC server. The
server owns one governed run for the lifetime of the transport. The run is
created lazily on the first `tools/call`, so discovery requests never create
workspace state.

Start it with the public CLI:

```bash
python -m agentpermit mcp --source ./repo --task "Inspect the repository"
```

Pass `--auto-approve` only when the local operator explicitly trusts this
server process. Values supplied by an MCP client in `initialize` or
`tools/call` parameters are ignored.

## Lifecycle

1. Client sends `initialize`.
2. Client sends the `notifications/initialized` notification.
3. Client may call `tools/list`, `resources/list`, and `prompts/list`.
4. The first `tools/call` starts the governed run and executes through the
   policy, approval, workspace, and audit gateway. Further calls reuse it.
5. When a tool needs approval, the session accepts only an identical retry. A
   different tool request cannot resume or replace the pending action. The
   identical retry executes only after that exact approval is approved.
6. Clean EOF finalizes a running run as `success`; a run waiting for approval
   remains `waiting_for_approval`. An input or output transport failure marks
   any started run as `failed` and exits the server with an error.

Only standard MCP methods are available. `run.start`, `tool.call`, and
`run.finish` are not supported and return JSON-RPC method-not-found (`-32601`).

## Example exchange

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"read_file","arguments":{"path":"README.md"}}}
```

Tool results use the MCP content shape (`content: [{"type":"text","text":...}]`)
and set `isError` for policy denials, pending approvals, validation errors, or
tool failures. Approval identifiers are stable across identical retries and a
single approved retry consumes the approval atomically. A rejected approval,
policy denial, or tool failure makes the run terminally failed and blocks later
tool execution in that session.
