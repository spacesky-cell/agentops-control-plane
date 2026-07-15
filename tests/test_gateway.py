import hashlib
import json
import os
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agentpermit.agents import ScriptedAgent
from agentpermit.audit import ApprovalNotFoundError
from agentpermit.gateway import RuntimeGateway
from agentpermit.models import ToolRequest


def make_sample_repo(root: Path) -> Path:
    source = root / "sample_repo"
    source.mkdir()
    (source / "math_utils.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8"
    )
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
                "args": {
                    "path": "math_utils.py",
                    "old": "return a - b",
                    "new": "return a + b",
                },
            },
            {
                "tool": "run_command",
                "args": {"program": "python", "args": ["-m", "unittest", "-q"]},
            },
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
    command_event = next(
        event
        for event in events
        if event["type"] == "tool_executed" and event["tool_name"] == "run_command"
    )
    assert command_event["payload"]["args"] == {
        "program": "python",
        "args": [
            "-m",
            {
                "content_chars": 8,
                "content_sha256": hashlib.sha256(b"unittest").hexdigest(),
            },
            "-q",
        ],
    }
    assert command_event["payload"]["output"]["program"] == "python"
    assert command_event["payload"]["output"]["args"][0] == "-m"
    assert command_event["payload"]["output"]["args"][1]["content_chars"] == 8
    assert command_event["payload"]["output"]["args"][2] == "-q"
    assert set(command_event["payload"]["output"]["output"]) == {
        "content_chars",
        "content_sha256",
    }


