from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agents import AgentAdapter
from .gateway import RuntimeGateway
from .models import ToolRequest, ToolStatus


@dataclass(frozen=True)
class McpToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass
class McpPlanAdapter(AgentAdapter):
    name: str
    tool_calls: list[McpToolCall]

    @classmethod
    def from_file(cls, path: str | Path) -> "McpPlanAdapter":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            name=data.get("name", "mcp-plan-adapter"),
            tool_calls=[
                McpToolCall(name=item["name"], arguments=item.get("arguments", {}))
                for item in data["tool_calls"]
            ],
        )

    def run(
        self,
        gateway: RuntimeGateway,
        task: str,
        source: str | Path | None = None,
        auto_approve: bool = False,
    ) -> str:
        run_id, workspace = gateway.start_run(task, self.name, source)
        status = "success"
        for index, call in enumerate(self.tool_calls, start=1):
            request = ToolRequest(
                tool_name=call.name,
                args=call.arguments,
                requested_by=self.name,
            )
            gateway.audit_store.add_event(
                run_id,
                "mcp_tool_call",
                f"MCP tool call {index}: {call.name}",
                {"tool_call": {"name": call.name, "arguments": gateway._redact_args(call.arguments)}},
                call.name,
            )
            result = gateway.execute_tool(run_id, workspace, request, auto_approve=auto_approve)
            if result.status == ToolStatus.PENDING_APPROVAL:
                status = "waiting_for_approval"
                break
            if not result.ok:
                status = "failed"
                break
            if request.tool_name == "run_command" and result.output.get("exit_code") != 0:
                status = "failed"
                break
        if status == "waiting_for_approval":
            gateway.pause_run(run_id, status)
        else:
            gateway.finish_run(run_id, workspace, status)
        return run_id

    def resume(
        self,
        gateway: RuntimeGateway,
        run_id: str,
        approver: str = "human",
        auto_approve_remaining: bool = False,
    ) -> str:
        raise NotImplementedError("MCP plan resume is not implemented yet.")
