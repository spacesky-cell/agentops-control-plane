from __future__ import annotations

import json
import sys
import threading
from io import StringIO
from pathlib import Path

import pytest

from agentpermit.audit import ApprovalStateConflictError
from agentpermit.config import PolicyConfig
from agentpermit.gateway import RuntimeGateway
from agentpermit.mcp_stdio import McpStdioSession, serve_json_lines
from agentpermit.models import ToolRequest, ToolStatus
from agentpermit.workspace import WorkspaceManager


def _initialized(session: McpStdioSession) -> None:
    session.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})


class _BoundedReadOnlyStream:
    def __init__(self, content: str, max_read_chars: int) -> None:
        self.content = content
        self.max_read_chars = max_read_chars
        self.offset = 0
        self.read_sizes: list[int] = []

    def __iter__(self):
        raise AssertionError("unbounded stream iteration is forbidden")

    def readline(self, size: int = -1) -> str:
        assert 0 < size <= self.max_read_chars
        self.read_sizes.append(size)
        end = min(self.offset + size, len(self.content))
        newline = self.content.find("\n", self.offset, end)
        if newline >= 0:
            end = newline + 1
        chunk = self.content[self.offset : end]
        self.offset = end
        return chunk

    def read(self, size: int = -1) -> str:
        raise AssertionError(f"read({size}) is forbidden; use bounded readline(size)")


@pytest.mark.parametrize("status", ["success", "failed"])
def test_gateway_rejects_tool_execution_and_workspace_resume_after_terminal_run(
    tmp_path: Path, status: str
) -> None:
    gateway = RuntimeGateway.from_home(tmp_path / "home")
    run_id, workspace = gateway.start_run("terminal", "test")
    assert gateway.finish_run(run_id, workspace, status)

    with pytest.raises(ValueError, match=f"terminal.*{status}"):
        gateway.resume_workspace(run_id)
    with pytest.raises(ValueError, match=f"terminal.*{status}"):
        gateway.execute_tool(run_id, workspace, ToolRequest("list_files", {}, "test"))


@pytest.mark.parametrize("run_status", ["success", "failed"])
@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_terminal_run_rejects_approval_decision_and_cancels_pending_approval(
    tmp_path: Path, run_status: str, decision: str
) -> None:
    gateway = RuntimeGateway.from_home(tmp_path / "home")
    run_id, workspace = gateway.start_run("terminal approval", "test")
    if run_status == "success":
        approval_id = gateway.audit_store.create_approval(
            run_id,
            "write_file",
            {"args": {"path": "out.txt", "content": "pending"}},
            "File writes require approval by policy.",
        )
    else:
        pending = gateway.execute_tool(
            run_id,
            workspace,
            ToolRequest(
                "write_file", {"path": "out.txt", "content": "pending"}, "test"
            ),
        )
        assert pending.status == ToolStatus.PENDING_APPROVAL
        assert pending.approval_id is not None
        approval_id = pending.approval_id
    assert gateway.finish_run(run_id, workspace, run_status)

    approval = gateway.audit_store.get_approval(approval_id)
    assert approval is not None and approval["status"] == "cancelled"
    with pytest.raises(ApprovalStateConflictError, match=f"terminal.*{run_status}"):
        gateway.audit_store.decide_approval(
            approval_id, decision, "reviewer", "too late"
        )


def test_audit_store_cannot_create_or_consume_approval_for_terminal_run(
    tmp_path: Path,
) -> None:
    gateway = RuntimeGateway.from_home(tmp_path / "home")
    run_id, workspace = gateway.start_run("terminal resolution", "test")
    request = ToolRequest(
        "write_file", {"path": "out.txt", "content": "pending"}, "test"
    )
    fingerprint = gateway.request_fingerprint(request)
    assert gateway.finish_run(run_id, workspace, "success")

    with pytest.raises(ApprovalStateConflictError, match="terminal.*success"):
        gateway.audit_store.resolve_approval(
            run_id,
            request.tool_name,
            fingerprint,
            {"args": request.args},
            "File writes require approval by policy.",
        )

    assert gateway.audit_store.list_approvals(run_id) == []


