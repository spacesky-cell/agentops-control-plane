from pathlib import Path

import json

import pytest

from agentpermit.config import PolicyConfig, is_protected_path, load_policy
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
        ToolRequest(
            "run_command", {"program": "git", "args": ["reset", "--hard", "HEAD"]}
        ),
        Path("workspace"),
    )

    assert decision.decision == Decision.DENY


def test_allows_unittest_command():
    decision = PolicyEngine().evaluate(
        ToolRequest(
            "run_command", {"program": "python", "args": ["-m", "unittest", "-q"]}
        ),
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
        ToolRequest(
            "run_command", {"program": "python", "args": ["-m", "pytest", "-q"]}
        ),
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

    for malformed_args in (
        None,
        42,
        "python -m unittest",
        ["python", "-m", "unittest"],
    ):
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


@pytest.mark.parametrize(
    ("tool", "args", "decision"),
    [
        ("list_files", {}, Decision.ALLOW),
        ("unknown_tool", {}, Decision.REQUIRE_APPROVAL),
        ("read_file", {}, Decision.DENY),
        ("read_file", {"path": ".env"}, Decision.DENY),
        ("read_file", {"path": "../outside"}, Decision.DENY),
        ("read_file", {"path": "app.py"}, Decision.ALLOW),
        ("write_file", {}, Decision.DENY),
        ("write_file", {"path": ".git/config"}, Decision.DENY),
        ("write_file", {"path": "../outside"}, Decision.DENY),
        ("patch_text", {}, Decision.DENY),
        ("patch_text", {"path": ".env"}, Decision.DENY),
        ("patch_text", {"path": "../outside"}, Decision.DENY),
    ],
)
def test_policy_covers_tool_boundary_decisions(tmp_path, tool, args, decision):
    result = PolicyEngine().evaluate(ToolRequest(tool, args), tmp_path)
    assert result.decision == decision


def test_policy_allows_configured_writes_patches_and_unknown_commands(tmp_path):
    engine = PolicyEngine(
        PolicyConfig(
            write_requires_approval=False,
            patch_requires_approval=False,
            unknown_command_requires_approval=False,
        )
    )
    assert (
        engine.evaluate(
            ToolRequest("write_file", {"path": "new.txt"}), tmp_path
        ).decision
        == Decision.ALLOW
    )
    assert (
        engine.evaluate(
            ToolRequest("patch_text", {"path": "app.py"}), tmp_path
        ).decision
        == Decision.ALLOW
    )
    assert (
        engine.evaluate(
            ToolRequest("run_command", {"program": "custom", "args": []}), tmp_path
        ).decision
        == Decision.ALLOW
    )


def test_policy_config_rejects_invalid_rules_and_loads_overrides(tmp_path):
    with pytest.raises(ValueError, match="must be a list"):
        PolicyConfig(command_allow_prefixes="bad")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=r"command_deny_prefixes\[0\]"):
        PolicyConfig(command_deny_prefixes=[[]])
    with pytest.raises(FileNotFoundError, match="Policy file not found"):
        load_policy(tmp_path / "missing.json")

    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        json.dumps({"write_requires_approval": False, "max_output_chars": 123}),
        encoding="utf-8",
    )
    loaded = load_policy(policy_path)
    assert loaded.write_requires_approval is False
    assert loaded.max_output_chars == 123


def test_protected_path_normalizes_prefixes_and_directory_globs():
    assert is_protected_path("././.GIT/config", [".git/**"])
    assert is_protected_path("nested/private/data.txt", ["**/private/**"])
    assert not is_protected_path("nested/public/data.txt", ["**/private/**"])
