import json
from pathlib import Path

from agentops_control_plane.gateway import RuntimeGateway
from agentops_control_plane.mcp_adapter import McpPlanAdapter


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


def write_mcp_plan(path: Path, calls: list[dict]) -> Path:
    path.write_text(
        json.dumps(
            {
                "name": "mcp-local-test",
                "tool_calls": calls,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_mcp_plan_adapter_runs_tool_calls_through_gateway(tmp_path):
    source = make_sample_repo(tmp_path)
    plan = write_mcp_plan(
        tmp_path / "mcp_plan.json",
        [
            {"name": "read_file", "arguments": {"path": "math_utils.py"}},
            {
                "name": "patch_text",
                "arguments": {"path": "math_utils.py", "old": "return a - b", "new": "return a + b"},
            },
            {"name": "run_command", "arguments": {"command": "python -m unittest -q"}},
        ],
    )
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    adapter = McpPlanAdapter.from_file(plan)

    run_id = adapter.run(gateway, "run mcp-style plan", source=source, auto_approve=True)
    run = gateway.audit_store.get_run(run_id)
    events = gateway.audit_store.get_events(run_id)

    assert run["agent_name"] == "mcp-local-test"
    assert run["status"] == "success"
    assert any(event["type"] == "mcp_tool_call" for event in events)
    assert any(event["type"] == "tool_executed" and event["tool_name"] == "patch_text" for event in events)


def test_mcp_plan_adapter_pauses_for_approval(tmp_path):
    source = make_sample_repo(tmp_path)
    plan = write_mcp_plan(
        tmp_path / "mcp_plan.json",
        [
            {
                "name": "patch_text",
                "arguments": {"path": "math_utils.py", "old": "return a - b", "new": "return a + b"},
            }
        ],
    )
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    adapter = McpPlanAdapter.from_file(plan)

    run_id = adapter.run(gateway, "approval mcp-style plan", source=source, auto_approve=False)
    run = gateway.audit_store.get_run(run_id)
    approvals = gateway.audit_store.list_approvals(run_id)

    assert run["status"] == "waiting_for_approval"
    assert approvals[0]["tool_name"] == "patch_text"
    assert approvals[0]["payload"]["requested_by"] == "mcp-local-test"


def test_mcp_plan_adapter_resumes_after_approval(tmp_path):
    source = make_sample_repo(tmp_path)
    plan = write_mcp_plan(
        tmp_path / "mcp_plan.json",
        [
            {"name": "read_file", "arguments": {"path": "math_utils.py"}},
            {
                "name": "patch_text",
                "arguments": {"path": "math_utils.py", "old": "return a - b", "new": "return a + b"},
            },
            {"name": "run_command", "arguments": {"command": "python -m unittest -q"}},
        ],
    )
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    adapter = McpPlanAdapter.from_file(plan)

    run_id = adapter.run(gateway, "approval mcp-style plan", source=source, auto_approve=False)
    approval = gateway.audit_store.list_approvals(run_id)[0]
    gateway.audit_store.decide_approval(approval["id"], "approved", "reviewer", "Looks safe")
    adapter.resume(gateway, run_id, approver="reviewer")
    run = gateway.audit_store.get_run(run_id)
    approvals = gateway.audit_store.list_approvals(run_id)
    events = gateway.audit_store.get_events(run_id)

    assert run["status"] == "success"
    assert approvals[0]["status"] == "consumed"
    assert any(event["type"] == "approval_used" for event in events)


def test_mcp_tool_call_event_redacts_content_arguments(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    content = "x" * 900
    plan = write_mcp_plan(
        tmp_path / "mcp_plan.json",
        [
            {"name": "write_file", "arguments": {"path": "notes.txt", "content": content}},
        ],
    )
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    adapter = McpPlanAdapter.from_file(plan)

    run_id = adapter.run(gateway, "write mcp-style plan", source=source, auto_approve=True)
    events = gateway.audit_store.get_events(run_id)
    mcp_event = [event for event in events if event["type"] == "mcp_tool_call"][-1]

    arguments = mcp_event["payload"]["tool_call"]["arguments"]
    assert "content" not in arguments
    assert arguments["content_preview"] == content[:500]
    assert arguments["content_chars"] == len(content)
    assert content not in json.dumps(mcp_event["payload"], ensure_ascii=False)
