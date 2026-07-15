import json
import os
import sqlite3
import subprocess
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

import agentpermit.workspace as workspace_module
from agentpermit.agents import ScriptedAgent
from agentpermit.audit import AuditStore
from agentpermit.gateway import RuntimeGateway
from agentpermit.mcp_stdio import McpStdioSession
from agentpermit.models import ToolRequest, ToolStatus


def make_source(root: Path) -> Path:
    source = root / "source"
    source.mkdir()
    (source / "app.py").write_text("print('safe')\n", encoding="utf-8")
    return source


def make_symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this platform.")


def make_directory_alias(link: Path, target: Path) -> None:
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise OSError(completed.stderr or completed.stdout)
    else:
        link.symlink_to(target, target_is_directory=True)


def test_mcp_client_cannot_enable_auto_approval(tmp_path):
    source = make_source(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(
        gateway, source=source, task="write", agent_name="untrusted-mcp"
    )
    session.handle(
        {"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}}
    )
    session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    response = session.handle(
        {
            "jsonrpc": "2.0",
            "id": "write",
            "method": "tools/call",
            "params": {
                "name": "write_file",
                "arguments": {"path": "created.txt", "content": "must wait"},
                "auto_approve": True,
            },
        }
    )

    run_id = session.run_id
    assert run_id is not None
    workspace = Path(gateway.audit_store.get_run(run_id)["workspace_path"])
    assert response["result"]["isError"] is True
    assert "pending_approval" in response["result"]["content"][0]["text"]
    assert not (workspace / "created.txt").exists()
    assert gateway.audit_store.list_approvals(run_id)[0]["status"] == "pending"


def test_identical_pending_requests_reuse_the_same_approval(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    run_id, workspace = gateway.start_run("pending reuse", "test-agent")
    request = ToolRequest("write_file", {"path": "created.txt", "content": "same"})

    first = gateway.execute_tool(run_id, workspace, request)
    second = gateway.execute_tool(run_id, workspace, request)

    assert first.status == ToolStatus.PENDING_APPROVAL
    assert second.status == ToolStatus.PENDING_APPROVAL
    assert second.approval_id == first.approval_id
    assert len(gateway.audit_store.list_approvals(run_id)) == 1


def test_concurrent_resumes_consume_one_approved_row_and_execute_once(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    run_id, workspace = gateway.start_run("consume race", "test-agent")
    request = ToolRequest(
        "write_file", {"path": "created.txt", "content": "one execution"}
    )
    pending = gateway.execute_tool(run_id, workspace, request)
    gateway.audit_store.decide_approval(
        pending.approval_id, "approved", "reviewer", "safe"
    )
    workers = 12
    barrier = Barrier(workers)

    def resume():
        barrier.wait()
        return gateway.execute_tool(
            run_id, workspace, request, preapproved_by="reviewer"
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda _index: resume(), range(workers)))

    assert sum(result.status == ToolStatus.OK for result in results) == 1
    losing_ids = {
        result.approval_id
        for result in results
        if result.status == ToolStatus.PENDING_APPROVAL
    }
    assert len(losing_ids) == 1
    assert None not in losing_ids
    events = gateway.audit_store.get_events(run_id)
    assert sum(event["type"] == "tool_executed" for event in events) == 1
    original = next(
        approval
        for approval in gateway.audit_store.list_approvals(run_id)
        if approval["id"] == pending.approval_id
    )
    assert original["status"] == "consumed"
    assert len(gateway.audit_store.list_approvals(run_id)) == 2


def test_rejected_matching_approval_is_terminal(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    run_id, workspace = gateway.start_run("reject", "test-agent")
    request = ToolRequest("write_file", {"path": "created.txt", "content": "denied"})
    pending = gateway.execute_tool(run_id, workspace, request)
    gateway.audit_store.decide_approval(
        pending.approval_id, "rejected", "reviewer", "unsafe"
    )

    with pytest.raises(ValueError, match="terminal.*failed"):
        gateway.execute_tool(run_id, workspace, request, preapproved_by="reviewer")

    assert not (workspace / "created.txt").exists()
    assert len(gateway.audit_store.list_approvals(run_id)) == 1


def test_rejection_before_pause_keeps_run_failed(tmp_path):
    store = AuditStore(tmp_path / "audit.db")
    run_id = store.start_run(
        "reject before pause", "test-agent", tmp_path / "workspace"
    )
    approval_id = store.create_approval(
        run_id,
        "write_file",
        {"args": {"path": "created.txt", "content": "denied"}},
        "Approval required.",
    )

    store.decide_approval(approval_id, "rejected", "reviewer", "unsafe")
    store.pause_run(run_id)

    approval = store.get_approval(approval_id)
    run = store.get_run(run_id)
    finished = [
        event for event in store.get_events(run_id) if event["type"] == "run_finished"
    ]
    assert approval and approval["status"] == "rejected"
    assert run and run["status"] == "failed"
    assert run["ended_at"] is not None
    assert len(finished) == 1


def test_rejection_after_pause_finishes_run_once(tmp_path):
    store = AuditStore(tmp_path / "audit.db")
    run_id = store.start_run("reject after pause", "test-agent", tmp_path / "workspace")
    approval_id = store.create_approval(
        run_id,
        "write_file",
        {"args": {"path": "created.txt", "content": "denied"}},
        "Approval required.",
    )

    store.pause_run(run_id)
    store.decide_approval(approval_id, "rejected", "reviewer", "unsafe")

    approval = store.get_approval(approval_id)
    run = store.get_run(run_id)
    finished = [
        event for event in store.get_events(run_id) if event["type"] == "run_finished"
    ]
    assert approval and approval["status"] == "rejected"
    assert run and run["status"] == "failed"
    assert run["ended_at"] is not None
    assert len(finished) == 1


def test_approval_before_pause_keeps_run_waiting_with_pause_event(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    run_id, _workspace = gateway.start_run("approve before pause", "test-agent")
    approval_id = gateway.audit_store.create_approval(
        run_id,
        "write_file",
        {"args": {"path": "created.txt", "content": "approved"}},
        "Approval required.",
    )

    gateway.audit_store.decide_approval(approval_id, "approved", "reviewer", "safe")
    gateway.pause_run(run_id)

    run = gateway.audit_store.get_run(run_id)
    assert run and run["status"] == "waiting_for_approval"
    assert (
        len(
            [
                event
                for event in gateway.audit_store.get_events(run_id)
                if event["type"] == "run_paused"
            ]
        )
        == 1
    )


def test_auto_approval_is_audited_and_consumed_before_execution(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    run_id, workspace = gateway.start_run("trusted auto", "server-adapter")

    result = gateway.execute_tool(
        run_id,
        workspace,
        ToolRequest("write_file", {"path": "created.txt", "content": "trusted"}),
        auto_approve=True,
    )

    assert result.status == ToolStatus.OK
    approvals = gateway.audit_store.list_approvals(run_id)
    assert len(approvals) == 1
    assert approvals[0]["status"] == "consumed"
    assert approvals[0]["approver"] == "auto-approve"
    assert approvals[0]["reviewer_reason"] == "Trusted server-side auto approval."


def test_protected_files_are_excluded_from_copy_listing_and_snapshots(tmp_path):
    source = make_source(tmp_path)
    (source / ".env").write_text("API_TOKEN=copy-marker", encoding="utf-8")
    (source / "service_token.txt").write_text("copy-marker", encoding="utf-8")
    (source / "credentials.json").write_text(
        '{"password":"copy-marker"}', encoding="utf-8"
    )
    (source / ".npmrc").write_text(
        "//registry.example/:_authToken=copy-marker", encoding="utf-8"
    )
    (source / "id_ed25519").write_text("copy-marker", encoding="utf-8")
    nested = source / "nested"
    nested.mkdir()
    (nested / ".env.production").write_text("SECRET=copy-marker", encoding="utf-8")
    gateway = RuntimeGateway.from_home(tmp_path / "project")

    run_id, workspace = gateway.start_run("protected copy", "test-agent", source)
    listed = gateway.tool_executor.list_files(workspace)

    assert (workspace / "app.py").exists()
    assert not (workspace / ".env").exists()
    assert not (workspace / "service_token.txt").exists()
    assert not (workspace / "credentials.json").exists()
    assert not (workspace / ".npmrc").exists()
    assert not (workspace / "id_ed25519").exists()
    assert not (workspace / "nested" / ".env.production").exists()
    assert listed == ["app.py"]
    before = (
        tmp_path / "project" / ".agentpermit" / "snapshots" / f"{run_id}-before.zip"
    )
    with zipfile.ZipFile(before) as archive:
        assert archive.namelist() == ["app.py"]

    (workspace / ".env").write_text("API_TOKEN=snapshot-marker", encoding="utf-8")
    after = gateway.workspace_manager.snapshot(run_id, workspace, "after-security")
    with zipfile.ZipFile(after) as archive:
        assert ".env" not in archive.namelist()


def test_all_durable_events_and_approvals_are_secret_free(tmp_path):
    source = make_source(tmp_path)
    write_marker = "WRITE_MARKER_SHOULD_NEVER_PERSIST"
    credential_marker = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
    camel_marker = "CAMEL_SECRET_MARKER_SHOULD_NEVER_PERSIST"
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    agent = ScriptedAgent(
        name="secret-test",
        steps=[
            {
                "tool": "write_file",
                "args": {
                    "path": "created.txt",
                    "content": write_marker,
                    "api_token": credential_marker,
                    "nested": {"clientSecret": camel_marker},
                },
            }
        ],
    )

    run_id = agent.run(gateway, "redact", source=source, auto_approve=False)
    durable = json.dumps(
        {
            "events": gateway.audit_store.get_events(run_id),
            "approvals": gateway.audit_store.list_approvals(run_id),
        },
        ensure_ascii=False,
    )

    assert write_marker not in durable
    assert credential_marker not in durable
    assert camel_marker not in durable
    assert "content_sha256" in durable
    assert "[redacted]" in durable


def test_run_identity_and_approval_approver_are_redacted_before_db_writes(tmp_path):
    task_credential = "ghp_0123456789abcdefghijklmnopqrstuv"
    agent_credential = "sk-0123456789abcdefghijklmnop"
    approver_credential = "AKIA0123456789ABCDEF"
    store = AuditStore(tmp_path / "runs.sqlite")

    run_id = store.start_run(
        f"Deploy the ordinary task with {task_credential}",
        f"ordinary-agent {agent_credential}",
        tmp_path / "workspace",
    )
    approval_id = store.create_approval(
        run_id,
        "write_file",
        {"args": {"path": "safe.txt"}},
        "File writes require approval by policy.",
    )
    store.decide_approval(
        approval_id,
        "approved",
        f"ordinary-reviewer {approver_credential}",
    )

    run = store.get_run(run_id)
    approval = store.list_approvals(run_id)[0]
    assert run["task"] == "Deploy the ordinary task with [redacted]"
    assert run["agent_name"] == "ordinary-agent [redacted]"
    assert approval["approver"] == "ordinary-reviewer [redacted]"


def test_source_copy_and_snapshots_skip_symlink_aliases(tmp_path):
    source = make_source(tmp_path)
    protected = source / ".env"
    protected.write_text("SECRET=symlink-marker", encoding="utf-8")
    try:
        (source / "config-link").symlink_to(protected)
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this platform.")

    gateway = RuntimeGateway.from_home(tmp_path / "project")
    run_id, workspace = gateway.start_run("symlink protection", "test-agent", source)

    assert not (workspace / "config-link").exists()
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("symlink-marker", encoding="utf-8")
    (workspace / "snapshot-link").symlink_to(outside)
    snapshot = gateway.workspace_manager.snapshot(run_id, workspace, "symlink-security")
    with zipfile.ZipFile(snapshot) as archive:
        assert "snapshot-link" not in archive.namelist()


def test_workspace_symlink_alias_to_protected_file_is_denied_by_policy(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    run_id, workspace = gateway.start_run("symlink policy", "test-agent")
    protected = workspace / ".env"
    protected.write_text("SECRET=original", encoding="utf-8")
    make_symlink_or_skip(workspace / "settings.txt", Path(".env"))

    for request in (
        ToolRequest("read_file", {"path": "settings.txt"}),
        ToolRequest("write_file", {"path": "settings.txt", "content": "changed"}),
        ToolRequest(
            "patch_text",
            {"path": "settings.txt", "old": "original", "new": "changed"},
        ),
    ):
        result = gateway.execute_tool(run_id, workspace, request)
        assert result.status == ToolStatus.DENIED

    assert protected.read_text(encoding="utf-8") == "SECRET=original"


def test_workspace_owner_rejects_protected_symlink_alias_and_listing_omits_it(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    _run_id, workspace = gateway.start_run("symlink owner", "test-agent")
    protected = workspace / ".env"
    protected.write_text("SECRET=original", encoding="utf-8")
    alias = workspace / "settings.txt"
    make_symlink_or_skip(alias, Path(".env"))

    assert "settings.txt" not in gateway.tool_executor.list_files(workspace)
    with pytest.raises(ValueError, match="Protected"):
        gateway.tool_executor.read_file(workspace, "settings.txt")
    with pytest.raises(ValueError, match="Protected"):
        gateway.tool_executor.write_file(workspace, "settings.txt", "SECRET=changed")
    with pytest.raises(ValueError, match="Protected"):
        gateway.tool_executor.patch_text(
            workspace, "settings.txt", "original", "changed"
        )

    assert alias.is_symlink()
    assert protected.read_text(encoding="utf-8") == "SECRET=original"


@pytest.mark.parametrize("operation", ["read", "write", "patch"])
def test_workspace_file_operations_reject_target_replacement_after_validation(
    tmp_path, monkeypatch, operation
):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    _run_id, workspace = gateway.start_run("target replacement", "test-agent")
    target = workspace / "safe.txt"
    target.write_text("ordinary", encoding="utf-8")
    protected = workspace / ".env"
    protected.write_text("protected-secret", encoding="utf-8")
    replacement = tmp_path / "target-replacement.txt"
    os.link(protected, replacement)
    original_open = workspace_module.os.open
    swapped = False

    def swap_before_open(path, flags, *args, **kwargs):
        nonlocal swapped
        targets_file = Path(path) == target or (
            kwargs.get("dir_fd") is not None and Path(path) == Path(target.name)
        )
        if targets_file and not swapped:
            os.replace(replacement, target)
            swapped = True
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(workspace_module.os, "open", swap_before_open)

    with pytest.raises(ValueError, match="changed during access"):
        if operation == "read":
            gateway.tool_executor.read_file(workspace, "safe.txt")
        elif operation == "write":
            gateway.tool_executor.write_file(workspace, "safe.txt", "changed")
        else:
            gateway.tool_executor.patch_text(
                workspace, "safe.txt", "protected", "changed"
            )

    assert swapped
    assert protected.read_text(encoding="utf-8") == "protected-secret"


def test_workspace_new_file_rejects_parent_replacement_after_validation(
    tmp_path, monkeypatch
):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    _run_id, workspace = gateway.start_run("parent replacement", "test-agent")
    parent = workspace / "nested"
    parent.mkdir()
    original_parent = workspace / "nested-original"
    replacement_parent = workspace / "nested-replacement"
    replacement_parent.mkdir()
    target = parent / "new.txt"
    original_open = workspace_module.os.open
    rename_blocked = False

    def swap_parent_before_open(path, flags, *args, **kwargs):
        nonlocal rename_blocked
        targets_file = Path(path) == target or (
            kwargs.get("dir_fd") is not None and Path(path) == Path(target.name)
        )
        if targets_file and parent.exists():
            try:
                os.replace(parent, original_parent)
                os.replace(replacement_parent, parent)
            except OSError:
                rename_blocked = True
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(workspace_module.os, "open", swap_parent_before_open)

    if os.name == "nt":
        result = gateway.tool_executor.write_file(
            workspace, "nested/new.txt", "sensitive"
        )
        assert rename_blocked
        assert result["created"] is True
        assert (parent / "new.txt").read_text(encoding="utf-8") == "sensitive"
    else:
        with pytest.raises(ValueError, match="Directory changed during access"):
            gateway.tool_executor.write_file(workspace, "nested/new.txt", "sensitive")
        assert not rename_blocked
        assert not (parent / "new.txt").exists()
    assert not (original_parent / "new.txt").exists()
    assert not (replacement_parent / "new.txt").exists()


def test_workspace_missing_parent_rejects_concurrent_alias_insertion(
    tmp_path, monkeypatch
):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    _run_id, workspace = gateway.start_run("missing parent race", "test-agent")
    missing_parent = workspace / "missing"
    outside = tmp_path / "outside"
    outside.mkdir()
    original_mkdir = workspace_module.os.mkdir
    inserted = False

    def insert_alias_before_mkdir(path, mode=0o777, *args, **kwargs):
        nonlocal inserted
        targets_parent = Path(path) == missing_parent or (
            kwargs.get("dir_fd") is not None and Path(path) == Path(missing_parent.name)
        )
        if targets_parent and not inserted:
            make_directory_alias(missing_parent, outside)
            inserted = True
        return original_mkdir(path, mode, *args, **kwargs)

    monkeypatch.setattr(workspace_module.os, "mkdir", insert_alias_before_mkdir)

    with pytest.raises(ValueError, match="changed during access"):
        gateway.tool_executor.write_file(
            workspace, "missing/new.txt", "must-not-escape"
        )

    assert inserted
    assert not (outside / "new.txt").exists()


def test_workspace_rejects_registered_root_replacement(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    _run_id, workspace = gateway.start_run("root replacement", "test-agent")
    (workspace / "safe.txt").write_text("ordinary", encoding="utf-8")
    assert gateway.tool_executor.read_file(workspace, "safe.txt") == "ordinary"
    original_root = workspace.with_name(f"{workspace.name}-original")
    replacement_root = workspace.with_name(f"{workspace.name}-replacement")
    replacement_root.mkdir()
    (replacement_root / "safe.txt").write_text("root-secret", encoding="utf-8")
    os.replace(workspace, original_root)
    os.replace(replacement_root, workspace)

    with pytest.raises(ValueError, match="Workspace root changed"):
        gateway.tool_executor.read_file(workspace, "safe.txt")


def test_workspace_rejects_reparse_root_alias(tmp_path):
    manager = workspace_module.WorkspaceManager(tmp_path / "project" / ".agentpermit")
    actual = tmp_path / "actual-workspace"
    actual.mkdir()
    (actual / "safe.txt").write_text("alias-secret", encoding="utf-8")
    alias = manager.workspaces_dir / "root-alias"
    make_directory_alias(alias, actual)

    with pytest.raises(ValueError, match="direct workspace root"):
        manager.read_text(alias, "safe.txt")


def test_workspace_create_rejects_unsafe_run_id_before_mutation(tmp_path):
    manager = workspace_module.WorkspaceManager(tmp_path / "project" / ".agentpermit")
    victim = tmp_path / "project" / "victim"
    victim.mkdir(parents=True)
    marker = victim / "marker.txt"
    marker.write_text("must-survive", encoding="utf-8")

    with pytest.raises(ValueError):
        manager.create("../../victim")

    assert marker.read_text(encoding="utf-8") == "must-survive"


def test_workspace_create_rejects_existing_workspace(tmp_path):
    manager = workspace_module.WorkspaceManager(tmp_path / "project" / ".agentpermit")
    workspace = manager.create("run_existing")
    marker = workspace / "marker.txt"
    marker.write_text("must-survive", encoding="utf-8")

    with pytest.raises(FileExistsError):
        manager.create("run_existing")

    assert marker.read_text(encoding="utf-8") == "must-survive"


@pytest.mark.parametrize(
    "relative",
    [".env::$DATA", "id_rsa::$DATA", ".npmrc::$DATA", "ordinary.txt:stream"],
)
def test_workspace_rejects_colon_components_on_every_platform(tmp_path, relative):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    _run_id, workspace = gateway.start_run("stream syntax", "test-agent")
    for name in (".env", "id_rsa", ".npmrc", "ordinary.txt"):
        (workspace / name).write_text("original", encoding="utf-8")

    with pytest.raises(ValueError, match="colon"):
        gateway.workspace_manager.write_text(workspace, relative, "stream-secret")

    assert all(
        (workspace / name).read_text(encoding="utf-8") == "original"
        for name in (".env", "id_rsa", ".npmrc", "ordinary.txt")
    )


def test_list_files_rejects_registered_root_replacement(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    _run_id, workspace = gateway.start_run("list root replacement", "test-agent")
    (workspace / "safe.txt").write_text("ordinary", encoding="utf-8")
    assert gateway.tool_executor.list_files(workspace) == ["safe.txt"]
    original_root = workspace.with_name(f"{workspace.name}-list-original")
    replacement_root = workspace.with_name(f"{workspace.name}-list-replacement")
    replacement_root.mkdir()
    (replacement_root / "leak.txt").write_text("root-secret", encoding="utf-8")
    os.replace(workspace, original_root)
    os.replace(replacement_root, workspace)

    with pytest.raises(ValueError, match="Workspace root changed"):
        gateway.tool_executor.list_files(workspace)


def test_list_files_rejects_reparse_root_alias(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    manager = gateway.workspace_manager
    actual = tmp_path / "actual-list-workspace"
    actual.mkdir()
    (actual / "leak.txt").write_text("alias-secret", encoding="utf-8")
    alias = manager.workspaces_dir / "list-root-alias"
    make_directory_alias(alias, actual)

    with pytest.raises(ValueError, match="direct workspace root"):
        gateway.tool_executor.list_files(alias)


def test_snapshot_rejects_target_replacement_before_archiving(tmp_path, monkeypatch):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    run_id, workspace = gateway.start_run("snapshot replacement", "test-agent")
    target = workspace / "safe.txt"
    target.write_text("ordinary", encoding="utf-8")
    protected = workspace / ".env"
    protected.write_text("snapshot-protected-secret", encoding="utf-8")
    replacement = tmp_path / "snapshot-replacement.txt"
    os.link(protected, replacement)
    original_is_protected = gateway.workspace_manager.is_protected
    swapped = False

    def swap_after_policy_check(relative):
        nonlocal swapped
        result = original_is_protected(relative)
        if str(relative).replace("\\", "/") == "safe.txt" and not swapped:
            os.replace(replacement, target)
            swapped = True
        return result

    monkeypatch.setattr(
        gateway.workspace_manager, "is_protected", swap_after_policy_check
    )

    snapshot = gateway.workspace_manager.snapshots_dir / f"{run_id}-replacement.zip"
    with pytest.raises(ValueError, match="changed during access"):
        gateway.workspace_manager.snapshot(run_id, workspace, "replacement")

    assert swapped
    assert not snapshot.exists()


def test_source_copy_rejects_file_replacement_after_policy_check(tmp_path, monkeypatch):
    manager = workspace_module.WorkspaceManager(tmp_path / "project" / ".agentpermit")
    source = tmp_path / "source-race"
    source.mkdir()
    source_file = source / "app.py"
    source_file.write_text("ordinary", encoding="utf-8")
    replacement = tmp_path / "source-replacement.py"
    replacement.write_text("source-copy-secret", encoding="utf-8")
    original_is_protected = manager.is_protected
    swapped = False

    def swap_after_policy_check(relative):
        nonlocal swapped
        result = original_is_protected(relative)
        if str(relative).replace("\\", "/") == "app.py" and not swapped:
            os.replace(replacement, source_file)
            swapped = True
        return result

    monkeypatch.setattr(manager, "is_protected", swap_after_policy_check)

    with pytest.raises(ValueError, match="Source file changed during copy"):
        manager.create("run_source_race", source)

    copied = manager.workspaces_dir / "run_source_race" / "app.py"
    assert swapped
    assert not copied.exists() or "source-copy-secret" not in copied.read_text(
        encoding="utf-8"
    )


def test_source_copy_holds_ancestor_lease_during_file_open(tmp_path, monkeypatch):
    manager = workspace_module.WorkspaceManager(tmp_path / "project" / ".agentpermit")
    source = tmp_path / "source-ancestor"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (nested / "app.py").write_text("ordinary", encoding="utf-8")
    original_nested = source / "nested-original"
    replacement_nested = source / "nested-replacement"
    replacement_nested.mkdir()
    (replacement_nested / "app.py").write_text("ancestor-secret", encoding="utf-8")
    original_is_protected = manager.is_protected
    rename_blocked = False

    def try_replace_ancestor(relative):
        nonlocal rename_blocked
        result = original_is_protected(relative)
        if str(relative).replace("\\", "/") == "nested/app.py":
            try:
                os.replace(nested, original_nested)
                os.replace(replacement_nested, nested)
            except OSError:
                rename_blocked = True
        return result

    monkeypatch.setattr(manager, "is_protected", try_replace_ancestor)

    workspace = manager.workspaces_dir / "run_source_ancestor"
    try:
        workspace = manager.create("run_source_ancestor", source)
    except workspace_module.WorkspaceIntegrityError as exc:
        assert "changed" in str(exc)

    assert rename_blocked is (os.name == "nt")
    copied = workspace / "nested" / "app.py"
    assert not copied.exists() or "ancestor-secret" not in copied.read_text(
        encoding="utf-8"
    )
    if copied.exists():
        assert copied.read_text(encoding="utf-8") == "ordinary"


@pytest.mark.parametrize(
    ("run_id", "label"),
    [
        ("../victim", "before"),
        ("run_safe", "../victim"),
        ("run:safe", "before"),
        ("run_safe", "before:stream"),
    ],
)
def test_snapshot_rejects_unsafe_filename_components_before_mutation(
    tmp_path, run_id, label
):
    manager = workspace_module.WorkspaceManager(tmp_path / "project" / ".agentpermit")
    workspace = manager.create("run_snapshot_components")
    (workspace / "safe.txt").write_text("ordinary", encoding="utf-8")
    victim = tmp_path / "project" / "victim-before.zip"
    victim.write_bytes(b"must-survive")

    with pytest.raises(ValueError, match="safe filename component"):
        manager.snapshot(run_id, workspace, label)

    assert victim.read_bytes() == b"must-survive"


def test_snapshot_rejects_registered_storage_replacement(tmp_path):
    manager = workspace_module.WorkspaceManager(tmp_path / "project" / ".agentpermit")
    workspace = manager.create("run_snapshot_storage")
    (workspace / "safe.txt").write_text("ordinary", encoding="utf-8")
    original = manager.snapshots_dir.with_name("snapshots-original")
    replacement = manager.snapshots_dir.with_name("snapshots-replacement")
    replacement.mkdir()
    os.replace(manager.snapshots_dir, original)
    os.replace(replacement, manager.snapshots_dir)

    with pytest.raises(ValueError, match="Snapshot storage changed"):
        manager.snapshot("run_snapshot_storage", workspace, "before")

    assert list(manager.snapshots_dir.iterdir()) == []


def test_workspace_manager_rejects_reparse_snapshot_storage(tmp_path):
    base = tmp_path / "project" / ".agentpermit"
    (base / "workspaces").mkdir(parents=True)
    actual = tmp_path / "actual-snapshots"
    actual.mkdir()
    make_directory_alias(base / "snapshots", actual)

    with pytest.raises(ValueError, match="Snapshot storage must be a direct directory"):
        workspace_module.WorkspaceManager(base)


def test_snapshot_rejects_hardlink_destination_without_touching_victim(tmp_path):
    manager = workspace_module.WorkspaceManager(tmp_path / "project" / ".agentpermit")
    workspace = manager.create("run_snapshot_hardlink")
    (workspace / "safe.txt").write_text("ordinary", encoding="utf-8")
    victim = tmp_path / "snapshot-victim.zip"
    victim.write_bytes(b"must-survive")
    destination = manager.snapshots_dir / "run_snapshot_hardlink-before.zip"
    os.link(victim, destination)

    with pytest.raises(ValueError, match="Snapshot destination alias"):
        manager.snapshot("run_snapshot_hardlink", workspace, "before")

    assert victim.read_bytes() == b"must-survive"
    assert destination.read_bytes() == b"must-survive"


def test_snapshot_rejects_symlink_destination_without_touching_victim(tmp_path):
    manager = workspace_module.WorkspaceManager(tmp_path / "project" / ".agentpermit")
    workspace = manager.create("run_snapshot_symlink")
    (workspace / "safe.txt").write_text("ordinary", encoding="utf-8")
    victim = tmp_path / "snapshot-symlink-victim.zip"
    victim.write_bytes(b"must-survive")
    destination = manager.snapshots_dir / "run_snapshot_symlink-before.zip"
    make_symlink_or_skip(destination, victim)

    with pytest.raises(ValueError, match="Snapshot destination alias"):
        manager.snapshot("run_snapshot_symlink", workspace, "before")

    assert victim.read_bytes() == b"must-survive"
    assert destination.is_symlink()


def test_snapshot_failure_preserves_existing_archive_and_removes_temp(
    tmp_path, monkeypatch
):
    manager = workspace_module.WorkspaceManager(tmp_path / "project" / ".agentpermit")
    workspace = manager.create("run_snapshot_cleanup")
    (workspace / "safe.txt").write_text("ordinary", encoding="utf-8")
    destination = manager.snapshot("run_snapshot_cleanup", workspace, "before")
    original = destination.read_bytes()

    def fail_walk(*args, **kwargs):
        raise workspace_module.WorkspaceIntegrityError("injected snapshot failure")

    monkeypatch.setattr(manager, "_walk_workspace", fail_walk)

    with pytest.raises(ValueError, match="injected snapshot failure"):
        manager.snapshot("run_snapshot_cleanup", workspace, "before")

    assert destination.read_bytes() == original
    assert [path.name for path in manager.snapshots_dir.iterdir()] == [destination.name]


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-relative cleanup")
def test_snapshot_failure_after_storage_rename_removes_temp_from_leased_object(
    tmp_path, monkeypatch
):
    manager = workspace_module.WorkspaceManager(tmp_path / "project" / ".agentpermit")
    workspace = manager.create("run_snapshot_posix_cleanup")
    (workspace / "safe.txt").write_text("ordinary", encoding="utf-8")
    moved_storage = manager.snapshots_dir.with_name("snapshots-moved")
    replacement_storage = manager.snapshots_dir.with_name("snapshots-replacement")
    replacement_storage.mkdir()

    def move_storage_then_fail(*args, **kwargs):
        os.replace(manager.snapshots_dir, moved_storage)
        os.replace(replacement_storage, manager.snapshots_dir)
        raise workspace_module.WorkspaceIntegrityError(
            "injected leased cleanup failure"
        )

    monkeypatch.setattr(manager, "_walk_workspace", move_storage_then_fail)

    with pytest.raises(ValueError, match="injected leased cleanup failure"):
        manager.snapshot("run_snapshot_posix_cleanup", workspace, "before")

    assert list(moved_storage.iterdir()) == []
    assert list(manager.snapshots_dir.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-relative promotion")
def test_snapshot_rejects_storage_rename_immediately_before_promotion(
    tmp_path, monkeypatch
):
    manager = workspace_module.WorkspaceManager(tmp_path / "project" / ".agentpermit")
    workspace = manager.create("run_snapshot_pre_replace")
    (workspace / "safe.txt").write_text("ordinary", encoding="utf-8")
    moved_storage = manager.snapshots_dir.with_name("snapshots-moved")
    replacement_storage = manager.snapshots_dir.with_name("snapshots-replacement")
    replacement_storage.mkdir()
    marker = replacement_storage / "unrelated.txt"
    marker.write_text("must-survive", encoding="utf-8")
    original_replace = manager._replace_child

    def move_storage_before_replace(parent, source_name, destination_name, *args):
        os.replace(manager.snapshots_dir, moved_storage)
        os.replace(replacement_storage, manager.snapshots_dir)
        return original_replace(parent, source_name, destination_name, *args)

    monkeypatch.setattr(manager, "_replace_child", move_storage_before_replace)

    with pytest.raises(ValueError, match="Snapshot storage changed"):
        manager.snapshot("run_snapshot_pre_replace", workspace, "before")

    assert list(moved_storage.iterdir()) == []
    assert (manager.snapshots_dir / marker.name).read_text(
        encoding="utf-8"
    ) == "must-survive"


@pytest.mark.skipif(os.name != "nt", reason="Windows delete-share lease")
def test_snapshot_storage_rename_is_blocked_during_promotion(tmp_path, monkeypatch):
    manager = workspace_module.WorkspaceManager(tmp_path / "project" / ".agentpermit")
    workspace = manager.create("run_snapshot_windows_replace")
    (workspace / "safe.txt").write_text("ordinary", encoding="utf-8")
    moved_storage = manager.snapshots_dir.with_name("snapshots-moved")
    original_replace = manager._replace_child
    rename_blocked = False

    def try_storage_rename(parent, source_name, destination_name, *args):
        nonlocal rename_blocked
        try:
            os.replace(manager.snapshots_dir, moved_storage)
        except OSError:
            rename_blocked = True
        return original_replace(parent, source_name, destination_name, *args)

    monkeypatch.setattr(manager, "_replace_child", try_storage_rename)

    snapshot = manager.snapshot("run_snapshot_windows_replace", workspace, "before")

    assert rename_blocked
    with zipfile.ZipFile(snapshot) as archive:
        assert archive.read("safe.txt") == b"ordinary"


@pytest.mark.skipif(os.name == "nt", reason="POSIX deterministic temp replacement")
def test_snapshot_temp_entry_replacement_is_not_promoted_or_deleted(
    tmp_path, monkeypatch
):
    manager = workspace_module.WorkspaceManager(tmp_path / "project" / ".agentpermit")
    workspace = manager.create("run_snapshot_temp_replace")
    (workspace / "safe.txt").write_text("ordinary", encoding="utf-8")
    unrelated = tmp_path / "unrelated-temp-entry"
    unrelated.write_bytes(b"must-survive")
    displaced_temp = tmp_path / "owner-temp-entry"
    original_replace = manager._replace_child
    replacement_path = None

    def replace_temp_before_promotion(parent, source_name, destination_name, *args):
        nonlocal replacement_path
        replacement_path = parent.state.path / source_name
        os.replace(replacement_path, displaced_temp)
        os.replace(unrelated, replacement_path)
        return original_replace(parent, source_name, destination_name, *args)

    monkeypatch.setattr(manager, "_replace_child", replace_temp_before_promotion)

    with pytest.raises(ValueError, match="Snapshot temp entry changed"):
        manager.snapshot("run_snapshot_temp_replace", workspace, "before")

    assert replacement_path is not None
    assert replacement_path.read_bytes() == b"must-survive"
    assert displaced_temp.exists()
    assert not (manager.snapshots_dir / "run_snapshot_temp_replace-before.zip").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX post-promotion storage binding")
def test_snapshot_post_promotion_storage_failure_removes_only_promoted_inode(
    tmp_path, monkeypatch
):
    manager = workspace_module.WorkspaceManager(tmp_path / "project" / ".agentpermit")
    workspace = manager.create("run_snapshot_post_replace")
    (workspace / "safe.txt").write_text("ordinary", encoding="utf-8")
    moved_storage = manager.snapshots_dir.with_name("snapshots-moved")
    replacement_storage = manager.snapshots_dir.with_name("snapshots-replacement")
    replacement_storage.mkdir()
    marker = replacement_storage / "unrelated.txt"
    marker.write_text("must-survive", encoding="utf-8")
    original_replace = manager._replace_child

    def move_storage_after_replace(parent, source_name, destination_name, *args):
        result = original_replace(parent, source_name, destination_name, *args)
        os.replace(manager.snapshots_dir, moved_storage)
        os.replace(replacement_storage, manager.snapshots_dir)
        return result

    monkeypatch.setattr(manager, "_replace_child", move_storage_after_replace)

    with pytest.raises(ValueError, match="Snapshot storage changed"):
        manager.snapshot("run_snapshot_post_replace", workspace, "before")

    assert list(moved_storage.iterdir()) == []
    assert (manager.snapshots_dir / marker.name).read_text(
        encoding="utf-8"
    ) == "must-survive"


def test_snapshot_post_promotion_does_not_delete_unrelated_replacement(
    tmp_path, monkeypatch
):
    manager = workspace_module.WorkspaceManager(tmp_path / "project" / ".agentpermit")
    workspace = manager.create("run_snapshot_post_entry")
    (workspace / "safe.txt").write_text("ordinary", encoding="utf-8")
    unrelated = tmp_path / "unrelated-promoted-entry"
    unrelated.write_bytes(b"must-survive")
    displaced_archive = tmp_path / "owner-promoted-entry"
    original_replace = manager._replace_child
    destination_path = None

    def replace_destination_after_promotion(
        parent, source_name, destination_name, *args
    ):
        nonlocal destination_path
        result = original_replace(parent, source_name, destination_name, *args)
        destination_path = parent.state.path / destination_name
        os.replace(destination_path, displaced_archive)
        os.replace(unrelated, destination_path)
        return result

    monkeypatch.setattr(manager, "_replace_child", replace_destination_after_promotion)

    with pytest.raises(ValueError, match="Snapshot promotion identity changed"):
        manager.snapshot("run_snapshot_post_entry", workspace, "before")

    assert destination_path is not None
    assert destination_path.read_bytes() == b"must-survive"
    assert displaced_archive.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-relative cleanup")
def test_workspace_create_removes_directory_created_after_storage_rename(
    tmp_path, monkeypatch
):
    manager = workspace_module.WorkspaceManager(tmp_path / "project" / ".agentpermit")
    moved_storage = manager.workspaces_dir.with_name("workspaces-moved")
    replacement_storage = manager.workspaces_dir.with_name("workspaces-replacement")
    replacement_storage.mkdir()
    original_mkdir = workspace_module.os.mkdir
    swapped = False

    def swap_storage_before_mkdir(path, mode=0o777, *args, **kwargs):
        nonlocal swapped
        if Path(path) == Path("run_posix_storage") and kwargs.get("dir_fd") is not None:
            os.replace(manager.workspaces_dir, moved_storage)
            os.replace(replacement_storage, manager.workspaces_dir)
            swapped = True
        return original_mkdir(path, mode, *args, **kwargs)

    monkeypatch.setattr(workspace_module.os, "mkdir", swap_storage_before_mkdir)

    with pytest.raises(ValueError, match="Workspace storage changed"):
        manager.create("run_posix_storage")

    assert swapped
    assert not (moved_storage / "run_posix_storage").exists()
    assert not (manager.workspaces_dir / "run_posix_storage").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-relative cleanup")
def test_nested_create_removes_directory_created_after_root_rename(
    tmp_path, monkeypatch
):
    manager = workspace_module.WorkspaceManager(tmp_path / "project" / ".agentpermit")
    workspace = manager.create("run_posix_nested")
    moved_workspace = workspace.with_name("run_posix_nested-moved")
    replacement_workspace = workspace.with_name("run_posix_nested-replacement")
    replacement_workspace.mkdir()
    original_mkdir = workspace_module.os.mkdir
    swapped = False

    def swap_root_before_mkdir(path, mode=0o777, *args, **kwargs):
        nonlocal swapped
        if Path(path) == Path("nested") and kwargs.get("dir_fd") is not None:
            os.replace(workspace, moved_workspace)
            os.replace(replacement_workspace, workspace)
            swapped = True
        return original_mkdir(path, mode, *args, **kwargs)

    monkeypatch.setattr(workspace_module.os, "mkdir", swap_root_before_mkdir)

    with pytest.raises(ValueError, match="changed during access"):
        manager.write_text(workspace, "nested/new.txt", "must-not-remain")

    assert swapped
    assert not (moved_workspace / "nested").exists()
    assert not (workspace / "nested").exists()


def test_source_copy_rejects_regular_file_with_multiple_hardlinks(tmp_path):
    manager = workspace_module.WorkspaceManager(tmp_path / "project" / ".agentpermit")
    source = tmp_path / "hardlink-source"
    source.mkdir()
    protected = source / ".env"
    protected.write_text("SECRET=source", encoding="utf-8")
    os.link(protected, source / "public.txt")

    with pytest.raises(ValueError, match="multiple hardlinks"):
        manager.create("run_source_hardlink", source)

    assert protected.read_text(encoding="utf-8") == "SECRET=source"
    assert not (manager.workspaces_dir / "run_source_hardlink").exists()


def test_workspace_operations_reject_regular_files_with_multiple_hardlinks(tmp_path):
    manager = workspace_module.WorkspaceManager(tmp_path / "project" / ".agentpermit")
    workspace = manager.create("run_workspace_hardlink")
    original = workspace / "ordinary.txt"
    original.write_text("ordinary", encoding="utf-8")
    alias = workspace / "alias.txt"
    os.link(original, alias)

    with pytest.raises(ValueError, match="multiple hardlinks"):
        manager.list_files(workspace)
    with pytest.raises(ValueError, match="multiple hardlinks"):
        manager.read_text(workspace, "ordinary.txt")
    with pytest.raises(ValueError, match="multiple hardlinks"):
        manager.write_text(workspace, "ordinary.txt", "changed")
    with pytest.raises(ValueError, match="multiple hardlinks"):
        manager.patch_text(workspace, "ordinary.txt", "ordinary", "changed")
    with pytest.raises(ValueError, match="multiple hardlinks"):
        manager.snapshot("run_workspace_hardlink", workspace, "before")
    assert original.read_text(encoding="utf-8") == "ordinary"
    assert alias.read_text(encoding="utf-8") == "ordinary"


def test_file_access_rechecks_link_count_after_open(tmp_path, monkeypatch):
    manager = workspace_module.WorkspaceManager(tmp_path / "project" / ".agentpermit")
    workspace = manager.create("run_link_count_race")
    target = workspace / "ordinary.txt"
    target.write_text("ordinary", encoding="utf-8")
    alias = workspace / "late-alias.txt"
    original_open = manager._open_file

    def add_link_after_open(parent, name, path, flags, *, mode=0o777):
        fd = original_open(parent, name, path, flags, mode=mode)
        if Path(path) == target and not alias.exists():
            os.link(target, alias)
        return fd

    monkeypatch.setattr(manager, "_open_file", add_link_after_open)

    with pytest.raises(ValueError, match="multiple hardlinks"):
        manager.read_text(workspace, "ordinary.txt")

    assert alias.exists()
    assert target.read_text(encoding="utf-8") == "ordinary"


def test_open_new_closes_fd_and_preserves_primary_when_cleanup_fails(
    tmp_path, monkeypatch
):
    manager = workspace_module.WorkspaceManager(tmp_path / "project" / ".agentpermit")
    workspace = manager.create("run_new_cleanup_failure")
    original_open = manager._open_file
    opened_fd = None

    def capture_open(parent, name, path, flags, *, mode=0o777):
        nonlocal opened_fd
        opened_fd = original_open(parent, name, path, flags, mode=mode)
        return opened_fd

    def fail_verification(fd, access, *, allow_created=False):
        raise workspace_module.WorkspaceIntegrityError("primary integrity failure")

    def fail_cleanup(access, fd):
        raise RuntimeError("descriptor cleanup failure")

    monkeypatch.setattr(manager, "_open_file", capture_open)
    monkeypatch.setattr(manager, "_verify_open_file", fail_verification)
    monkeypatch.setattr(manager, "_discard_created_file", fail_cleanup)

    error = None
    try:
        manager.write_text(workspace, "new.txt", "sensitive")
    except Exception as exc:  # noqa: BLE001 - assert exact primary and cause below.
        error = exc

    assert opened_fd is not None
    fd_closed = False
    try:
        os.fstat(opened_fd)
    except OSError:
        fd_closed = True
    finally:
        if not fd_closed:
            os.close(opened_fd)
    assert isinstance(error, workspace_module.WorkspaceIntegrityError)
    assert str(error) == "primary integrity failure"
    assert isinstance(error.__cause__, RuntimeError)
    assert str(error.__cause__) == "descriptor cleanup failure"
    assert fd_closed


def test_auto_approve_consumes_human_approval_without_overwriting_provenance(tmp_path):
    store = AuditStore(tmp_path / "runs.sqlite")
    run_id = store.start_run(
        "approval provenance", "test-agent", tmp_path / "workspace"
    )
    fingerprint = "approved-request"
    approval_id = store.create_approval(
        run_id,
        "write_file",
        {"args": {"path": "safe.txt"}},
        "Write approval.",
        fingerprint,
    )
    store.decide_approval(
        approval_id, "approved", "human-reviewer", "Reviewed manually"
    )
    before = store.list_approvals(run_id)[0]

    resolution = store.resolve_approval(
        run_id,
        "write_file",
        fingerprint,
        {"args": {"path": "safe.txt"}},
        "Write approval.",
        auto_approve=True,
    )
    after = store.list_approvals(run_id)[0]

    assert resolution.state == "consumed"
    assert resolution.approver == "human-reviewer"
    assert after["status"] == "consumed"
    assert after["approver"] == before["approver"] == "human-reviewer"
    assert after["decided_at"] == before["decided_at"]
    assert after["reviewer_reason"] == before["reviewer_reason"] == "Reviewed manually"


def test_v1_database_migrates_reasons_and_adds_v2_indexes(tmp_path):
    db_path = tmp_path / "runs.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE runs (
                id TEXT PRIMARY KEY, task TEXT NOT NULL, agent_name TEXT NOT NULL,
                status TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
                workspace_path TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, ts TEXT NOT NULL,
                type TEXT NOT NULL, tool_name TEXT, decision TEXT, risk TEXT,
                message TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE TABLE approvals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                tool_name TEXT NOT NULL, status TEXT NOT NULL, requested_at TEXT NOT NULL,
                decided_at TEXT, approver TEXT, reason TEXT, payload_json TEXT NOT NULL
            );
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta (key, value) VALUES ('schema_version', '1');
            INSERT INTO runs VALUES ('run_old', 'old', 'agent', 'waiting_for_approval',
                '2026-01-01T00:00:00+00:00', NULL, '.', '{}');
            INSERT INTO approvals (
                run_id, tool_name, status, requested_at, reason, payload_json
            ) VALUES (
                'run_old', 'write_file', 'pending', '2026-01-01T00:00:00+00:00',
                'Legacy policy reason',
                '{"request_fingerprint":"legacy-fingerprint","args":{"path":"safe.txt"}}'
            );
            """
        )

    store = AuditStore(db_path)
    approval = store.list_approvals("run_old")[0]

    assert store.get_schema_version() == 2
    assert approval["request_fingerprint"] == "legacy-fingerprint"
    assert approval["policy_reason"] == "Legacy policy reason"
    assert approval["reviewer_reason"] is None
    with sqlite3.connect(db_path) as conn:
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_%'"
            )
        }
    assert {
        "idx_events_run_id_id",
        "idx_approvals_run_id_id",
        "idx_approvals_lookup",
        "idx_approvals_active_unique",
    }.issubset(indexes)


def test_v1_migration_redacts_all_legacy_durable_fields(tmp_path):
    db_path = tmp_path / "legacy-secrets.sqlite"
    credentials = {
        "task": "ghp_0123456789abcdefghijklmnop",
        "agent": "sk-0123456789abcdefghijklmnop",
        "metadata": "AKIA0123456789ABCDEF",
        "approver": "eyJheader.payload.signature",
        "reason": "Bearer abcdefghijklmnop",
        "approval_payload": "token=approval-secret-value",
        "event_message": "password=event-message-secret",
        "event_payload": "gho_0123456789abcdefghijklmnop",
    }
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE runs (
                id TEXT PRIMARY KEY, task TEXT NOT NULL, agent_name TEXT NOT NULL,
                status TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
                workspace_path TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, ts TEXT NOT NULL,
                type TEXT NOT NULL, tool_name TEXT, decision TEXT, risk TEXT,
                message TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE TABLE approvals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                tool_name TEXT NOT NULL, status TEXT NOT NULL, requested_at TEXT NOT NULL,
                decided_at TEXT, approver TEXT, reason TEXT, payload_json TEXT NOT NULL
            );
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta (key, value) VALUES ('schema_version', '1');
            """
        )
        conn.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run_secret",
                f"ordinary task {credentials['task']} retained",
                f"ordinary agent {credentials['agent']} retained",
                "waiting_for_approval",
                "2026-01-01T00:00:00+00:00",
                None,
                ".",
                json.dumps(
                    {"note": f"ordinary metadata {credentials['metadata']} retained"}
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO approvals (
                run_id, tool_name, status, requested_at, approver, reason, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "run_secret",
                "write_file",
                "approved",
                "2026-01-01T00:00:01+00:00",
                f"ordinary approver {credentials['approver']} retained",
                f"ordinary policy {credentials['reason']} retained",
                json.dumps(
                    {
                        "request_fingerprint": "legacy-secret-fingerprint",
                        "note": (
                            "ordinary approval payload "
                            f"{credentials['approval_payload']} retained"
                        ),
                    }
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO events (run_id, ts, type, message, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "run_secret",
                "2026-01-01T00:00:02+00:00",
                "legacy_event",
                f"ordinary event {credentials['event_message']} retained",
                json.dumps(
                    {
                        "note": (
                            "ordinary event payload "
                            f"{credentials['event_payload']} retained"
                        )
                    }
                ),
            ),
        )

    store = AuditStore(db_path)
    run = store.get_run("run_secret")
    approval = store.list_approvals("run_secret")[0]
    event = store.get_events("run_secret")[0]

    assert run["task"] == "ordinary task [redacted] retained"
    assert run["agent_name"] == "ordinary agent [redacted] retained"
    assert json.loads(run["metadata_json"]) == {
        "note": "ordinary metadata [redacted] retained"
    }
    assert approval["approver"] == "ordinary approver [redacted] retained"
    assert approval["policy_reason"] == "ordinary policy Bearer [redacted] retained"
    assert approval["reviewer_reason"] is None
    assert approval["payload"] == {
        "request_fingerprint": "legacy-secret-fingerprint",
        "note": "ordinary approval payload token=[redacted] retained",
    }
    assert event["message"] == "ordinary event password=[redacted] retained"
    assert event["payload"] == {"note": "ordinary event payload [redacted] retained"}
    migrated = json.dumps(
        {"run": run, "approval": approval, "event": event}, ensure_ascii=False
    )
    assert all(credential not in migrated for credential in credentials.values())


def test_v1_migration_reconciles_duplicate_active_approvals(tmp_path):
    db_path = tmp_path / "duplicates.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE runs (
                id TEXT PRIMARY KEY, task TEXT NOT NULL, agent_name TEXT NOT NULL,
                status TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
                workspace_path TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, ts TEXT NOT NULL,
                type TEXT NOT NULL, tool_name TEXT, decision TEXT, risk TEXT,
                message TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE TABLE approvals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                tool_name TEXT NOT NULL, status TEXT NOT NULL, requested_at TEXT NOT NULL,
                decided_at TEXT, approver TEXT, reason TEXT, payload_json TEXT NOT NULL
            );
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta (key, value) VALUES ('schema_version', '1');
            INSERT INTO runs VALUES ('run_duplicate', 'old', 'agent', 'waiting_for_approval',
                '2026-01-01T00:00:00+00:00', NULL, '.', '{}');
            INSERT INTO approvals (
                run_id, tool_name, status, requested_at, reason, payload_json
            ) VALUES
                ('run_duplicate', 'write_file', 'pending', '2026-01-01T00:00:00+00:00',
                 'Policy reason', '{"request_fingerprint":"same","args":{"path":"safe.txt"}}'),
                ('run_duplicate', 'write_file', 'pending', '2026-01-01T00:00:01+00:00',
                 'Policy reason', '{"request_fingerprint":"same","args":{"path":"safe.txt"}}');
            """
        )

    store = AuditStore(db_path)
    approvals = store.list_approvals("run_duplicate")
    reused = store.create_approval(
        "run_duplicate",
        "write_file",
        {"request_fingerprint": "same", "args": {"path": "safe.txt"}},
        "Policy reason",
    )

    assert store.get_schema_version() == 2
    assert [(item["id"], item["status"]) for item in approvals] == [
        (1, "pending"),
        (2, "superseded"),
    ]
    assert reused == 1