def test_tool_execution_claim_rejects_missing_and_nonrunning_runs_and_rolls_back(
    tmp_path: Path,
) -> None:
    gateway = RuntimeGateway.from_home(tmp_path / "home")
    with pytest.raises(ValueError, match="Run not found"):
        with gateway.audit_store.tool_execution("missing"):
            pass

    initializing = gateway.audit_store.start_run("initializing", "test")
    with pytest.raises(ValueError, match="not running.*initializing"):
        with gateway.audit_store.tool_execution(initializing):
            pass

    run_id, _workspace = gateway.start_run("rollback", "test")
    with pytest.raises(RuntimeError, match="rollback marker"):
        with gateway.audit_store.tool_execution(run_id) as lifecycle:
            gateway.audit_store.add_event(
                run_id,
                "uncommitted",
                "must roll back",
                _connection=lifecycle,
            )
            raise RuntimeError("rollback marker")
    assert gateway.audit_store.get_events(run_id)[-1]["type"] == "run_started"


def test_terminal_snapshot_waits_for_claimed_tool_and_lifecycle_writes_do_not_deadlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = RuntimeGateway.from_home(tmp_path / "home")
    run_id, workspace = gateway.start_run("execution race", "test")
    entered = threading.Event()
    release = threading.Event()
    snapshot_started = threading.Event()
    original_execute = gateway.tool_executor.execute
    original_snapshot = gateway.workspace_manager.snapshot

    def blocked_execute(*args: object, **kwargs: object) -> object:
        entered.set()
        assert release.wait(5)
        return original_execute(*args, **kwargs)

    def observed_snapshot(*args: object, **kwargs: object) -> Path:
        snapshot_started.set()
        return original_snapshot(*args, **kwargs)

    monkeypatch.setattr(gateway.tool_executor, "execute", blocked_execute)
    monkeypatch.setattr(gateway.workspace_manager, "snapshot", observed_snapshot)
    tool_result: list[object] = []
    finish_result: list[object] = []
    tool_thread = threading.Thread(
        target=lambda: tool_result.append(
            gateway.execute_tool(
                run_id, workspace, ToolRequest("list_files", {}, "test")
            )
        )
    )
    finish_thread = threading.Thread(
        target=lambda: finish_result.append(
            gateway.finish_run(run_id, workspace, "success")
        )
    )

    tool_thread.start()
    assert entered.wait(5)
    finish_thread.start()
    assert not snapshot_started.wait(0.2)
    release.set()
    tool_thread.join(5)
    finish_thread.join(5)

    assert not tool_thread.is_alive() and not finish_thread.is_alive()
    assert tool_result[0].status == ToolStatus.OK
    assert finish_result == [True]
    events = gateway.audit_store.get_events(run_id)
    executed = next(event for event in events if event["type"] == "tool_executed")
    finished = next(event for event in events if event["type"] == "run_finished")
    assert executed["id"] < finished["id"]


def test_terminalization_winner_blocks_approval_consumption_and_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = RuntimeGateway.from_home(tmp_path / "home")
    run_id, workspace = gateway.start_run("approval race", "test")
    request = ToolRequest(
        "write_file", {"path": "out.txt", "content": "blocked"}, "test"
    )
    pending = gateway.execute_tool(run_id, workspace, request)
    gateway.audit_store.decide_approval(
        pending.approval_id, "approved", "reviewer", "ok"
    )
    snapshot_entered = threading.Event()
    release_snapshot = threading.Event()
    original_snapshot = gateway.workspace_manager.snapshot

    def blocked_snapshot(*args: object, **kwargs: object) -> Path:
        snapshot_entered.set()
        assert release_snapshot.wait(5)
        return original_snapshot(*args, **kwargs)

    monkeypatch.setattr(gateway.workspace_manager, "snapshot", blocked_snapshot)
    finish_thread = threading.Thread(
        target=lambda: gateway.finish_run(run_id, workspace, "failed")
    )
    finish_thread.start()
    assert snapshot_entered.wait(5)
    result: list[object] = []

    def execute_after_terminal_claim() -> None:
        try:
            gateway.execute_tool(run_id, workspace, request)
        except Exception as exc:  # noqa: BLE001 - capture the racing public result.
            result.append(exc)

    tool_thread = threading.Thread(target=execute_after_terminal_claim)
    tool_thread.start()
    tool_thread.join(0.2)
    assert tool_thread.is_alive()
    release_snapshot.set()
    finish_thread.join(5)
    tool_thread.join(5)

    assert not finish_thread.is_alive() and not tool_thread.is_alive()
    assert isinstance(result[0], ValueError)
    assert "terminal with status failed" in str(result[0])


