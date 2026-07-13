from pathlib import Path

from agentpermit.models import Decision, ToolRequest
from agentpermit.policy import PolicyEngine


def test_denies_workspace_escape():
    decision = PolicyEngine().evaluate(
        ToolRequest("read_file", {"path": "../secret.txt"}),
        Path("workspace"),
    )

    assert decision.decision == Decision.DENY


def test_denies_dangerous_command():
    decision = PolicyEngine().evaluate(
        ToolRequest("run_command", {"command": "git reset --hard HEAD"}),
        Path("workspace"),
    )

    assert decision.decision == Decision.DENY


def test_allows_unittest_command():
    decision = PolicyEngine().evaluate(
        ToolRequest("run_command", {"command": "python -m unittest -q"}),
        Path("workspace"),
    )

    assert decision.decision == Decision.ALLOW


def test_denies_allowlisted_command_with_shell_control_operator():
    decision = PolicyEngine().evaluate(
        ToolRequest("run_command", {"command": "python -m unittest -q && git status"}),
        Path("workspace"),
    )

    assert decision.decision == Decision.DENY


def test_denies_command_that_only_shares_allowlisted_string_prefix():
    decision = PolicyEngine().evaluate(
        ToolRequest("run_command", {"command": "git statusx"}),
        Path("workspace"),
    )

    assert decision.decision == Decision.REQUIRE_APPROVAL


def test_patch_requires_approval():
    decision = PolicyEngine().evaluate(
        ToolRequest("patch_text", {"path": "app.py", "old": "x", "new": "y"}),
        Path("workspace"),
    )

    assert decision.decision == Decision.REQUIRE_APPROVAL

