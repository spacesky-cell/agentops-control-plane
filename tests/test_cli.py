import json
from pathlib import Path

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