@pytest.mark.parametrize(
    "command_output,error_fragment",
    [
        (
            {
                "program": "python",
                "args": ["-c", "raise SystemExit(7)"],
                "exit_code": 7,
                "output": "bounded failure",
                "output_truncated": False,
                "timed_out": False,
            },
            "exit code 7",
        ),
        (
            {
                "program": "python",
                "args": ["-c", "pass"],
                "exit_code": None,
                "output": "bounded timeout",
                "output_truncated": True,
                "timed_out": True,
            },
            "timed out",
        ),
    ],
)
def test_gateway_classifies_unsuccessful_command_result_as_failed_with_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command_output: dict[str, object],
    error_fragment: str,
) -> None:
    gateway = RuntimeGateway.from_home(tmp_path / "home")
    run_id, workspace = gateway.start_run("command failure", "test")
    monkeypatch.setattr(
        gateway.tool_executor, "execute", lambda *_args, **_kwargs: command_output
    )

    result = gateway.execute_tool(
        run_id,
        workspace,
        ToolRequest("run_command", {"program": "python", "args": ["-m", "pytest"]}),
    )

    assert result.status == ToolStatus.FAILED
    assert result.output == command_output
    assert error_fragment in (result.error or "").lower()
    event = gateway.audit_store.get_events(run_id)[-1]
    assert event["type"] == "tool_failed"
    assert event["payload"]["output"]["exit_code"] == command_output["exit_code"]
    assert event["payload"]["output"]["timed_out"] == command_output["timed_out"]
    assert (
        event["payload"]["output"]["output_truncated"]
        == command_output["output_truncated"]
    )


def test_mcp_command_failure_returns_is_error_with_structured_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = RuntimeGateway.from_home(tmp_path / "home")
    session = McpStdioSession(gateway)
    _initialized(session)
    command_output = {
        "program": "python",
        "args": ["-c", "raise SystemExit(9)"],
        "exit_code": 9,
        "output": "bounded failure",
        "output_truncated": False,
        "timed_out": False,
    }
    monkeypatch.setattr(
        gateway.tool_executor, "execute", lambda *_args, **_kwargs: command_output
    )

    response = session.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "run_command",
                "arguments": {"program": "python", "args": ["-m", "pytest"]},
            },
        }
    )

    assert response["result"]["isError"] is True
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["status"] == "failed"
    assert payload["output"] == command_output


def test_real_gateway_nonzero_and_mcp_timeout_are_failed_tool_calls(
    tmp_path: Path,
) -> None:
    config = PolicyConfig(
        command_allow_prefixes=[[sys.executable]], max_command_seconds=1
    )
    gateway = RuntimeGateway.from_home(tmp_path / "gateway", config)
    run_id, workspace = gateway.start_run("real nonzero", "test")
    nonzero = gateway.execute_tool(
        run_id,
        workspace,
        ToolRequest(
            "run_command",
            {"program": sys.executable, "args": ["-c", "raise SystemExit(7)"]},
        ),
    )
    assert nonzero.status == ToolStatus.FAILED
    assert nonzero.output["exit_code"] == 7
    assert nonzero.output["timed_out"] is False

    mcp_gateway = RuntimeGateway.from_home(tmp_path / "mcp", config)
    session = McpStdioSession(mcp_gateway)
    _initialized(session)
    response = session.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "run_command",
                "arguments": {
                    "program": sys.executable,
                    "args": ["-c", "import time; time.sleep(30)"],
                },
            },
        }
    )
    payload = json.loads(response["result"]["content"][0]["text"])
    assert response["result"]["isError"] is True
    assert payload["status"] == "failed"
    assert payload["output"]["exit_code"] is None
    assert payload["output"]["timed_out"] is True


