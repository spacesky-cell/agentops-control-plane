from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from . import __version__
from .gateway import RuntimeGateway
from .models import ToolRequest
from .tools import list_tool_definitions

PROTOCOL_VERSION = "2025-06-18"


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class McpStdioSession:
    gateway: RuntimeGateway
    run_id: str | None = None
    workspace: Path | None = None
    initialize_requested: bool = False
    initialized: bool = False

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        request_id = request.get("id")
        try:
            method = self._request_method(request)
            params = request.get("params", {})
            if method == "initialize":
                result = self._initialize(params)
            elif method == "notifications/initialized":
                if self.initialize_requested:
                    self.initialized = True
                return None
            elif method == "ping":
                result = {}
            elif method == "run.start":
                result = self._start(params)
            elif method == "tools/list":
                self._require_initialized()
                result = self._list_tools()
            elif method == "tools/call":
                self._require_initialized()
                result = self._call_tool_mcp(params)
            elif method == "tool.call":
                result = self._call_tool(params)
            elif method == "run.finish":
                result = self._finish(params)
            else:
                raise JsonRpcError(-32601, f"Method not found: {method}")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except JsonRpcError as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": exc.code, "message": exc.message},
            }
        except Exception as exc:  # noqa: BLE001 - transport boundary returns structured errors.
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": str(exc)},
            }

    def _request_method(self, request: dict[str, Any]) -> str:
        method = request.get("method")
        if not isinstance(method, str) or not method:
            raise JsonRpcError(-32600, "Invalid Request: missing method.")
        return method

    def _require_initialized(self) -> None:
        if not self.initialized:
            raise JsonRpcError(-32002, "Session is not initialized.")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        self.initialize_requested = True
        requested_version = str(params.get("protocolVersion") or PROTOCOL_VERSION)
        protocol_version = requested_version if requested_version == PROTOCOL_VERSION else PROTOCOL_VERSION
        return {
            "protocolVersion": protocol_version,
            "capabilities": {
                "tools": {"listChanged": False},
            },
            "serverInfo": {
                "name": "agentops-control-plane",
                "version": __version__,
            },
        }

    def _start(self, params: dict[str, Any]) -> dict[str, Any]:
        task = str(params.get("task", "MCP stdio run"))
        agent_name = str(params.get("agent_name", "mcp-stdio"))
        source = params.get("source")
        self.run_id, self.workspace = self.gateway.start_run(task, agent_name, source)
        return {"run_id": self.run_id, "status": "running", "workspace": str(self.workspace)}

    def _list_tools(self) -> dict[str, Any]:
        return {"tools": list_tool_definitions()}

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self.run_id or self.workspace is None:
            raise ValueError("run.start must be called before tool.call")
        request = ToolRequest(
            tool_name=str(params["name"]),
            args=dict(params.get("arguments", {})),
            requested_by=str(params.get("requested_by", "mcp-stdio")),
        )
        result = self.gateway.execute_tool(
            self.run_id,
            self.workspace,
            request,
            auto_approve=bool(params.get("auto_approve", False)),
        )
        payload: dict[str, Any] = {
            "status": result.status.value,
            "ok": result.ok,
            "output": result.output,
        }
        if result.error:
            payload["error"] = result.error
        if result.approval_id is not None:
            payload["approval_id"] = result.approval_id
        return payload

    def _call_tool_mcp(self, params: dict[str, Any]) -> dict[str, Any]:
        payload = self._call_tool(params)
        if payload["ok"]:
            return {
                "content": [{"type": "text", "text": self._stringify_tool_output(payload["output"])}],
                "isError": False,
            }
        message = payload.get("error") or payload["status"]
        if payload.get("approval_id") is not None:
            message = f"{payload['status']}: {message} approval_id={payload['approval_id']}"
        else:
            message = f"{payload['status']}: {message}"
        return {"content": [{"type": "text", "text": message}], "isError": True}

    def _stringify_tool_output(self, output: Any) -> str:
        if isinstance(output, str):
            return output
        return json.dumps(output, ensure_ascii=False, indent=2)

    def _finish(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self.run_id or self.workspace is None:
            raise ValueError("run.start must be called before run.finish")
        status = str(params.get("status", "success"))
        self.gateway.finish_run(self.run_id, self.workspace, status)
        return {"run_id": self.run_id, "status": status}


def serve_json_lines(gateway: RuntimeGateway, input_stream: TextIO, output_stream: TextIO) -> None:
    session = McpStdioSession(gateway)
    for line in input_stream:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error."}}
        else:
            response = session.handle(request)
        if response is None:
            continue
        output_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
        output_stream.flush()