def test_gateway_denies_non_mapping_structured_command_args_without_raising(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    run_id, workspace = gateway.start_run("malformed command", "test-agent")

    for malformed_args in (
        None,
        42,
        "python -m unittest",
        ["python", "-m", "unittest"],
    ):
        result = gateway.execute_tool(
            run_id,
            workspace,
            ToolRequest("run_command", malformed_args),
        )

        assert result.status.value == "denied"
        assert result.decision is not None
        assert result.decision.decision.value == "deny"


def test_scripted_agent_pauses_without_auto_approval(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    agent = ScriptedAgent(
        name="test-agent",
        steps=[
            {
                "tool": "patch_text",
                "args": {
                    "path": "math_utils.py",
                    "old": "return a - b",
                    "new": "return a + b",
                },
            },
            {
                "tool": "run_command",
                "args": {"program": "python", "args": ["-m", "unittest", "-q"]},
            },
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
                "args": {
                    "path": "math_utils.py",
                    "old": "return a - b",
                    "new": "return a + b",
                },
            },
            {
                "tool": "run_command",
                "args": {"program": "python", "args": ["-m", "unittest", "-q"]},
            },
        ],
    )

    run_id = agent.run(gateway, "fix sample repo", source=source, auto_approve=False)
    approval = gateway.audit_store.list_approvals(run_id)[0]
    gateway.audit_store.decide_approval(
        approval["id"], "approved", "reviewer", "Looks safe"
    )
    agent.resume(gateway, run_id, approver="reviewer")
    run = gateway.audit_store.get_run(run_id)
    events = gateway.audit_store.get_events(run_id)

    assert run["status"] == "success"
    assert any(event["type"] == "approval_used" for event in events)


def test_resume_fails_closed_if_run_finishes_after_approval_read(tmp_path, monkeypatch):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    agent = ScriptedAgent(
        name="test-agent",
        steps=[
            {
                "tool": "write_file",
                "args": {"path": "notes.txt", "content": "approved"},
            }
        ],
    )
    run_id = agent.run(gateway, "resume race", auto_approve=False)
    approval = gateway.audit_store.list_approvals(run_id)[0]
    gateway.audit_store.decide_approval(approval["id"], "approved", "reviewer")

    original_resume = gateway.audit_store.resume_run

    def finish_before_resume(candidate: str) -> bool:
        gateway.audit_store.finish_run(candidate, "failed", message="race winner")
        return original_resume(candidate)

    monkeypatch.setattr(gateway.audit_store, "resume_run", finish_before_resume)
    with pytest.raises(ValueError, match="not waiting for approval"):
        agent.resume(gateway, run_id, approver="reviewer")

    run = gateway.audit_store.get_run(run_id)
    assert run["status"] == "failed"
    assert not (Path(run["workspace_path"]) / "notes.txt").exists()
    assert (
        len(
            [
                event
                for event in gateway.audit_store.get_events(run_id)
                if event["type"] == "run_finished"
            ]
        )
        == 1
    )


def test_concurrent_resume_allows_only_one_execution(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    agent = ScriptedAgent(
        name="test-agent",
        steps=[
            {
                "tool": "write_file",
                "args": {"path": "notes.txt", "content": "approved"},
            }
        ],
    )
    run_id = agent.run(gateway, "concurrent resume", auto_approve=False)
    approval = gateway.audit_store.list_approvals(run_id)[0]
    gateway.audit_store.decide_approval(approval["id"], "approved", "reviewer")

    def resume_once(_index: int):
        try:
            agent.resume(gateway, run_id, approver="reviewer")
            return "ok"
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(resume_once, range(2)))

    assert results.count("ok") == 1
    assert any(
        "not waiting for approval" in result for result in results if result != "ok"
    )
    assert gateway.audit_store.get_run(run_id)["status"] == "success"
    assert (
        len(
            [
                event
                for event in gateway.audit_store.get_events(run_id)
                if event["type"] == "run_finished"
            ]
        )
        == 1
    )


def test_duplicate_finish_preserves_winning_snapshot_and_cleans_loser(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    run_id, workspace = gateway.start_run("immutable snapshot", "test-agent")
    (workspace / "state.txt").write_text("state-one", encoding="utf-8")

    gateway.finish_run(run_id, workspace, "success")
    events = gateway.audit_store.get_events(run_id)
    finished = [event for event in events if event["type"] == "run_finished"]
    winner_snapshot = Path(finished[0]["payload"]["snapshot"])
    with zipfile.ZipFile(winner_snapshot) as archive:
        assert archive.read("state.txt") == b"state-one"

    (workspace / "state.txt").write_text("state-two", encoding="utf-8")
    gateway.finish_run(run_id, workspace, "failed")

    events = gateway.audit_store.get_events(run_id)
    finished = [event for event in events if event["type"] == "run_finished"]
    assert gateway.audit_store.get_run(run_id)["status"] == "success"
    assert len(finished) == 1
    assert Path(finished[0]["payload"]["snapshot"]) == winner_snapshot
    with zipfile.ZipFile(winner_snapshot) as archive:
        assert archive.read("state.txt") == b"state-one"
    assert not any(
        path.is_file()
        and path != winner_snapshot
        and path.name.startswith(f"{run_id}-after-")
        for path in gateway.workspace_manager.snapshots_dir.iterdir()
    )


def test_concurrent_gateway_finish_keeps_only_winning_snapshot(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    run_id, workspace = gateway.start_run("concurrent finish", "test-agent")
    (workspace / "state.txt").write_text("stable", encoding="utf-8")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda index: gateway.finish_run(
                    run_id, workspace, "success" if index % 2 == 0 else "failed"
                ),
                range(8),
            )
        )

    finished = [
        event
        for event in gateway.audit_store.get_events(run_id)
        if event["type"] == "run_finished"
    ]
    after_snapshots = list(
        gateway.workspace_manager.snapshots_dir.glob(f"{run_id}-after-*.zip")
    )
    assert results.count(True) == 1
    assert results.count(False) == 7
    assert len(finished) == 1
    assert after_snapshots == [Path(finished[0]["payload"]["snapshot"])]
    with zipfile.ZipFile(after_snapshots[0]) as archive:
        assert archive.read("state.txt") == b"stable"


def test_scripted_agent_does_not_resume_after_rejection(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    agent = ScriptedAgent(
        name="test-agent",
        steps=[
            {
                "tool": "patch_text",
                "args": {
                    "path": "math_utils.py",
                    "old": "return a - b",
                    "new": "return a + b",
                },
            },
            {
                "tool": "run_command",
                "args": {"program": "python", "args": ["-m", "unittest", "-q"]},
            },
        ],
    )

    run_id = agent.run(gateway, "fix sample repo", source=source, auto_approve=False)
    approval = gateway.audit_store.list_approvals(run_id)[0]
    gateway.audit_store.decide_approval(
        approval["id"], "rejected", "reviewer", "Too risky"
    )

    try:
        agent.resume(gateway, run_id, approver="reviewer")
    except ValueError as exc:
        assert "not waiting for approval: failed" in str(exc)
    else:
        raise AssertionError(
            "resume should reject runs without an approved pending action"
        )

    run = gateway.audit_store.get_run(run_id)
    assert run["status"] == "failed"


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
                "args": {
                    "path": "math_utils.py",
                    "old": "return a - b",
                    "new": "return a + b",
                },
            },
            {
                "tool": "patch_text",
                "args": {
                    "path": "math_utils.py",
                    "old": "return a + b",
                    "new": "return a - b",
                },
            },
        ],
    )

    run_id = agent.run(gateway, "two approvals", source=source, auto_approve=False)
    first_approval = gateway.audit_store.list_approvals(run_id)[0]
    gateway.audit_store.decide_approval(
        first_approval["id"], "approved", "reviewer", "First patch"
    )
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
        raise AssertionError(
            "resume should require approval for the current pending action"
        )

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

    run_id = original_agent.run(
        gateway, "write notes", source=source, auto_approve=False
    )
    approval = gateway.audit_store.list_approvals(run_id)[0]
    gateway.audit_store.decide_approval(
        approval["id"], "approved", "reviewer", "Original write"
    )

    try:
        changed_agent.resume(gateway, run_id, approver="reviewer")
    except ValueError as exc:
        assert "approved pending action" in str(exc)
    else:
        raise AssertionError(
            "resume should reject an approval for a different full request"
        )

    workspace = Path(gateway.audit_store.get_run(run_id)["workspace_path"])
    assert not (workspace / "notes.txt").exists()


