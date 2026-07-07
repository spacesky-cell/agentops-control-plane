import json
import zipfile
from pathlib import Path

from agentops_control_plane.agents import ScriptedAgent
from agentops_control_plane.audit import ApprovalNotFoundError
from agentops_control_plane.gateway import RuntimeGateway
from agentops_control_plane.models import ToolRequest


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


def test_scripted_agent_does_not_resume_after_rejection(tmp_path):
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
    approval = gateway.audit_store.list_approvals(run_id)[0]
    gateway.audit_store.decide_approval(approval["id"], "rejected", "reviewer", "Too risky")

    try:
        agent.resume(gateway, run_id, approver="reviewer")
    except ValueError as exc:
        assert "approved pending action" in str(exc)
    else:
        raise AssertionError("resume should reject runs without an approved pending action")

    run = gateway.audit_store.get_run(run_id)
    assert run["status"] == "waiting_for_approval"


def test_scripted_agent_requires_approval_for_each_pending_action(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    (source / "math_utils.py").write_text(
        "def add(a, b):\n    return a - b\n\ndef subtract(a, b):\n    return a + b\n",
        encoding="utf-8",
    )
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    agent = ScriptedAgent(
        name="test-agent",
        steps=[
            {
                "tool": "patch_text",
                "args": {"path": "math_utils.py", "old": "return a - b", "new": "return a + b"},
            },
            {
                "tool": "patch_text",
                "args": {"path": "math_utils.py", "old": "return a + b", "new": "return a - b"},
            },
        ],
    )

    run_id = agent.run(gateway, "two approvals", source=source, auto_approve=False)
    first_approval = gateway.audit_store.list_approvals(run_id)[0]
    gateway.audit_store.decide_approval(first_approval["id"], "approved", "reviewer", "First patch")
    agent.resume(gateway, run_id, approver="reviewer")

    run = gateway.audit_store.get_run(run_id)
    approvals = gateway.audit_store.list_approvals(run_id)
    assert run["status"] == "waiting_for_approval"
    assert [approval["status"] for approval in approvals] == ["consumed", "pending"]

    try:
        agent.resume(gateway, run_id, approver="reviewer")
    except ValueError as exc:
        assert "approved pending action" in str(exc)
    else:
        raise AssertionError("resume should require approval for the current pending action")

    run = gateway.audit_store.get_run(run_id)
    assert run["status"] == "waiting_for_approval"


def test_approval_fingerprint_uses_full_unredacted_request(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    original_content = ("x" * 500) + "a"
    changed_content = ("x" * 500) + "b"
    original_agent = ScriptedAgent(
        name="test-agent",
        steps=[
            {
                "tool": "write_file",
                "args": {"path": "notes.txt", "content": original_content},
            }
        ],
    )
    changed_agent = ScriptedAgent(
        name="test-agent",
        steps=[
            {
                "tool": "write_file",
                "args": {"path": "notes.txt", "content": changed_content},
            }
        ],
    )

    run_id = original_agent.run(gateway, "write notes", source=source, auto_approve=False)
    approval = gateway.audit_store.list_approvals(run_id)[0]
    gateway.audit_store.decide_approval(approval["id"], "approved", "reviewer", "Original write")

    try:
        changed_agent.resume(gateway, run_id, approver="reviewer")
    except ValueError as exc:
        assert "approved pending action" in str(exc)
    else:
        raise AssertionError("resume should reject an approval for a different full request")

    workspace = Path(gateway.audit_store.get_run(run_id)["workspace_path"])
    assert not (workspace / "notes.txt").exists()


def test_snapshots_include_workspace_files(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")

    run_id, workspace = gateway.start_run("snapshot sample repo", "test-agent", source)
    before_snapshot = tmp_path / "project" / ".agentops" / "snapshots" / f"{run_id}-before.zip"

    with zipfile.ZipFile(before_snapshot) as archive:
        assert "math_utils.py" in archive.namelist()
        assert "test_math_utils.py" in archive.namelist()

    gateway.finish_run(run_id, workspace, "success")
    after_snapshot = tmp_path / "project" / ".agentops" / "snapshots" / f"{run_id}-after.zip"

    with zipfile.ZipFile(after_snapshot) as archive:
        assert "math_utils.py" in archive.namelist()
        assert "test_math_utils.py" in archive.namelist()


def test_audit_store_records_schema_version(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")

    assert gateway.audit_store.get_schema_version() == 1


def test_waiting_for_approval_keeps_run_open(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    agent = ScriptedAgent(
        name="test-agent",
        steps=[
            {
                "tool": "patch_text",
                "args": {"path": "math_utils.py", "old": "return a - b", "new": "return a + b"},
            }
        ],
    )

    run_id = agent.run(gateway, "approval required", source=source, auto_approve=False)
    run = gateway.audit_store.get_run(run_id)
    events = gateway.audit_store.get_events(run_id)
    after_snapshot = tmp_path / "project" / ".agentops" / "snapshots" / f"{run_id}-after.zip"

    assert run["status"] == "waiting_for_approval"
    assert run["ended_at"] is None
    assert not after_snapshot.exists()
    assert not any(event["type"] == "run_finished" for event in events)


def test_read_file_audit_uses_preview_not_full_content(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    content = "x" * 900
    (source / "notes.txt").write_text(content, encoding="utf-8")
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    run_id, workspace = gateway.start_run("read audit", "test-agent", source)

    result = gateway.execute_tool(run_id, workspace, ToolRequest("read_file", {"path": "notes.txt"}))
    events = gateway.audit_store.get_events(run_id)
    tool_event = [event for event in events if event["type"] == "tool_executed"][-1]

    assert result.output == content
    assert tool_event["payload"]["output"] == {
        "content_preview": content[:500],
        "content_chars": len(content),
        "truncated": True,
    }
    assert content not in json.dumps(tool_event["payload"], ensure_ascii=False)


def test_deciding_unknown_approval_raises_not_found(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")

    try:
        gateway.audit_store.decide_approval(999, "approved", "reviewer")
    except ApprovalNotFoundError as exc:
        assert "Approval not found: 999" in str(exc)
    else:
        raise AssertionError("deciding an unknown approval should raise ApprovalNotFoundError")
