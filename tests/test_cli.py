import json
import sys
from io import StringIO
from pathlib import Path

from agentpermit import cli


def make_sample_repo(root: Path) -> Path:
    source = root / "sample_repo"
    source.mkdir()
    (source / "math_utils.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    return source


def make_plan(root: Path) -> Path:
    plan = root / "plan.json"
    plan.write_text(json.dumps({"name": "cli-test-agent", "steps": [{"tool": "read_file", "args": {"path": "math_utils.py"}}]}), encoding="utf-8")
    return plan


def test_cli_uses_current_working_directory_for_agentpermit_home(tmp_path, monkeypatch, capsys):
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
    cli.main(["--home", str(custom_home), "run-script", "--plan", str(plan), "--source", str(source)])
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
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
                json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read_file", "arguments": {"path": "math_utils.py"}}}),
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
    plan.write_text("\ufeff" + json.dumps({"name": "bom-agent", "steps": [{"tool": "read_file", "args": {"path": "math_utils.py"}}]}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cli.main(["run-script", "--plan", str(plan), "--source", str(source)])
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "success"