@pytest.mark.parametrize(
    "field",
    [
        "max_mcp_frame_bytes",
        "max_tool_argument_bytes",
        "max_file_bytes",
        "max_source_bytes",
    ],
)
@pytest.mark.parametrize("invalid", [True, 0, -1, 1.5, "10"])
def test_new_policy_limits_require_positive_integers(
    field: str, invalid: object
) -> None:
    with pytest.raises(ValueError, match=field):
        PolicyConfig(**{field: invalid})


def test_mcp_frame_limit_rejects_before_parse_and_accepts_exact_boundary(
    tmp_path: Path,
) -> None:
    frame = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        separators=(",", ":"),
    )
    exact = RuntimeGateway.from_home(
        tmp_path / "exact", PolicyConfig(max_mcp_frame_bytes=len(frame.encode("utf-8")))
    )
    exact_output = StringIO()
    serve_json_lines(exact, StringIO(frame + "\n"), exact_output)
    assert (
        json.loads(exact_output.getvalue())["result"]["serverInfo"]["name"]
        == "agentpermit"
    )

    limited = RuntimeGateway.from_home(
        tmp_path / "limited",
        PolicyConfig(max_mcp_frame_bytes=len(frame.encode("utf-8")) - 1),
    )
    limited_output = StringIO()
    serve_json_lines(limited, StringIO(frame + "\n"), limited_output)
    error = json.loads(limited_output.getvalue())["error"]
    assert error["code"] == -32001
    assert error["data"] == {
        "limit": "max_mcp_frame_bytes",
        "max_bytes": len(frame.encode("utf-8")) - 1,
        "actual_bytes": len(frame.encode("utf-8")),
    }


def test_mcp_frame_reader_uses_bounded_reads_and_recovers_next_frame(
    tmp_path: Path,
) -> None:
    limit = 128
    valid = json.dumps(
        {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}},
        separators=(",", ":"),
    )
    stream = _BoundedReadOnlyStream("x" * 200 + "\n" + valid + "\n", limit + 1)
    output = StringIO()

    serve_json_lines(
        RuntimeGateway.from_home(
            tmp_path / "home", PolicyConfig(max_mcp_frame_bytes=limit)
        ),
        stream,
        output,
    )

    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses[0]["error"] == {
        "code": -32001,
        "message": "MCP frame exceeds max_mcp_frame_bytes.",
        "data": {
            "limit": "max_mcp_frame_bytes",
            "max_bytes": limit,
            "actual_bytes": 200,
        },
    }
    assert responses[1]["id"] == 2
    assert responses[1]["result"]["serverInfo"]["name"] == "agentpermit"
    assert stream.read_sizes and max(stream.read_sizes) <= limit + 1


def test_mcp_frame_reader_rejects_oversized_unterminated_frame(tmp_path: Path) -> None:
    limit = 8
    stream = _BoundedReadOnlyStream("z" * 25, limit + 1)
    output = StringIO()

    serve_json_lines(
        RuntimeGateway.from_home(
            tmp_path / "home", PolicyConfig(max_mcp_frame_bytes=limit)
        ),
        stream,
        output,
    )

    error = json.loads(output.getvalue())["error"]
    assert error["code"] == -32001
    assert error["data"]["actual_bytes"] == 25


def test_mcp_frame_reader_enforces_exact_multibyte_utf8_bytes(tmp_path: Path) -> None:
    frame = json.dumps("你", ensure_ascii=False)
    frame_bytes = len(frame.encode("utf-8"))
    exact_output = StringIO()
    serve_json_lines(
        RuntimeGateway.from_home(
            tmp_path / "exact", PolicyConfig(max_mcp_frame_bytes=frame_bytes)
        ),
        _BoundedReadOnlyStream(frame + "\n", frame_bytes + 1),
        exact_output,
    )
    assert json.loads(exact_output.getvalue())["error"]["code"] == -32600

    limited_output = StringIO()
    serve_json_lines(
        RuntimeGateway.from_home(
            tmp_path / "limited", PolicyConfig(max_mcp_frame_bytes=frame_bytes - 1)
        ),
        _BoundedReadOnlyStream(frame + "\n", frame_bytes),
        limited_output,
    )
    error = json.loads(limited_output.getvalue())["error"]
    assert error["code"] == -32001
    assert error["data"]["actual_bytes"] == frame_bytes


