from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .audit import AuditStore
from .config import PolicyConfig
from .models import Decision, ToolRequest, ToolResult, ToolStatus
from .policy import PolicyEngine
from .tools import ToolExecutor
from .workspace import WorkspaceManager


class RuntimeGateway:
    def __init__(
        self,
        audit_store: AuditStore,
        workspace_manager: WorkspaceManager,
        policy_engine: PolicyEngine,
        tool_executor: ToolExecutor,
    ) -> None:
        self.audit_store = audit_store
        self.workspace_manager = workspace_manager
        self.policy_engine = policy_engine
        self.tool_executor = tool_executor

    @classmethod
    def from_home(cls, home: str | Path, config: PolicyConfig | None = None) -> "RuntimeGateway":
        root = Path(home)
        agentops_dir = root / ".agentops"
        policy_config = config or PolicyConfig()
        workspace_manager = WorkspaceManager(agentops_dir)
        return cls(
            audit_store=AuditStore(agentops_dir / "runs.sqlite"),
            workspace_manager=workspace_manager,
            policy_engine=PolicyEngine(policy_config),
            tool_executor=ToolExecutor(workspace_manager, policy_config),
        )

    def start_run(self, task: str, agent_name: str, source: str | Path | None = None) -> tuple[str, Path]:
        provisional_workspace = Path(".")
        run_id = self.audit_store.start_run(task, agent_name, provisional_workspace)
        workspace = self.workspace_manager.create(run_id, source)
        self._update_workspace_path(run_id, workspace)
        snapshot = self.workspace_manager.snapshot(run_id, workspace, "before")
        self.audit_store.add_event(
            run_id,
            "run_started",
            "Run started.",
            {"workspace": str(workspace), "snapshot": str(snapshot)},
        )
        return run_id, workspace

    def finish_run(self, run_id: str, workspace: Path, status: str) -> None:
        snapshot = self.workspace_manager.snapshot(run_id, workspace, "after")
        self.audit_store.add_event(
            run_id,
            "run_finished",
            f"Run finished with status {status}.",
            {"snapshot": str(snapshot)},
        )
        self.audit_store.finish_run(run_id, status)

    def pause_run(self, run_id: str, status: str = "waiting_for_approval") -> None:
        self.audit_store.add_event(
            run_id,
            "run_paused",
            f"Run paused with status {status}.",
        )
        self.audit_store.pause_run(run_id, status)

    def execute_tool(
        self,
        run_id: str,
        workspace: Path,
        request: ToolRequest,
        auto_approve: bool = False,
        preapproved_by: str | None = None,
    ) -> ToolResult:
        decision = self.policy_engine.evaluate(request, workspace)
        self.audit_store.add_event(
            run_id,
            "policy_decision",
            decision.reason,
            {"args": self._redact_args(request.args)},
            request.tool_name,
            decision.decision.value,
            decision.risk.value,
        )
        if decision.decision == Decision.DENY:
            return ToolResult(ToolStatus.DENIED, error=decision.reason, decision=decision)
        if decision.decision == Decision.REQUIRE_APPROVAL and not auto_approve and not preapproved_by:
            approval_id = self.audit_store.create_approval(
                run_id,
                request.tool_name,
                {
                    "args": self._redact_args(request.args),
                    "requested_by": request.requested_by,
                    "request_fingerprint": self.request_fingerprint(request),
                },
                decision.reason,
            )
            self.audit_store.add_event(
                run_id,
                "approval_requested",
                f"Approval required: {decision.reason}",
                {"approval_id": approval_id},
                request.tool_name,
                decision.decision.value,
                decision.risk.value,
            )
            return ToolResult(
                ToolStatus.PENDING_APPROVAL,
                error=decision.reason,
                decision=decision,
                approval_id=approval_id,
            )
        if decision.decision == Decision.REQUIRE_APPROVAL and auto_approve:
            approval_id = self.audit_store.create_approval(
                run_id,
                request.tool_name,
                {
                    "args": self._redact_args(request.args),
                    "requested_by": request.requested_by,
                    "request_fingerprint": self.request_fingerprint(request),
                },
                decision.reason,
            )
            self.audit_store.decide_approval(
                approval_id,
                "approved",
                "auto-approve",
                "Demo auto-approval mode.",
            )
            self.audit_store.add_event(
                run_id,
                "approval_auto_approved",
                "Approval auto-approved for demo run.",
                {"approval_id": approval_id},
                request.tool_name,
                "approved",
                decision.risk.value,
            )
        if decision.decision == Decision.REQUIRE_APPROVAL and preapproved_by:
            approval_id = self._find_approved_approval(run_id, request)
            if approval_id is None:
                return ToolResult(
                    ToolStatus.PENDING_APPROVAL,
                    error="No approved pending action found for this request.",
                    decision=decision,
                )
            self.audit_store.consume_approval(approval_id)
            self.audit_store.add_event(
                run_id,
                "approval_used",
                f"Pre-approved action executed by {preapproved_by}.",
                {"approved_by": preapproved_by, "approval_id": approval_id},
                request.tool_name,
                "approved",
                decision.risk.value,
            )
        try:
            output = self.tool_executor.execute(workspace, request.tool_name, request.args)
        except Exception as exc:  # noqa: BLE001 - audit boundary should capture tool exceptions.
            self.audit_store.add_event(
                run_id,
                "tool_failed",
                str(exc),
                {"args": self._redact_args(request.args)},
                request.tool_name,
                decision.decision.value,
                decision.risk.value,
            )
            return ToolResult(ToolStatus.FAILED, error=str(exc), decision=decision)
        self.audit_store.add_event(
            run_id,
            "tool_executed",
            f"Tool {request.tool_name} executed.",
            {
                "args": self._redact_args(request.args),
                "output": self._redact_output(request.tool_name, output),
            },
            request.tool_name,
            decision.decision.value,
            decision.risk.value,
        )
        return ToolResult(ToolStatus.OK, output=output, decision=decision)

    def _update_workspace_path(self, run_id: str, workspace: Path) -> None:
        with self.audit_store._connect() as conn:
            conn.execute(
                "UPDATE runs SET workspace_path = ? WHERE id = ?",
                (str(workspace), run_id),
            )

    def _redact_args(self, args: dict[str, Any]) -> dict[str, Any]:
        redacted = dict(args)
        for key in list(redacted):
            lowered = key.lower()
            if "token" in lowered or "secret" in lowered or "password" in lowered:
                redacted[key] = "[redacted]"
        if "content" in redacted and isinstance(redacted["content"], str):
            content = redacted["content"]
            redacted["content_preview"] = content[:500]
            redacted["content_chars"] = len(content)
            del redacted["content"]
        return redacted

    def request_fingerprint(self, request: ToolRequest) -> str:
        payload = {
            "tool_name": request.tool_name,
            "args": request.args,
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _find_approved_approval(self, run_id: str, request: ToolRequest) -> int | None:
        fingerprint = self.request_fingerprint(request)
        for approval in self.audit_store.list_approvals(run_id):
            if approval["status"] != "approved":
                continue
            if approval["tool_name"] != request.tool_name:
                continue
            if approval.get("payload", {}).get("request_fingerprint") == fingerprint:
                return int(approval["id"])
        return None

    def _redact_output(self, tool_name: str, output: Any) -> Any:
        if tool_name == "read_file" and isinstance(output, str):
            return self._summarize_text(output)
        if tool_name == "run_command" and isinstance(output, dict):
            redacted = dict(output)
            command_output = redacted.get("output")
            if isinstance(command_output, str):
                redacted["output"] = self._summarize_text(command_output)
            return redacted
        return output

    def _summarize_text(self, text: str) -> dict[str, Any]:
        return {
            "content_preview": text[:500],
            "content_chars": len(text),
            "truncated": len(text) > 500,
        }
