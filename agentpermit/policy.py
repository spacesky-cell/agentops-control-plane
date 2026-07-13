from __future__ import annotations

from pathlib import Path

from .config import PolicyConfig, is_protected_path
from .models import Decision, PolicyDecision, Risk, ToolRequest


class PolicyEngine:
    def __init__(self, config: PolicyConfig | None = None) -> None:
        self.config = config or PolicyConfig()

    def evaluate(self, request: ToolRequest, workspace_root: Path) -> PolicyDecision:
        if request.tool_name in {"list_files"}:
            return PolicyDecision(Decision.ALLOW, Risk.LOW, "Listing files is read-only.")
        if request.tool_name == "read_file":
            return self._evaluate_read(request, workspace_root)
        if request.tool_name == "write_file":
            return self._evaluate_write(request, workspace_root)
        if request.tool_name == "patch_text":
            return self._evaluate_patch(request, workspace_root)
        if request.tool_name == "run_command":
            return self._evaluate_command(request)
        return PolicyDecision(
            Decision.REQUIRE_APPROVAL,
            Risk.HIGH,
            f"Unknown tool '{request.tool_name}' requires human approval.",
        )

    def _evaluate_read(self, request: ToolRequest, workspace_root: Path) -> PolicyDecision:
        relative = str(request.args.get("path", ""))
        if not relative:
            return PolicyDecision(Decision.DENY, Risk.MEDIUM, "Missing file path.")
        if self._is_protected(relative, workspace_root):
            return PolicyDecision(Decision.DENY, Risk.HIGH, "Protected files cannot be read.")
        if not self._is_inside_workspace(relative, workspace_root):
            return PolicyDecision(Decision.DENY, Risk.HIGH, "Path escapes the workspace.")
        return PolicyDecision(Decision.ALLOW, Risk.LOW, "Read is allowed inside workspace.")

    def _evaluate_write(self, request: ToolRequest, workspace_root: Path) -> PolicyDecision:
        relative = str(request.args.get("path", ""))
        if not relative:
            return PolicyDecision(Decision.DENY, Risk.MEDIUM, "Missing file path.")
        if self._is_protected(relative, workspace_root):
            return PolicyDecision(Decision.DENY, Risk.CRITICAL, "Protected files cannot be written.")
        if not self._is_inside_workspace(relative, workspace_root):
            return PolicyDecision(Decision.DENY, Risk.CRITICAL, "Path escapes the workspace.")
        if self.config.write_requires_approval:
            return PolicyDecision(
                Decision.REQUIRE_APPROVAL,
                Risk.MEDIUM,
                "File writes require approval by policy.",
            )
        return PolicyDecision(Decision.ALLOW, Risk.MEDIUM, "File write allowed by policy.")

    def _evaluate_patch(self, request: ToolRequest, workspace_root: Path) -> PolicyDecision:
        relative = str(request.args.get("path", ""))
        if not relative:
            return PolicyDecision(Decision.DENY, Risk.MEDIUM, "Missing file path.")
        if self._is_protected(relative, workspace_root):
            return PolicyDecision(Decision.DENY, Risk.CRITICAL, "Protected files cannot be patched.")
        if not self._is_inside_workspace(relative, workspace_root):
            return PolicyDecision(Decision.DENY, Risk.CRITICAL, "Path escapes the workspace.")
        if self.config.patch_requires_approval:
            return PolicyDecision(
                Decision.REQUIRE_APPROVAL,
                Risk.MEDIUM,
                "Patch operations require approval by policy.",
            )
        return PolicyDecision(Decision.ALLOW, Risk.MEDIUM, "Patch allowed by policy.")

    def _evaluate_command(self, request: ToolRequest) -> PolicyDecision:
        command = str(request.args.get("command", "")).strip()
        if not command:
            return PolicyDecision(Decision.DENY, Risk.MEDIUM, "Missing command.")
        lowered = f" {command.lower()} "
        for denied in self.config.command_deny_contains:
            if denied.lower() in lowered:
                return PolicyDecision(
                    Decision.DENY,
                    Risk.CRITICAL,
                    f"Command matches denied pattern: {denied}",
                )
        for token in self.config.command_deny_shell_tokens:
            if token in command:
                return PolicyDecision(
                    Decision.DENY,
                    Risk.CRITICAL,
                    f"Command contains shell control token: {token}",
                )
        for prefix in self.config.command_allow_prefixes:
            lowered_command = command.lower()
            lowered_prefix = prefix.lower()
            if lowered_command == lowered_prefix or lowered_command.startswith(f"{lowered_prefix} "):
                return PolicyDecision(Decision.ALLOW, Risk.LOW, "Command matches allowlist.")
        if self.config.unknown_command_requires_approval:
            return PolicyDecision(
                Decision.REQUIRE_APPROVAL,
                Risk.HIGH,
                "Command is not allowlisted and requires approval.",
            )
        return PolicyDecision(Decision.ALLOW, Risk.HIGH, "Unknown command allowed by policy.")

    def _is_inside_workspace(self, relative: str, workspace_root: Path) -> bool:
        candidate = (workspace_root / relative).resolve()
        root = workspace_root.resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return False
        return True

    def _is_protected(self, relative: str, workspace_root: Path) -> bool:
        if is_protected_path(relative, self.config.protected_globs):
            return True
        root = workspace_root.resolve()
        candidate = (root / relative).resolve()
        try:
            resolved_relative = candidate.relative_to(root)
        except ValueError:
            return False
        return is_protected_path(resolved_relative, self.config.protected_globs)

