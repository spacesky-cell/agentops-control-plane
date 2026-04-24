from pathlib import Path

from agentops_control_plane.agents import ScriptedAgent
from agentops_control_plane.gateway import RuntimeGateway


def make_sample_repo(root: Path) -> Path:
    source = root / "sample_repo"
    source.mkdir()
    (source / "math_utils.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (source / "test_math_utils.py").write_text(
        "\n".join(
            [
                "import unittest",
                "from math_utils import add",
                "",
                "class MathUtilsTest(unittest.TestCase):",
                "    def test_add(self):",
                "        self.assertEqual(add(2, 3), 5)",
                "",
                "if __name__ == '__main__':",
                "    unittest.main()",
            ]
        ),
        encoding="utf-8",
    )
    return source


def test_scripted_agent_runs_in_isolated_workspace(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    agent = ScriptedAgent(
        name="test-agent",
        steps=[
            {"tool": "read_file", "args": {"path": "math_utils.py"}},
            {
                "tool": "patch_text",
                "args": {"path": "math_utils.py", "old": "return a - b", "new": "return a + b"},
            },
            {"tool": "run_command", "args": {"command": "python -m unittest -q"}},
        ],
    )

    run_id = agent.run(gateway, "fix sample repo", source=source, auto_approve=True)
    run = gateway.audit_store.get_run(run_id)
    events = gateway.audit_store.get_events(run_id)
    workspace = Path(run["workspace_path"])

    assert run["status"] == "success"
    assert "return a - b" in (source / "math_utils.py").read_text(encoding="utf-8")
    assert "return a + b" in (workspace / "math_utils.py").read_text(encoding="utf-8")
    assert any(event["type"] == "approval_auto_approved" for event in events)
    assert any(event["type"] == "tool_executed" for event in events)


def test_scripted_agent_pauses_without_auto_approval(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    agent = ScriptedAgent(
        name="test-agent",
        steps=[
            {
                "tool": "patch_text",
                "args": {"path": "math_utils.py", "old": "return a - b", "new": "return a + b"},
            },
            {"tool": "run_command", "args": {"command": "python -m unittest -q"}},
        ],
    )

    run_id = agent.run(gateway, "fix sample repo", source=source, auto_approve=False)
    run = gateway.audit_store.get_run(run_id)
    approvals = gateway.audit_store.list_approvals(run_id)

    assert run["status"] == "waiting_for_approval"
    assert approvals[0]["status"] == "pending"


def test_scripted_agent_resumes_after_approval(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    agent = ScriptedAgent(
        name="test-agent",
        steps=[
            {"tool": "read_file", "args": {"path": "math_utils.py"}},
            {
                "tool": "patch_text",
                "args": {"path": "math_utils.py", "old": "return a - b", "new": "return a + b"},
            },
            {"tool": "run_command", "args": {"command": "python -m unittest -q"}},
        ],
    )

    run_id = agent.run(gateway, "fix sample repo", source=source, auto_approve=False)
    approval = gateway.audit_store.list_approvals(run_id)[0]
    gateway.audit_store.decide_approval(approval["id"], "approved", "reviewer", "Looks safe")
    agent.resume(gateway, run_id, approver="reviewer")
    run = gateway.audit_store.get_run(run_id)
    events = gateway.audit_store.get_events(run_id)

    assert run["status"] == "success"
    assert any(event["type"] == "approval_used" for event in events)