def test_snapshots_include_workspace_files(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")

    run_id, workspace = gateway.start_run("snapshot sample repo", "test-agent", source)
    before_snapshot = (
        tmp_path / "project" / ".agentpermit" / "snapshots" / f"{run_id}-before.zip"
    )

    with zipfile.ZipFile(before_snapshot) as archive:
        assert "math_utils.py" in archive.namelist()
        assert "test_math_utils.py" in archive.namelist()

    gateway.finish_run(run_id, workspace, "success")
    finished = next(
        event
        for event in gateway.audit_store.get_events(run_id)
        if event["type"] == "run_finished"
    )
    after_snapshot = Path(finished["payload"]["snapshot"])

    with zipfile.ZipFile(after_snapshot) as archive:
        assert "math_utils.py" in archive.namelist()
        assert "test_math_utils.py" in archive.namelist()


def test_audit_store_records_schema_version(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")

    assert gateway.audit_store.get_schema_version() == 2


def test_audit_finish_run_is_atomic_and_idempotent_under_concurrency(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    run_id, _workspace = gateway.start_run("atomic finish", "test-agent")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _index: gateway.audit_store.finish_run(run_id, "success"),
                range(8),
            )
        )

    assert results.count(True) == 1
    assert results.count(False) == 7
    assert gateway.audit_store.finish_run(run_id, "failed") is False

    run = gateway.audit_store.get_run(run_id)
    events = gateway.audit_store.get_events(run_id)
    assert run["status"] == "success"
    assert len([event for event in events if event["type"] == "run_finished"]) == 1


def test_started_run_persists_authoritative_workspace_identity(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")

    run_id, workspace = gateway.start_run("identity run", "test-agent")
    run = gateway.audit_store.get_run(run_id)

    assert run["workspace_path"] == str(workspace)
    assert json.loads(run["workspace_identity"]) == list(
        gateway.workspace_manager.workspace_identity(workspace)
    )


def test_fresh_gateway_rejects_replaced_workspace_root(tmp_path):
    home = tmp_path / "project"
    gateway = RuntimeGateway.from_home(home)
    run_id, workspace = gateway.start_run("fresh identity", "test-agent")
    (workspace / "safe.txt").write_text("ordinary", encoding="utf-8")
    original = workspace.with_name(f"{workspace.name}-original")
    replacement = workspace.with_name(f"{workspace.name}-replacement")
    replacement.mkdir()
    (replacement / "safe.txt").write_text("replacement-secret", encoding="utf-8")
    os.replace(workspace, original)
    os.replace(replacement, workspace)
    fresh = RuntimeGateway.from_home(home)

    with pytest.raises(ValueError, match="authoritative workspace identity"):
        fresh.execute_tool(
            run_id, workspace, ToolRequest("read_file", {"path": "safe.txt"})
        )

    assert (workspace / "safe.txt").read_text(encoding="utf-8") == "replacement-secret"


def test_resume_fails_closed_when_running_row_has_no_workspace_identity(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    workspace = gateway.workspace_manager.create("run_legacy_identity")
    run_id = gateway.audit_store.start_run("legacy identity", "test-agent", workspace)

    with pytest.raises(ValueError, match="authoritative workspace identity"):
        gateway.resume_workspace(run_id)


def test_start_run_copy_failure_marks_failed_and_removes_partial_workspace(
    tmp_path, monkeypatch
):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    original_copy = gateway.workspace_manager._copy_source

    def copy_then_fail(source_path, workspace):
        original_copy(source_path, workspace)
        raise RuntimeError("injected copy failure")

    monkeypatch.setattr(gateway.workspace_manager, "_copy_source", copy_then_fail)

    with pytest.raises(RuntimeError, match="injected copy failure"):
        gateway.start_run("copy failure", "test-agent", source)

    run = gateway.audit_store.list_runs()[0]
    assert run["status"] == "failed"
    assert run["workspace_path"] != "."
    assert list(gateway.workspace_manager.workspaces_dir.iterdir()) == []


def test_start_run_create_failure_after_root_mkdir_removes_partial_workspace(
    tmp_path, monkeypatch
):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    original_stat = gateway.workspace_manager._stat_child
    target_stats = 0

    def fail_after_root_mkdir(parent, name):
        nonlocal target_stats
        if name.startswith("run_"):
            target_stats += 1
            if target_stats == 3:
                raise RuntimeError("injected create failure")
        return original_stat(parent, name)

    monkeypatch.setattr(gateway.workspace_manager, "_stat_child", fail_after_root_mkdir)

    with pytest.raises(RuntimeError, match="injected create failure"):
        gateway.start_run("create failure", "test-agent")

    run = gateway.audit_store.list_runs()[0]
    assert run["status"] == "failed"
    assert run["workspace_path"] == ""
    assert list(gateway.workspace_manager.workspaces_dir.iterdir()) == []


def test_before_snapshot_failure_marks_failed_and_removes_workspace(
    tmp_path, monkeypatch
):
    gateway = RuntimeGateway.from_home(tmp_path / "project")

    def fail_snapshot(run_id, workspace, label):
        raise RuntimeError("injected before snapshot failure")

    monkeypatch.setattr(gateway.workspace_manager, "snapshot", fail_snapshot)

    with pytest.raises(RuntimeError, match="injected before snapshot failure"):
        gateway.start_run("snapshot failure", "test-agent")

    run = gateway.audit_store.list_runs()[0]
    assert run["status"] == "failed"
    assert run["workspace_path"] != "."
    assert run["workspace_identity"]
    assert not Path(run["workspace_path"]).exists()
    assert list(gateway.workspace_manager.workspaces_dir.iterdir()) == []


def test_audit_store_persists_run_metadata(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    run_id, _workspace = gateway.start_run("metadata run", "test-agent")

    gateway.audit_store.set_run_metadata(
        run_id, {"transport": "mcp", "task": "metadata run"}
    )

    assert gateway.audit_store.get_run_metadata(run_id) == {
        "transport": "mcp",
        "task": "metadata run",
    }


def test_setting_unknown_run_metadata_raises_not_found(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")

    try:
        gateway.audit_store.set_run_metadata("run_missing", {"transport": "mcp"})
    except ValueError as exc:
        assert "Run not found: run_missing" in str(exc)
    else:
        raise AssertionError("setting metadata for an unknown run should fail")


def test_waiting_for_approval_keeps_run_open(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    agent = ScriptedAgent(
        name="test-agent",
        steps=[
            {
                "tool": "patch_text",
                "args": {
                    "path": "math_utils.py",
                    "old": "return a - b",
                    "new": "return a + b",
                },
            }
        ],
    )

    run_id = agent.run(gateway, "approval required", source=source, auto_approve=False)
    run = gateway.audit_store.get_run(run_id)
    events = gateway.audit_store.get_events(run_id)
    after_snapshots = list(
        gateway.workspace_manager.snapshots_dir.glob(f"{run_id}-after-*.zip")
    )

    assert run["status"] == "waiting_for_approval"
    assert run["ended_at"] is None
    assert not after_snapshots
    assert not any(event["type"] == "run_finished" for event in events)


def test_read_file_audit_uses_hash_not_full_content(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    content = "x" * 900
    (source / "notes.txt").write_text(content, encoding="utf-8")
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    run_id, workspace = gateway.start_run("read audit", "test-agent", source)

    result = gateway.execute_tool(
        run_id, workspace, ToolRequest("read_file", {"path": "notes.txt"})
    )
    events = gateway.audit_store.get_events(run_id)
    tool_event = [event for event in events if event["type"] == "tool_executed"][-1]

    assert result.output == content
    assert tool_event["payload"]["output"] == {
        "content_chars": len(content),
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }
    assert content not in json.dumps(tool_event["payload"], ensure_ascii=False)


def test_deciding_unknown_approval_raises_not_found(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")

    try:
        gateway.audit_store.decide_approval(999, "approved", "reviewer")
    except ApprovalNotFoundError as exc:
        assert "Approval not found: 999" in str(exc)
    else:
        raise AssertionError(
            "deciding an unknown approval should raise ApprovalNotFoundError"
        )


def test_deciding_non_pending_approval_raises_conflict(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    run_id = gateway.audit_store.start_run(
        "approval state", "test-agent", tmp_path / "workspace"
    )
    approval_id = gateway.audit_store.create_approval(
        run_id,
        "patch_text",
        {"args": {"path": "math_utils.py"}},
        "Patch approval.",
    )
    gateway.audit_store.decide_approval(approval_id, "rejected", "reviewer", "No")

    try:
        gateway.audit_store.decide_approval(
            approval_id, "approved", "reviewer", "Changed mind"
        )
    except ValueError as exc:
        assert "not pending" in str(exc)
    else:
        raise AssertionError("deciding a non-pending approval should fail")

    approval = gateway.audit_store.list_approvals(run_id)[0]
    assert approval["status"] == "rejected"


def test_gateway_rejects_missing_and_non_authoritative_workspaces(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    with pytest.raises(ValueError, match="Run not found"):
        gateway.resume_workspace("missing")
    run_id, workspace = gateway.start_run("task", "agent")
    with pytest.raises(ValueError, match="does not match authoritative"):
        gateway._verified_workspace(run_id, tmp_path / "other")
    assert gateway._verified_workspace(run_id, workspace) == workspace


def test_gateway_records_cleanup_failure_when_startup_fails(tmp_path, monkeypatch):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(
        gateway.workspace_manager, "create", lambda run_id, source: workspace
    )
    monkeypatch.setattr(
        gateway.workspace_manager, "workspace_identity", lambda path: (1, 2)
    )
    monkeypatch.setattr(
        gateway.audit_store,
        "activate_run_workspace",
        lambda *args: (_ for _ in ()).throw(RuntimeError("activation failed")),
    )
    monkeypatch.setattr(
        gateway.workspace_manager,
        "remove_workspace",
        lambda *args: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
    )
    with pytest.raises(RuntimeError, match="activation failed"):
        gateway.start_run("task", "agent")
