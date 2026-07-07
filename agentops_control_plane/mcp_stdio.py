from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from .gateway import RuntimeGateway
from .models import ToolRequest


@dataclass
class McpStdioSession:
    gateway: RuntimeGateway
    run_id: str | None = None
    workspace: Path | None = None

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        try:
            method = request["method"]
            params = request.get("params", {})
            if method == "run.start":
                result = self._start(params)
            elif method == "tool.call":
                result = self._call_tool(params)
            elif method == "run.finish":
                result = self._finish(params)
            else:
                raise ValueError(f"Unsupported method: {method}")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:  # noqa: BLE001 - transport boundary returns structured errors.
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": str(exc)},
            }

    def _start(self, params: dict[str, Any]) -> dict[str, Any]:
        task = str(params.get("task", "MCP stdio run"))
        agent_name = str(params.get("agent_name", "mcp-stdio"))
        source = params.get("source")
        self.run_id, self.workspace = self.gateway.start_run(task, agent_name, source)
        return {"run_id": self.run_id, "status": "running", "workspace": str(self.workspace)}

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
        response = session.handle(json.loads(line))
        output_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
        output_stream.flush()
