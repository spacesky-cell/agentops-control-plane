from pathlib import Path

from agentops_control_plane.models import Decision, ToolRequest
from agentops_control_plane.policy import PolicyEngine


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


def test_patch_requires_approval():
    decision = PolicyEngine().evaluate(
        ToolRequest("patch_text", {"path": "app.py", "old": "x", "new": "y"}),
        Path("workspace"),
    )

    assert decision.decision == Decision.REQUIRE_APPROVAL