def test_pending_approval_wins_clean_finish_race_with_atomic_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = RuntimeGateway.from_home(tmp_path / "home")
    session = McpStdioSession(gateway, task="pending wins")
    _initialized(session)
    original_execute = gateway.execute_tool
    finish_results: list[bool] = []

    def finish_after_pending_commit(*args: object, **kwargs: object):
        result = original_execute(*args, **kwargs)
        run_id = str(args[0])
        assert session.workspace is not None
        finish_results.append(gateway.finish_run(run_id, session.workspace, "success"))
        return result

    monkeypatch.setattr(gateway, "execute_tool", finish_after_pending_commit)
    response = session.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "write_file",
                "arguments": {"path": "out.txt", "content": "pending"},
            },
        }
    )

    assert response["result"]["isError"] is True
    assert "pending_approval" in response["result"]["content"][0]["text"]
    assert "approval_id=" in response["result"]["content"][0]["text"]
    assert finish_results == [False]
    assert (
        gateway.audit_store.get_run(session.run_id)["status"] == "waiting_for_approval"
    )
    approvals = gateway.audit_store.list_approvals(session.run_id)
    assert len(approvals) == 1 and approvals[0]["status"] == "pending"
    event_types = [
        event["type"] for event in gateway.audit_store.get_events(session.run_id)
    ]
    assert event_types.count("approval_requested") == 1
    assert event_types.count("run_paused") == 1
    assert "run_finished" not in event_types
    assert event_types.index("approval_requested") < event_types.index("run_paused")


def test_clean_finish_winner_returns_terminal_tool_error_without_pending_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = RuntimeGateway.from_home(tmp_path / "home")
    session = McpStdioSession(gateway, task="finish wins")
    _initialized(session)
    session.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "list_files", "arguments": {}},
        }
    )
    assert session.run_id is not None and session.workspace is not None
    snapshot_entered = threading.Event()
    release_snapshot = threading.Event()
    original_snapshot = gateway.workspace_manager.snapshot

    def blocked_snapshot(*args: object, **kwargs: object) -> Path:
        snapshot_entered.set()
        assert release_snapshot.wait(5)
        return original_snapshot(*args, **kwargs)

    monkeypatch.setattr(gateway.workspace_manager, "snapshot", blocked_snapshot)
    finish_results: list[bool] = []
    finish_thread = threading.Thread(
        target=lambda: finish_results.append(
            gateway.finish_run(session.run_id, session.workspace, "success")
        )
    )
    finish_thread.start()
    assert snapshot_entered.wait(5)
    responses: list[dict[str, object]] = []
    request_thread = threading.Thread(
        target=lambda: responses.append(
            session.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "write_file",
                        "arguments": {"path": "out.txt", "content": "too late"},
                    },
                }
            )
        )
    )
    request_thread.start()
    request_thread.join(0.2)
    assert request_thread.is_alive()
    release_snapshot.set()
    finish_thread.join(5)
    request_thread.join(5)

    assert finish_results == [True]
    assert responses[0]["result"]["isError"] is True
    text = responses[0]["result"]["content"][0]["text"]
    assert "terminal" in text and "success" in text
    assert "approval_id" not in text
    assert gateway.audit_store.get_run(session.run_id)["status"] == "success"
    assert gateway.audit_store.list_approvals(session.run_id) == []
    event_types = [
        event["type"] for event in gateway.audit_store.get_events(session.run_id)
    ]
    assert event_types.count("run_finished") == 1
    assert "approval_requested" not in event_types
    assert "run_paused" not in event_types


def test_mcp_transport_ignores_blank_frames(tmp_path: Path) -> None:
    output = StringIO()
    serve_json_lines(
        RuntimeGateway.from_home(tmp_path / "home"), StringIO("\n\r\n"), output
    )
    assert output.getvalue() == ""


