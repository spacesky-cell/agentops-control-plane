from pathlib import Path

from agentpermit.config import PolicyConfig
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
        ToolRequest("run_command", {"program": "git", "args": ["reset", "--hard", "HEAD"]}),
        Path("workspace"),
    )

    assert decision.decision == Decision.DENY


def test_allows_unittest_command():
    decision = PolicyEngine().evaluate(
        ToolRequest("run_command", {"program": "python", "args": ["-m", "unittest", "-q"]}),
        Path("workspace"),
    )

    assert decision.decision == Decision.ALLOW


def test_denies_command_that_only_shares_allowlisted_string_prefix():
    decision = PolicyEngine().evaluate(
        ToolRequest("run_command", {"program": "git", "args": ["statusx"]}),
        Path("workspace"),
    )

    assert decision.decision == Decision.REQUIRE_APPROVAL


def test_command_rules_compare_exact_argv_elements_and_deny_before_allow():
    config = PolicyConfig(
        command_allow_prefixes=[["python", "-m", "pytest"]],
        command_deny_prefixes=[["python", "-m", "pytest", "--collect-only"]],
    )
    engine = PolicyEngine(config)

    allowed = engine.evaluate(
        ToolRequest("run_command", {"program": "python", "args": ["-m", "pytest", "-q"]}),
        Path("workspace"),
    )
    similar = engine.evaluate(
        ToolRequest("run_command", {"program": "python", "args": ["-m", "pytest-x"]}),
        Path("workspace"),
    )
    denied = engine.evaluate(
        ToolRequest(
            "run_command",
            {"program": "python", "args": ["-m", "pytest", "--collect-only"]},
        ),
        Path("workspace"),
    )

    assert allowed.decision == Decision.ALLOW
    assert similar.decision == Decision.REQUIRE_APPROVAL
    assert denied.decision == Decision.DENY


def test_policy_rejects_legacy_and_malformed_structured_commands():
    engine = PolicyEngine()

    legacy = engine.evaluate(
        ToolRequest("run_command", {"command": "python -m unittest"}),
        Path("workspace"),
    )
    malformed = engine.evaluate(
        ToolRequest("run_command", {"program": "python", "args": "-m unittest"}),
        Path("workspace"),
    )

    assert legacy.decision == Decision.DENY
    assert malformed.decision == Decision.DENY


def test_policy_denies_non_mapping_structured_command_args_without_raising():
    engine = PolicyEngine()

    for malformed_args in (None, 42, "python -m unittest", ["python", "-m", "unittest"]):
        decision = engine.evaluate(
            ToolRequest("run_command", malformed_args),
            Path("workspace"),
        )

        assert decision.decision == Decision.DENY


def test_patch_requires_approval():
    decision = PolicyEngine().evaluate(
        ToolRequest("patch_text", {"path": "app.py", "old": "x", "new": "y"}),
        Path("workspace"),
    )

    assert decision.decision == Decision.REQUIRE_APPROVAL

