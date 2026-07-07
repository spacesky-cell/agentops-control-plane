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
