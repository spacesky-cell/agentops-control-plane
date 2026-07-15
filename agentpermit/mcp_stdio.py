from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from . import __version__
from .gateway import RuntimeGateway
from .models import ToolRequest, ToolStatus
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
    source: str | Path | None = None
    task: str = "MCP stdio run"
    agent_name: str = "mcp-stdio"
    auto_approve: bool = False
    run_id: str | None = None
    workspace: Path | None = None
    initialize_requested: bool = False
    initialized: bool = False
    _finalized: bool = False
    _pending_request_fingerprint: str | None = None
    _pending_approval_id: int | None = None
    _terminal_status: str | None = None

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
            self._finish_failed()
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": str(exc)},
            }

    def close(self, *, transport_failed: bool = False) -> None:
        """Finalize a transport exactly once, preserving approval waits."""
        if self._finalized or not self.run_id or self.workspace is None:
            return
        run = self.gateway.audit_store.get_run(self.run_id)
        if not run:
            self._finalized = True
            return
        if run["status"] in {"success", "failed"}:
            self._finalized = True
            return
        if not transport_failed and run["status"] == "waiting_for_approval":
            approval = self._pending_approval()
            if approval is not None and str(approval["status"]) == "rejected":
                self._finish_failed()
            elif approval is None or str(approval["status"]) not in {
                "pending",
                "approved",
            }:
                self._finish_failed()
            self._finalized = True
            return
        status = (
            "failed"
            if transport_failed or self._terminal_status == "failed"
            else "success"
        )
        try:
            self.gateway.finish_run(self.run_id, self.workspace, status)
        finally:
            self._finalized = True

    def _ensure_run(self) -> None:
        if self.run_id is not None:
            return
        self.run_id, self.workspace = self.gateway.start_run(
            self.task,
            self.agent_name,
            self.source,
        )

    def _response_id(self, request: Any) -> str | int | None:
        if not isinstance(request, dict):
            return None
        request_id = request.get("id")
        if isinstance(request_id, str) or (
            isinstance(request_id, int) and not isinstance(request_id, bool)
        ):
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
        if isinstance(request_id, str) or (
            isinstance(request_id, int) and not isinstance(request_id, bool)
        ):
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
        protocol_version = (
            requested_version
            if requested_version == PROTOCOL_VERSION
            else PROTOCOL_VERSION
        )
        return {
            "protocolVersion": protocol_version,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"listChanged": False},
                "prompts": {"listChanged": False},
            },
            "serverInfo": {"name": "agentpermit", "version": __version__},
        }

    def _list_tools(self) -> dict[str, Any]:
        return {"tools": list_tool_definitions()}

    def _list_resources(self) -> dict[str, Any]:
        return {"resources": []}

    def _list_prompts(self) -> dict[str, Any]:
        return {"prompts": []}

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        self._ensure_run()
        if self.run_id is None or self.workspace is None:
            raise ValueError("Unable to start governed MCP run")
        request = ToolRequest(
            tool_name=str(params["name"]),
            args=dict(params.get("arguments", {})),
            requested_by=self.agent_name,
        )
        run = self.gateway.audit_store.get_run(self.run_id)
        if self._terminal_status == "failed" or (
            run is not None and run["status"] == "failed"
        ):
            return {
                "status": ToolStatus.FAILED.value,
                "ok": False,
                "output": None,
                "error": "This governed run has failed; further tool calls are disabled.",
            }

        request_fingerprint = self.gateway.request_fingerprint(request)
        if self._pending_approval_id is not None:
            approval = self._pending_approval()
            if request_fingerprint != self._pending_request_fingerprint:
                if approval is not None and str(approval["status"]) == "rejected":
                    self._finish_failed()
                    return {
                        "status": ToolStatus.DENIED.value,
                        "ok": False,
                        "output": None,
                        "error": "The pending approval was rejected.",
                        "approval_id": self._pending_approval_id,
                    }
                return {
                    "status": ToolStatus.PENDING_APPROVAL.value,
                    "ok": False,
                    "output": None,
                    "error": "Session is waiting for approval of the original tool request.",
                    "approval_id": self._pending_approval_id,
                }
            if (
                approval is None
                or approval["run_id"] != self.run_id
                or approval["request_fingerprint"] != self._pending_request_fingerprint
            ):
                self._finish_failed()
                return {
                    "status": ToolStatus.FAILED.value,
                    "ok": False,
                    "output": None,
                    "error": "The pending approval is unavailable.",
                }
            approval_status = str(approval["status"])
            if approval_status == "pending":
                return {
                    "status": ToolStatus.PENDING_APPROVAL.value,
                    "ok": False,
                    "output": None,
                    "error": "Session is waiting for approval of the original tool request.",
                    "approval_id": self._pending_approval_id,
                }
            if approval_status == "rejected":
                self._finish_failed()
                return {
                    "status": ToolStatus.DENIED.value,
                    "ok": False,
                    "output": None,
                    "error": "The pending approval was rejected.",
                    "approval_id": self._pending_approval_id,
                }
            if approval_status != "approved":
                self._finish_failed()
                return {
                    "status": ToolStatus.FAILED.value,
                    "ok": False,
                    "output": None,
                    "error": f"The pending approval is no longer actionable: {approval_status}.",
                    "approval_id": self._pending_approval_id,
                }
            if not self.gateway.resume_run(self.run_id):
                # A terminalizer may win after the approval read. Do not execute
                # the retried request or resurrect the run in that case.
                self._terminal_status = "failed"
                self._pending_request_fingerprint = None
                self._pending_approval_id = None
                return {
                    "status": ToolStatus.FAILED.value,
                    "ok": False,
                    "output": None,
                    "error": "The governed run is no longer waiting for approval; retry was not executed.",
                }

        result = self.gateway.execute_tool(
            self.run_id,
            self.workspace,
            request,
            auto_approve=self.auto_approve,
        )
        if result.status == ToolStatus.PENDING_APPROVAL:
            if self._pending_approval_id is None:
                self._pending_request_fingerprint = request_fingerprint
                self._pending_approval_id = result.approval_id
                self.gateway.pause_run(
                    self.run_id,
                    "waiting_for_approval",
                    approval_id=result.approval_id,
                )
        else:
            self._pending_request_fingerprint = None
            self._pending_approval_id = None
        if result.status in {ToolStatus.DENIED, ToolStatus.FAILED}:
            self._finish_failed()
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

    def _pending_approval(self) -> dict[str, Any] | None:
        """Read the pending approval through the public audit store API."""
        if self.run_id is None:
            return None
        if self._pending_approval_id is not None:
            return self.gateway.audit_store.get_approval(self._pending_approval_id)
        approvals = self.gateway.audit_store.list_approvals(self.run_id)
        for approval in reversed(approvals):
            if str(approval["status"]) in {"pending", "approved", "rejected"}:
                self._pending_approval_id = int(approval["id"])
                self._pending_request_fingerprint = str(approval["request_fingerprint"])
                return approval
        return None

    def _finish_failed(self) -> None:
        """Persist a terminal failure once, before returning a failed tool result."""
        self._terminal_status = "failed"
        if self.run_id is None or self.workspace is None:
            return
        run = self.gateway.audit_store.get_run(self.run_id)
        if run is None or str(run["status"]) in {"success", "failed"}:
            return
        try:
            self.gateway.finish_run(self.run_id, self.workspace, "failed")
        except Exception as exc:  # noqa: BLE001 - terminal persistence must survive snapshot failures.
            self.gateway.audit_store.finish_run(
                self.run_id,
                "failed",
                message="Run failed after an unexpected governed tool error.",
                payload={"snapshot_error": str(exc)},
            )

    def _call_tool_mcp(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return self._tool_error("Invalid tools/call params: name is required.")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return self._tool_error(
                "Invalid tools/call params: arguments must be an object."
            )
        validation_error = self._validate_tool_arguments(name, arguments)
        if validation_error is not None:
            return self._tool_error(validation_error)
        payload = self._call_tool(params)
        if payload["ok"]:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": self._stringify_tool_output(payload["output"]),
                    }
                ],
                "isError": False,
            }
        message = payload.get("error") or payload["status"]
        if payload.get("output") is not None:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    }
                ],
                "isError": True,
            }
        if payload.get("approval_id") is not None:
            message = (
                f"{payload['status']}: {message} approval_id={payload['approval_id']}"
            )
        else:
            message = f"{payload['status']}: {message}"
        return {"content": [{"type": "text", "text": message}], "isError": True}

    def _tool_error(self, message: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": message}], "isError": True}

    def _validate_tool_arguments(
        self, name: str, arguments: dict[str, Any]
    ) -> str | None:
        definitions = {tool["name"]: tool for tool in list_tool_definitions()}
        tool = definitions.get(name)
        if tool is None:
            return f"Invalid tools/call params: unknown tool: {name}."
        schema = tool.get("inputSchema", {})
        required = schema.get("required", [])
        for field in required:
            if field not in arguments:
                return f"Invalid tools/call arguments: {field} is required."
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for field in arguments:
                if field not in properties:
                    return f"Invalid tools/call arguments: unexpected field: {field}."
        for field, value in arguments.items():
            field_schema = properties.get(field, {})
            expected = field_schema.get("type")
            if expected == "string" and not isinstance(value, str):
                return f"Invalid tools/call arguments: {field} must be a string."
            if expected == "array":
                if not isinstance(value, list):
                    return f"Invalid tools/call arguments: {field} must be an array."
                item_type = field_schema.get("items", {}).get("type")
                if item_type == "string":
                    for index, item in enumerate(value):
                        if not isinstance(item, str):
                            return f"Invalid tools/call arguments: {field}[{index}] must be a string."
        return None

    def _stringify_tool_output(self, output: Any) -> str:
        if isinstance(output, str):
            return output
        return json.dumps(output, ensure_ascii=False, indent=2)


def serve_json_lines(
    gateway: RuntimeGateway,
    input_stream: TextIO,
    output_stream: TextIO,
    *,
    source: str | Path | None = None,
    task: str = "MCP stdio run",
    agent_name: str = "mcp-stdio",
    auto_approve: bool = False,
) -> None:
    session = McpStdioSession(
        gateway,
        source=source,
        task=task,
        agent_name=agent_name,
        auto_approve=auto_approve,
    )
    try:
        for line in input_stream:
            if not line.strip():
                continue
            frame = line.rstrip("\r\n")
            frame_bytes = len(frame.encode("utf-8"))
            frame_limit = gateway.policy_engine.config.max_mcp_frame_bytes
            if frame_bytes > frame_limit:
                frame_response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32001,
                        "message": "MCP frame exceeds max_mcp_frame_bytes.",
                        "data": {
                            "limit": "max_mcp_frame_bytes",
                            "max_bytes": frame_limit,
                            "actual_bytes": frame_bytes,
                        },
                    },
                }
                output_stream.write(
                    json.dumps(frame_response, ensure_ascii=False) + "\n"
                )
                output_stream.flush()
                continue
            response: dict[str, Any] | None
            try:
                request = json.loads(frame)
            except json.JSONDecodeError:
                response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error."},
                }
            else:
                response = session.handle(request)
            if response is None:
                continue
            output_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
            output_stream.flush()
    except Exception:
        try:
            session.close(transport_failed=True)
        finally:
            raise
    else:
        session.close()
