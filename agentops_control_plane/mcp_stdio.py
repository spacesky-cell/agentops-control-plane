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

    def handle(self, request: Any) -> dict[str, Any] | None:
        request_id = self._response_id(request)
        try:
            self._validate_request_object(request)
            method = self._request_method(request)
            request_id = self._request_id(request, method)
            params = self._request_params(request)
            if method == "initialize":
                result = self._initialize(params)
            elif self._is_notification(method):
                self._handle_notification(method)
                return None
            elif method == "ping":
                result = {}
            elif method == "run.start":
                result = self._start(params)
            elif method == "tools/list":
                self._require_initialized()
                result = self._list_tools()
            elif method == "resources/list":
                self._require_initialized()
                result = self._list_resources()
            elif method == "prompts/list":
                self._require_initialized()
                result = self._list_prompts()
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

    def _response_id(self, request: Any) -> str | int | None:
        if not isinstance(request, dict):
            return None
        request_id = request.get("id")
        if isinstance(request_id, str) or (isinstance(request_id, int) and not isinstance(request_id, bool)):
            return request_id
        return None

    def _validate_request_object(self, request: Any) -> None:
        if not isinstance(request, dict):
            raise JsonRpcError(-32600, "Invalid Request: request must be an object.")
        if request.get("jsonrpc") != "2.0":
            raise JsonRpcError(-32600, "Invalid Request: jsonrpc must be '2.0'.")

    def _request_method(self, request: dict[str, Any]) -> str:
        method = request.get("method")
        if not isinstance(method, str) or not method:
            raise JsonRpcError(-32600, "Invalid Request: missing method.")
        return method

    def _request_id(self, request: dict[str, Any], method: str) -> str | int | None:
        if self._is_notification(method):
            return None
        if "id" not in request or request["id"] is None:
            raise JsonRpcError(-32600, "Invalid Request: id is required.")
        request_id = request["id"]
        if isinstance(request_id, str) or (isinstance(request_id, int) and not isinstance(request_id, bool)):
            return request_id
        raise JsonRpcError(-32600, "Invalid Request: id must be a string or integer.")

    def _request_params(self, request: dict[str, Any]) -> dict[str, Any]:
        params = request.get("params", {})
        if not isinstance(params, dict):
            raise JsonRpcError(-32600, "Invalid Request: params must be an object.")
        return params

    def _is_notification(self, method: str) -> bool:
        return method.startswith("notifications/")

    def _handle_notification(self, method: str) -> None:
        if method == "notifications/initialized" and self.initialize_requested:
            self.initialized = True

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
                "resources": {"listChanged": False},
                "prompts": {"listChanged": False},
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

    def _list_resources(self) -> dict[str, Any]:
        return {"resources": []}

    def _list_prompts(self) -> dict[str, Any]:
        return {"prompts": []}

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
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return self._tool_error("Invalid tools/call params: name is required.")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return self._tool_error("Invalid tools/call params: arguments must be an object.")
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

    def _tool_error(self, message: str) -> dict[str, Any]:
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
