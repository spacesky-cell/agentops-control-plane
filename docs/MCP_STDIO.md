# Standard MCP stdio

AgentPermit exposes a standard newline-delimited MCP JSON-RPC server. One transport owns at most one governed run, created lazily by its first `tools/call`.

## Client configuration

Claude Code project configuration:

```powershell
claude mcp add --scope project agentpermit -- agentpermit --home . mcp --source . --task "Govern this workspace"
```

Codex project configuration:

```powershell
codex mcp add agentpermit -- agentpermit --home . mcp --source . --task "Govern this workspace"
```

Both clients treat every token after `--` as the MCP server command and arguments. Use an absolute executable path if the client's environment cannot resolve the npm bin directory.

You can also run the server directly:

```powershell
agentpermit --home . mcp --source . --task "Inspect the repository"
```

`--auto-approve` is available only as a deliberately trusted server-process option. Values supplied by the MCP client cannot enable it.

## Lifecycle

1. Client sends `initialize`.
2. Client sends `notifications/initialized`.
3. Client may call `tools/list`, `resources/list`, and `prompts/list`.
4. The first `tools/call` starts the governed run and copied workspace; later calls reuse it.
5. A pending approval blocks any different request. Retrying the identical request after review atomically consumes the approval and executes once.
6. Clean EOF finishes an active run as `success`; a waiting run remains `waiting_for_approval`. Transport failure marks a started active run as failed.

Only the standard MCP methods described here are available.

## Example exchange

Requests are one JSON object per line:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"read_file","arguments":{"path":"README.md"}}}
```

The initialize response reports server name/version and the selected supported protocol version. Notification lines do not receive a response.

Tool results use MCP content blocks:

```json
{"content":[{"type":"text","text":"..."}],"isError":false}
```

Policy denials, pending approvals, validation errors, and tool failures set `isError: true` while preserving JSON-RPC transport success. Invalid JSON, invalid requests, unknown methods, and invalid parameters use the corresponding JSON-RPC error codes.

## Exposed tools

- `list_files`: optional workspace-relative glob.
- `read_file`: UTF-8 workspace-relative path.
- `write_file`: UTF-8 path and content.
- `patch_text`: path, exact old text, and replacement text.
- `run_command`: exact `program` string plus `args` string array.

All tool schemas reject additional properties. File tools remain inside the copied workspace and protected globs are inaccessible. `run_command` does not accept a shell string.

## Approval retry

When a tool requires review, the response includes a stable approval id. Review it at the loopback dashboard or with:

```powershell
agentpermit --home . approvals --run-id <run_id>
agentpermit --home . approve <approval_id> --approver reviewer --reason "Reviewed exact request"
```

The MCP client must retry the same tool name and arguments. A different call cannot replace the pending request, and an approved record is consumed only once.
