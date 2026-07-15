import json
import runpy
import sys
from io import StringIO
from pathlib import Path

import pytest

from agentpermit import cli


def make_sample_repo(root: Path) -> Path:
    source = root / "sample_repo"
    source.mkdir()
    (source / "math_utils.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    return source


def make_plan(root: Path) -> Path:
    plan = root / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "name": "cli-test-agent",
                "steps": [{"tool": "read_file", "args": {"path": "math_utils.py"}}],
            }
        ),
        encoding="utf-8",
    )
    return plan


def make_write_plan(root: Path, name: str = "write-plan") -> Path:
    plan = root / f"{name}.json"
    plan.write_text(
        json.dumps(
            {
                "name": name,
                "steps": [
                    {
                        "tool": "write_file",
                        "args": {"path": "result.txt", "content": "complete\n"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return plan


def test_cli_uses_current_working_directory_for_agentpermit_home(
    tmp_path, monkeypatch, capsys
):
    source = make_sample_repo(tmp_path)
    plan = make_plan(tmp_path)
    monkeypatch.chdir(tmp_path)
    cli.main(["run-script", "--plan", str(plan), "--source", str(source)])
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "success"
    assert (tmp_path / ".agentpermit" / "runs.sqlite").exists()


def test_cli_home_option_overrides_agentpermit_home(tmp_path, monkeypatch, capsys):
    source = make_sample_repo(tmp_path)
    plan = make_plan(tmp_path)
    custom_home = tmp_path / "custom-home"
    monkeypatch.chdir(tmp_path)
    cli.main(
        [
            "--home",
            str(custom_home),
            "run-script",
            "--plan",
            str(plan),
            "--source",
            str(source),
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "success"
    assert (custom_home / ".agentpermit" / "runs.sqlite").exists()
    assert not (tmp_path / ".agentpermit" / "runs.sqlite").exists()


def test_cli_mcp_uses_standard_protocol(tmp_path, monkeypatch):
    source = make_sample_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    input_stream = StringIO(
        "\n".join(
            [
                json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
                ),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                json.dumps(
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
                ),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "read_file",
                            "arguments": {"path": "math_utils.py"},
                        },
                    }
                ),
            ]
        )
    )
    output_stream = StringIO()
    monkeypatch.setattr(sys, "stdin", input_stream)
    monkeypatch.setattr(sys, "stdout", output_stream)
    cli.main(["mcp", "--source", str(source), "--task", "cli stdio read"])
    responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert [response["id"] for response in responses] == [1, 2, 3]
    assert responses[-1]["result"]["isError"] is False


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


def test_cli_review_resume_listing_and_exports(tmp_path, monkeypatch, capsys):
    source = make_sample_repo(tmp_path)
    plan = make_write_plan(tmp_path)
    home = tmp_path / "home"
    monkeypatch.chdir(tmp_path)

    cli.main(
        [
            "--home",
            str(home),
            "run-script",
            "--plan",
            str(plan),
            "--source",
            str(source),
        ]
    )
    run = json.loads(capsys.readouterr().out)
    assert run["status"] == "waiting_for_approval"

    cli.main(["--home", str(home), "approvals", "--run-id", run["run_id"]])
    approvals = json.loads(capsys.readouterr().out)
    approval_id = approvals[0]["id"]

    cli.main(
        [
            "--home",
            str(home),
            "approve",
            str(approval_id),
            "--approver",
            "reviewer",
            "--reason",
            "checked",
        ]
    )
    assert f"Approved {approval_id}" in capsys.readouterr().out

    cli.main(
        [
            "--home",
            str(home),
            "resume-script",
            run["run_id"],
            "--plan",
            str(plan),
            "--approver",
            "reviewer",
        ]
    )
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["status"] == "success"

    cli.main(["--home", str(home), "runs"])
    assert run["run_id"] in capsys.readouterr().out
    cli.main(["--home", str(home), "show", run["run_id"]])
    assert '"status": "success"' in capsys.readouterr().out

    for report_format in ("json", "html"):
        report = tmp_path / f"report.{report_format}"
        cli.main(
            [
                "--home",
                str(home),
                "export",
                run["run_id"],
                "--format",
                report_format,
                "--out",
                str(report),
            ]
        )
        assert report.exists()
        assert f"Wrote {report}" in capsys.readouterr().out


def test_cli_reject_init_policy_eval_and_error_paths(tmp_path, monkeypatch, capsys):
    source = make_sample_repo(tmp_path)
    read_plan = make_plan(tmp_path)
    write_plan = make_write_plan(tmp_path, "reject-plan")
    home = tmp_path / "home"
    monkeypatch.chdir(tmp_path)

    cli.main(["--home", str(home), "init-policy", "--out", "policy.json"])
    assert (home / "policy.json").exists()
    capsys.readouterr()

    cli.main(
        [
            "--home",
            str(home),
            "run-script",
            "--plan",
            str(write_plan),
            "--source",
            str(source),
        ]
    )
    run = json.loads(capsys.readouterr().out)
    cli.main(["--home", str(home), "approvals", "--run-id", run["run_id"]])
    approval_id = json.loads(capsys.readouterr().out)[0]["id"]
    cli.main(["--home", str(home), "reject", str(approval_id), "--reason", "unsafe"])
    assert f"Rejected {approval_id}" in capsys.readouterr().out

    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text(
        json.dumps(
            {
                "name": "read-eval",
                "task": "read sample",
                "source": str(source),
                "plan": str(read_plan),
                "expected_status": "success",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    cli.main(
        [
            "--home",
            str(home),
            "eval",
            "--tasks",
            str(tasks),
            "--auto-approve",
        ]
    )
    assert json.loads(capsys.readouterr().out)["failed"] == 0

    with pytest.raises(SystemExit, match="Run not found"):
        cli.main(["--home", str(home), "show", "run_missing"])

    monkeypatch.setattr(cli, "run_eval", lambda *args, **kwargs: {"failed": 1})
    with pytest.raises(SystemExit) as exc:
        cli.main(["--home", str(home), "eval", "--tasks", str(tasks)])
    assert exc.value.code == 1


def test_cli_serve_delegates_loopback_settings(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        cli, "serve", lambda store, host, port: calls.append((host, port))
    )
    cli.main(
        ["--home", str(tmp_path), "serve", "--host", "localhost", "--port", "9123"]
    )
    assert calls == [("localhost", 9123)]
    assert "Serving http://localhost:9123" in capsys.readouterr().out


def test_module_entrypoint_dispatches_to_cli_help(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["agentpermit", "--help"])
    with pytest.raises(SystemExit, match="0"):
        runpy.run_module("agentpermit", run_name="__main__")