def test_gateway_and_mcp_reject_oversized_tool_arguments_structurally(
    tmp_path: Path,
) -> None:
    config = PolicyConfig(max_tool_argument_bytes=32)
    gateway = RuntimeGateway.from_home(tmp_path / "home", config)
    run_id, workspace = gateway.start_run("large args", "test")
    request = ToolRequest("write_file", {"path": "a.txt", "content": "x" * 40})

    result = gateway.execute_tool(run_id, workspace, request)
    assert result.status == ToolStatus.DENIED
    assert result.output["limit"] == "max_tool_argument_bytes"
    assert result.output["actual_bytes"] > result.output["max_bytes"]

    second_gateway = RuntimeGateway.from_home(tmp_path / "mcp", config)
    session = McpStdioSession(second_gateway)
    _initialized(session)
    response = session.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "write_file",
                "arguments": {"path": "a.txt", "content": "x" * 40},
            },
        }
    )
    assert response["result"]["isError"] is True
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["output"]["limit"] == "max_tool_argument_bytes"

    exact_gateway = RuntimeGateway.from_home(
        tmp_path / "exact-args", PolicyConfig(max_tool_argument_bytes=2)
    )
    exact_run, exact_workspace = exact_gateway.start_run("exact args", "test")
    exact = exact_gateway.execute_tool(
        exact_run, exact_workspace, ToolRequest("list_files", {})
    )
    assert exact.status == ToolStatus.OK

    circular: dict[str, object] = {}
    circular["self"] = circular
    circular_gateway = RuntimeGateway.from_home(
        tmp_path / "circular", PolicyConfig(max_tool_argument_bytes=1)
    )
    circular_run, circular_workspace = circular_gateway.start_run("circular", "test")
    circular_result = circular_gateway.execute_tool(
        circular_run, circular_workspace, ToolRequest("list_files", circular)
    )
    assert circular_result.status == ToolStatus.DENIED


def test_file_read_write_patch_and_snapshot_enforce_per_file_limit(
    tmp_path: Path,
) -> None:
    config = PolicyConfig(max_file_bytes=4)
    manager = WorkspaceManager(tmp_path / ".agentpermit", config)
    workspace = manager.create("bounded")

    assert manager.write_text(workspace, "exact.txt", "abcd") is None
    assert manager.read_text(workspace, "exact.txt") == "abcd"
    assert manager.snapshot("bounded", workspace, "exact").is_file()
    with pytest.raises(ValueError, match="max_file_bytes"):
        manager.write_text(workspace, "large.txt", "abcde")
    assert not (workspace / "large.txt").exists()
    with pytest.raises(ValueError, match="max_file_bytes"):
        manager.patch_text(workspace, "exact.txt", "a", "abcde")
    assert manager.read_text(workspace, "exact.txt") == "abcd"
    (workspace / "outside.txt").write_bytes(b"12345")
    with pytest.raises(ValueError, match="max_file_bytes"):
        manager.read_text(workspace, "outside.txt")
    with pytest.raises(ValueError, match="max_file_bytes"):
        manager.snapshot("bounded", workspace, "oversized")


def test_source_copy_enforces_per_file_and_aggregate_limits(tmp_path: Path) -> None:
    per_file_source = tmp_path / "per-file"
    per_file_source.mkdir()
    (per_file_source / "large.txt").write_bytes(b"12345")
    per_file_manager = WorkspaceManager(
        tmp_path / "per-file-home",
        PolicyConfig(max_file_bytes=4, max_source_bytes=100),
    )
    with pytest.raises(ValueError, match="max_file_bytes"):
        per_file_manager.create("per-file", per_file_source)

    aggregate_source = tmp_path / "aggregate"
    aggregate_source.mkdir()
    (aggregate_source / "a.txt").write_bytes(b"1234")
    (aggregate_source / "b.txt").write_bytes(b"5678")
    exact_manager = WorkspaceManager(
        tmp_path / "exact-home",
        PolicyConfig(max_file_bytes=4, max_source_bytes=8),
    )
    exact_workspace = exact_manager.create("exact", aggregate_source)
    assert exact_manager.read_text(exact_workspace, "b.txt") == "5678"
    limited_manager = WorkspaceManager(
        tmp_path / "limited-home",
        PolicyConfig(max_file_bytes=4, max_source_bytes=7),
    )
    with pytest.raises(ValueError, match="max_source_bytes"):
        limited_manager.create("aggregate", aggregate_source)
