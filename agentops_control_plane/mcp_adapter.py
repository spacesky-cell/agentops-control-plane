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
    plan_path: Path | None = None

    @classmethod
    def from_file(cls, path: str | Path) -> "McpPlanAdapter":
        plan_path = Path(path).resolve()
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        return cls(
            name=data.get("name", "mcp-plan-adapter"),
            tool_calls=[
                McpToolCall(name=item["name"], arguments=item.get("arguments", {}))
                for item in data["tool_calls"]
            ],
            plan_path=plan_path,
        )

    def run(
        self,
        gateway: RuntimeGateway,
        task: str,
        source: str | Path | None = None,
        auto_approve: bool = False,
    ) -> str:
        run_id, workspace = gateway.start_run(task, self.name, source)
        gateway.audit_store.set_run_metadata(
            run_id,
            {
                "adapter": "mcp-plan",
                "plan_path": str(self.plan_path) if self.plan_path else "",
                "source": str(Path(source).resolve()) if source is not None else "",
                "task": task,
            },
        )
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
        run = gateway.audit_store.get_run(run_id)
        if not run:
            raise ValueError(f"Run not found: {run_id}")
        if run["status"] != "waiting_for_approval":
            raise ValueError(f"Run is not waiting for approval: {run['status']}")

        approvals = gateway.audit_store.list_approvals(run_id)
        approved = [
            item for item in approvals if item["status"] == "approved" and item["tool_name"]
        ]
        if not approved:
            raise ValueError("No approved pending action found for this run.")

        workspace = Path(run["workspace_path"])
        executed_count = sum(
            1 for event in gateway.audit_store.get_events(run_id) if event["type"] == "tool_executed"
        )
        status = "success"
        for index, call in enumerate(self.tool_calls[executed_count:], start=executed_count + 1):
            request = ToolRequest(
                tool_name=call.name,
                args=call.arguments,
                requested_by=self.name,
            )
            gateway.audit_store.add_event(
                run_id,
                "mcp_tool_call",
                f"Resumed MCP tool call {index}: {call.name}",
                {"tool_call": {"name": call.name, "arguments": gateway._redact_args(call.arguments)}},
                call.name,
            )
            preapproved_by = approver if index == executed_count + 1 else None
            result = gateway.execute_tool(
                run_id,
                workspace,
                request,
                auto_approve=auto_approve_remaining,
                preapproved_by=preapproved_by,
            )
            if result.status == ToolStatus.PENDING_APPROVAL:
                if preapproved_by:
                    raise ValueError(result.error or "No approved pending action found for this request.")
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
