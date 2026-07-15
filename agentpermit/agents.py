from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .gateway import RuntimeGateway
from .models import ToolRequest, ToolStatus
from .redaction import redact_durable


@runtime_checkable
class AgentAdapter(Protocol):
    name: str

    def run(
        self,
        gateway: RuntimeGateway,
        task: str,
        source: str | Path | None = None,
        auto_approve: bool = False,
    ) -> str:
        """Start a governed run through the runtime gateway."""

    def resume(
        self,
        gateway: RuntimeGateway,
        run_id: str,
        approver: str = "human",
        auto_approve_remaining: bool = False,
    ) -> str:
        """Resume a paused governed run through the runtime gateway."""


@dataclass
class ScriptedAgent(AgentAdapter):
    name: str
    steps: list[dict[str, Any]]

    @classmethod
    def from_file(cls, path: str | Path) -> "ScriptedAgent":
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return cls(name=data.get("name", "scripted-agent"), steps=data["steps"])

    def run(
        self,
        gateway: RuntimeGateway,
        task: str,
        source: str | Path | None = None,
        auto_approve: bool = False,
    ) -> str:
        run_id, workspace = gateway.start_run(task, self.name, source)
        status = "success"
        for index, step in enumerate(self.steps, start=1):
            request = ToolRequest(
                tool_name=step["tool"],
                args=step.get("args", {}),
                requested_by=self.name,
            )
            gateway.audit_store.add_event(
                run_id,
                "agent_step",
                f"Agent step {index}: {request.tool_name}",
                redact_durable({"step": step}),
                request.tool_name,
            )
            result = gateway.execute_tool(
                run_id, workspace, request, auto_approve=auto_approve
            )
            if result.status == ToolStatus.PENDING_APPROVAL:
                status = "waiting_for_approval"
                break
            if not result.ok:
                status = "failed"
                break
            if (
                request.tool_name == "run_command"
                and result.output.get("exit_code") != 0
            ):
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
            item
            for item in approvals
            if item["status"] == "approved" and item["tool_name"]
        ]
        if not approved:
            raise ValueError("No approved pending action found for this run.")

        if not gateway.resume_run(run_id):
            current = gateway.audit_store.get_run(run_id)
            status = current["status"] if current else "missing"
            raise ValueError(f"Run is not waiting for approval: {status}")
        workspace = gateway.resume_workspace(run_id)
        executed_count = sum(
            1
            for event in gateway.audit_store.get_events(run_id)
            if event["type"] == "tool_executed"
        )
        status = "success"
        for index, step in enumerate(
            self.steps[executed_count:], start=executed_count + 1
        ):
            request = ToolRequest(
                tool_name=step["tool"],
                args=step.get("args", {}),
                requested_by=self.name,
            )
            gateway.audit_store.add_event(
                run_id,
                "agent_step",
                f"Resumed step {index}: {request.tool_name}",
                {"step": step},
                request.tool_name,
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
                    raise ValueError(
                        "No approved pending action found for this request."
                    )
                status = "waiting_for_approval"
                break
            if not result.ok:
                status = "failed"
                break
            if (
                request.tool_name == "run_command"
                and result.output.get("exit_code") != 0
            ):
                status = "failed"
                break
        if status == "waiting_for_approval":
            gateway.pause_run(run_id, status)
        else:
            gateway.finish_run(run_id, workspace, status)
        return run_id
