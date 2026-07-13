from __future__ import annotations

import hashlib
import json
from pathlib import Path

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
        agentpermit_dir = root / ".agentpermit"
        policy_config = config or PolicyConfig()
        workspace_manager = WorkspaceManager(agentpermit_dir, policy_config)
        return cls(
            audit_store=AuditStore(agentpermit_dir / "runs.sqlite"),
            workspace_manager=workspace_manager,
            policy_engine=PolicyEngine(policy_config),
            tool_executor=ToolExecutor(workspace_manager, policy_config),
        )

    def start_run(self, task: str, agent_name: str, source: str | Path | None = None) -> tuple[str, Path]:
        run_id = self.audit_store.start_run(task, agent_name)
        workspace: Path | None = None
        identity: tuple[int, int] | None = None
        try:
            workspace = self.workspace_manager.create(run_id, source)
            identity = self.workspace_manager.workspace_identity(workspace)
            self.audit_store.activate_run_workspace(run_id, workspace, identity)
            snapshot = self.workspace_manager.snapshot(run_id, workspace, "before")
            self.audit_store.add_event(
                run_id,
                "run_started",
                "Run started.",
                {"workspace": str(workspace), "snapshot": str(snapshot)},
            )
            return run_id, workspace
        except Exception as exc:
            cleanup_error: str | None = None
            if workspace is not None and identity is not None:
                try:
                    self.workspace_manager.remove_workspace(workspace, identity)
                except Exception as cleanup_exc:  # noqa: BLE001 - audit cleanup failure.
                    cleanup_error = str(cleanup_exc)
            self.audit_store.finish_run(run_id, "failed")
            self.audit_store.add_event(
                run_id,
                "run_start_failed",
                str(exc),
                {"cleanup_error": cleanup_error} if cleanup_error else {},
            )
            raise

    def finish_run(self, run_id: str, workspace: Path, status: str) -> None:
        workspace = self._verified_workspace(run_id, workspace)
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
        workspace = self._verified_workspace(run_id, workspace)
        decision = self.policy_engine.evaluate(request, workspace)
        self.audit_store.add_event(
            run_id,
            "policy_decision",
            decision.reason,
            {"args": request.args},
            request.tool_name,
            decision.decision.value,
            decision.risk.value,
        )
        if decision.decision == Decision.DENY:
            return ToolResult(ToolStatus.DENIED, error=decision.reason, decision=decision)
        if decision.decision == Decision.REQUIRE_APPROVAL:
            resolution = self.audit_store.resolve_approval(
                run_id,
                request.tool_name,
                self.request_fingerprint(request),
                {
                    "args": request.args,
                    "requested_by": request.requested_by,
                },
                decision.reason,
                auto_approve=auto_approve,
            )
            if resolution.state == "rejected":
                self.audit_store.add_event(
                    run_id,
                    "approval_rejected",
                    "Matching approval was rejected.",
                    {"approval_id": resolution.approval_id},
                    request.tool_name,
                    "rejected",
                    decision.risk.value,
                )
                return ToolResult(
                    ToolStatus.DENIED,
                    error="A matching approval was rejected.",
                    decision=decision,
                    approval_id=resolution.approval_id,
                )
            if resolution.state == "pending":
                event_type = "approval_requested" if resolution.created else "approval_pending"
                self.audit_store.add_event(
                    run_id,
                    event_type,
                    f"Approval required: {decision.reason}",
                    {"approval_id": resolution.approval_id},
                    request.tool_name,
                    decision.decision.value,
                    decision.risk.value,
                )
                return ToolResult(
                    ToolStatus.PENDING_APPROVAL,
                    error=decision.reason,
                    decision=decision,
                    approval_id=resolution.approval_id,
                )
            if auto_approve and resolution.approver == "auto-approve":
                self.audit_store.add_event(
                    run_id,
                    "approval_auto_approved",
                    "Approval auto-approved by a trusted server adapter.",
                    {"approval_id": resolution.approval_id},
                    request.tool_name,
                    "approved",
                    decision.risk.value,
                )
            else:
                approved_by = resolution.approver or preapproved_by or "reviewer"
                self.audit_store.add_event(
                    run_id,
                    "approval_used",
                    f"Approved action executed by {approved_by}.",
                    {"approved_by": approved_by, "approval_id": resolution.approval_id},
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
                {"args": request.args},
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
                "args": request.args,
                "output": output,
            },
            request.tool_name,
            decision.decision.value,
            decision.risk.value,
        )
        return ToolResult(ToolStatus.OK, output=output, decision=decision)

    def resume_workspace(self, run_id: str) -> Path:
        run = self.audit_store.get_run(run_id)
        if not run:
            raise ValueError(f"Run not found: {run_id}")
        identity = self.audit_store.get_run_workspace_identity(run_id)
        return self.workspace_manager.register_workspace(
            Path(str(run["workspace_path"])), identity
        )

    def _verified_workspace(self, run_id: str, workspace: Path) -> Path:
        authoritative = self.resume_workspace(run_id)
        supplied = Path(workspace).absolute()
        if supplied != authoritative:
            raise ValueError(
                f"Workspace path does not match authoritative run workspace: {workspace}"
            )
        return authoritative

    def request_fingerprint(self, request: ToolRequest) -> str:
        payload = {
            "tool_name": request.tool_name,
            "args": request.args,
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
