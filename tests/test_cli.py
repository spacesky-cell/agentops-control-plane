import json
import sys
from io import StringIO
from pathlib import Path

import subprocess

from agentops_control_plane import cli


def make_sample_repo(root: Path) -> Path:
    source = root / "sample_repo"
    source.mkdir()
    (source / "math_utils.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    return source


def make_plan(root: Path) -> Path:
    plan = root / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "name": "cli-test-agent",
                "steps": [
                    {"tool": "read_file", "args": {"path": "math_utils.py"}},
                ],
            }
        ),
        encoding="utf-8",
    )
    return plan


def make_mcp_plan(root: Path) -> Path:
    plan = root / "mcp_plan.json"
    plan.write_text(
        json.dumps(
            {
                "name": "cli-mcp-agent",
                "tool_calls": [
                    {"name": "read_file", "arguments": {"path": "math_utils.py"}},
                ],
            }
        ),
        encoding="utf-8",
    )
    return plan


def make_mcp_patch_plan(root: Path) -> Path:
    plan = root / "mcp_patch_plan.json"
    plan.write_text(
        json.dumps(
            {
                "name": "cli-mcp-patch-agent",
                "tool_calls": [
                    {
                        "name": "patch_text",
                        "arguments": {
                            "path": "math_utils.py",
                            "old": "return a + b",
                            "new": "return a + b",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return plan


def test_cli_uses_current_working_directory_for_agentops_home(tmp_path, monkeypatch, capsys):
    source = make_sample_repo(tmp_path)
    plan = make_plan(tmp_path)
    monkeypatch.chdir(tmp_path)

    cli.main(["run-script", "--plan", str(plan), "--source", str(source)])

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "success"
    assert (tmp_path / ".agentops" / "runs.sqlite").exists()


def test_cli_home_option_overrides_agentops_home(tmp_path, monkeypatch, capsys):
    source = make_sample_repo(tmp_path)
    plan = make_plan(tmp_path)
    custom_home = tmp_path / "custom-home"
    monkeypatch.chdir(tmp_path)

    cli.main(["--home", str(custom_home), "run-script", "--plan", str(plan), "--source", str(source)])

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "success"
    assert (custom_home / ".agentops" / "runs.sqlite").exists()
    assert not (tmp_path / ".agentops" / "runs.sqlite").exists()


def test_cli_runs_mcp_plan(tmp_path, monkeypatch, capsys):
    source = make_sample_repo(tmp_path)
    plan = make_mcp_plan(tmp_path)
    monkeypatch.chdir(tmp_path)

    cli.main(["run-mcp-plan", "--plan", str(plan), "--source", str(source)])

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "success"
    assert output["run_id"].startswith("run_")


def test_cli_runs_claude_code_plan_with_injected_runner(tmp_path, monkeypatch, capsys):
    source = make_sample_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    def fake_runner(args, **kwargs):
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps(
                {
                    "result": json.dumps(
                        {
                            "name": "cli-claude-plan",
                            "tool_calls": [
                                {"name": "read_file", "arguments": {"path": "math_utils.py"}}
                            ],
                        }
                    )
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_runner)

    cli.main(
        [
            "run-claude-code-plan",
            "--source",
            str(source),
            "--task",
            "Read math_utils.py",
            "--claude-command",
            "claude-test",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "success"
    assert output["run_id"].startswith("run_")


def test_cli_reports_claude_code_plan_failure_without_traceback(tmp_path, monkeypatch, capsys):
    source = make_sample_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    def fake_runner(args, **kwargs):
        return subprocess.CompletedProcess(
            args=args,
            returncode=1,
            stdout="",
            stderr="503 no available accounts",
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_runner)

    cli.main(
        [
            "run-claude-code-plan",
            "--source",
            str(source),
            "--task",
            "Read math_utils.py",
            "--claude-command",
            "claude-test",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "failed"
    assert output["run_id"].startswith("run_")
    assert "503 no available accounts" in output["error"]


def test_cli_resumes_mcp_plan_after_approval(tmp_path, monkeypatch, capsys):
    source = make_sample_repo(tmp_path)
    plan = make_mcp_patch_plan(tmp_path)
    monkeypatch.chdir(tmp_path)

    cli.main(["run-mcp-plan", "--plan", str(plan), "--source", str(source)])
    first_output = json.loads(capsys.readouterr().out)
    assert first_output["status"] == "waiting_for_approval"

    cli.main(["approvals", "--run-id", first_output["run_id"]])
    approval = json.loads(capsys.readouterr().out)[0]
    cli.main(["approve", str(approval["id"]), "--approver", "reviewer"])
    capsys.readouterr()

    cli.main(["resume-mcp-plan", first_output["run_id"], "--plan", str(plan), "--approver", "reviewer"])
    output = json.loads(capsys.readouterr().out)

    assert output["run_id"] == first_output["run_id"]
    assert output["status"] == "success"


def test_cli_serves_mcp_stdio_json_lines(tmp_path, monkeypatch):
    source = make_sample_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    input_stream = StringIO(
        "\n".join(
            [
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "start",
                        "method": "run.start",
                        "params": {
                            "task": "cli stdio read",
                            "agent_name": "stdio-agent",
                            "source": str(source),
                        },
                    }
                ),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "read",
                        "method": "tool.call",
                        "params": {"name": "read_file", "arguments": {"path": "math_utils.py"}},
                    }
                ),
            ]
        )
    )
    output_stream = StringIO()
    monkeypatch.setattr(sys, "stdin", input_stream)
    monkeypatch.setattr(sys, "stdout", output_stream)

    cli.main(["serve-mcp-stdio"])

    responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert responses[0]["id"] == "start"
    assert responses[0]["result"]["status"] == "running"
    assert responses[1]["id"] == "read"
    assert responses[1]["result"]["status"] == "ok"


def test_cli_reads_utf8_bom_scripted_plan(tmp_path, monkeypatch, capsys):
    source = make_sample_repo(tmp_path)
    plan = tmp_path / "plan_bom.json"
    plan.write_text(
        "\ufeff"
        + json.dumps(
            {
                "name": "bom-agent",
                "steps": [{"tool": "read_file", "args": {"path": "math_utils.py"}}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    cli.main(["run-script", "--plan", str(plan), "--source", str(source)])

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "success"
