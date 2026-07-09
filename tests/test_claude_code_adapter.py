import json
import subprocess
from pathlib import Path

import pytest

from agentops_control_plane.claude_code_adapter import (
    ClaudeCodePlanAdapter,
    ClaudeCodePlanner,
)
from agentops_control_plane.gateway import RuntimeGateway


def make_sample_repo(root: Path) -> Path:
    source = root / "sample_repo"
    source.mkdir()
    (source / "math_utils.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    return source


def completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["claude"], returncode=0, stdout=stdout, stderr="")


def test_claude_code_planner_invokes_cli_without_tools_and_parses_tool_calls(tmp_path):
    calls = []

    def fake_runner(args, **kwargs):
        calls.append((args, kwargs))
        return completed(
            json.dumps(
                {
                    "result": json.dumps(
                        {
                            "name": "claude-generated-plan",
                            "tool_calls": [{"name": "read_file", "arguments": {"path": "math_utils.py"}}],
                        }
                    )
                }
            )
        )

    planner = ClaudeCodePlanner(command="claude-custom", runner=fake_runner)

    plan = planner.plan("Inspect the repo", workspace=tmp_path)

    assert plan.name == "claude-generated-plan"
    assert plan.tool_calls[0].name == "read_file"
    args, kwargs = calls[0]
    assert args[:4] == ["claude-custom", "--safe-mode", "-p", "--tools="]
    assert "--output-format" in args
    assert kwargs["cwd"] == tmp_path
    assert kwargs["timeout"] == 120
    assert kwargs["shell"] is False


def test_claude_code_planner_accepts_plain_json_stdout(tmp_path):
    planner = ClaudeCodePlanner(
        command="claude",
        runner=lambda args, **kwargs: completed(
            json.dumps(
                {
                    "name": "plain-json",
                    "tool_calls": [{"name": "list_files", "arguments": {}}],
                }
            )
        ),
    )

    plan = planner.plan("List files", workspace=tmp_path)

    assert plan.name == "plain-json"
    assert plan.tool_calls[0].name == "list_files"


def test_claude_code_planner_rejects_invalid_tool_plan(tmp_path):
    planner = ClaudeCodePlanner(
        command="claude",
        runner=lambda args, **kwargs: completed(json.dumps({"tool_calls": [{"arguments": {}}]})),
    )

    with pytest.raises(ValueError, match="tool_calls\\[0\\].name is required"):
        planner.plan("bad plan", workspace=tmp_path)


def test_claude_code_planner_reports_cli_errors(tmp_path):
    planner = ClaudeCodePlanner(
        command="claude",
        runner=lambda args, **kwargs: subprocess.CompletedProcess(
            args=args,
            returncode=1,
            stdout="",
            stderr="503 no available accounts",
        ),
    )

    with pytest.raises(RuntimeError, match="Claude Code exited with 1: 503 no available accounts"):
        planner.plan("Try claude", workspace=tmp_path)


def test_claude_code_plan_adapter_runs_generated_plan_through_gateway(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")

    planner = ClaudeCodePlanner(
        command="claude",
        runner=lambda args, **kwargs: completed(
            json.dumps(
                {
                    "result": json.dumps(
                        {
                            "name": "claude-code-test",
                            "tool_calls": [
                                {"name": "read_file", "arguments": {"path": "math_utils.py"}}
                            ],
                        }
                    )
                }
            )
        ),
    )
    adapter = ClaudeCodePlanAdapter(planner=planner)

    run_id = adapter.run(gateway, "Read math_utils.py", source=source, auto_approve=False)
    run = gateway.audit_store.get_run(run_id)
    events = gateway.audit_store.get_events(run_id)
    metadata = gateway.audit_store.get_run_metadata(run_id)

    assert run["agent_name"] == "claude-code-plan"
    assert run["status"] == "success"
    assert metadata["adapter"] == "claude-code-plan"
    assert metadata["plan_name"] == "claude-code-test"
    assert metadata["task"] == "Read math_utils.py"
    assert any(event["type"] == "claude_code_plan_generated" for event in events)
    assert any(event["type"] == "mcp_tool_call" and event["tool_name"] == "read_file" for event in events)
