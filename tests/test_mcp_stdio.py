import json
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest

from agentpermit import __version__
from agentpermit.audit import AuditStore
from agentpermit.gateway import RuntimeGateway
from agentpermit.mcp_stdio import McpStdioSession, serve_json_lines


def make_sample_repo(root: Path) -> Path:
    source = root / "sample_repo"
    source.mkdir()
    (source / "math_utils.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    return source


def initialized(session: McpStdioSession) -> None:
    assert session.handle(
        {"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}}
    )["result"]["serverInfo"] == {
        "name": "agentpermit",
        "version": __version__,
    }
    assert (
        session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        is None
    )


def test_standard_mcp_lifecycle_lazily_starts_one_run(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(
        gateway, source=source, task="stdio read", agent_name="test-agent"
    )

    initialized(session)
    listed = session.handle({"jsonrpc": "2.0", "id": "tools", "method": "tools/list"})
    assert listed["result"]["tools"]
    assert session.run_id is None

    response = session.handle(
        {
            "jsonrpc": "2.0",
            "id": "read",
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "math_utils.py"}},
        }
    )
    assert response["result"] == {
        "content": [{"type": "text", "text": "def add(a, b):\n    return a + b\n"}],
        "isError": False,
    }
    run_id = session.run_id
    assert run_id and gateway.audit_store.get_run(run_id)["agent_name"] == "test-agent"

    session.handle(
        {
            "jsonrpc": "2.0",
            "id": "read2",
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "math_utils.py"}},
        }
    )
    assert session.run_id == run_id


def test_private_lifecycle_methods_are_not_supported(tmp_path):
    session = McpStdioSession(
        RuntimeGateway.from_home(tmp_path / "project"), task="test"
    )
    for method in ("run.start", "tool.call", "run.finish"):
        response = session.handle(
            {"jsonrpc": "2.0", "id": method, "method": method, "params": {}}
        )
        assert response["error"]["code"] == -32601


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ([], -32600),
        ({"jsonrpc": "1.0", "id": 1, "method": "ping"}, -32600),
        ({"jsonrpc": "2.0", "id": 1}, -32600),
        ({"jsonrpc": "2.0", "method": "ping"}, -32600),
        ({"jsonrpc": "2.0", "id": True, "method": "ping"}, -32600),
        ({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []}, -32600),
    ],
)
def test_protocol_validation_returns_json_rpc_errors(tmp_path, payload, code):
    session = McpStdioSession(RuntimeGateway.from_home(tmp_path / "project"))
    response = session.handle(payload)
    assert response["error"]["code"] == code
    session.close()


def test_protocol_capabilities_and_parse_error_transport(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway)
    before_init = session.handle(
        {"jsonrpc": "2.0", "id": "tools", "method": "tools/list"}
    )
    assert before_init["error"]["code"] == -32002
    response = session.handle(
        {
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {"protocolVersion": "old"},
        }
    )
    assert response["result"]["protocolVersion"]
    assert session.handle({"jsonrpc": "2.0", "method": "notifications/other"}) is None
    assert (
        session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        is None
    )
    assert (
        session.handle({"jsonrpc": "2.0", "id": "ping", "method": "ping"})["result"]
        == {}
    )
    assert session.handle(
        {"jsonrpc": "2.0", "id": "resources", "method": "resources/list"}
    )["result"] == {"resources": []}
    assert session.handle(
        {"jsonrpc": "2.0", "id": "prompts", "method": "prompts/list"}
    )["result"] == {"prompts": []}

    output = StringIO()
    serve_json_lines(gateway, StringIO("not-json\n"), output)
    parsed = json.loads(output.getvalue())
    assert parsed["error"]["code"] == -32700


def test_client_auto_approve_is_ignored_and_trusted_flag_controls_approval(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(
        gateway, source=source, task="approval", auto_approve=False
    )
    initialized(session)

    response = session.handle(
        {
            "jsonrpc": "2.0",
            "id": "patch",
            "method": "tools/call",
            "params": {
                "name": "patch_text",
                "auto_approve": True,
                "arguments": {
                    "path": "math_utils.py",
                    "old": "return a + b",
                    "new": "return a - b",
                },
            },
        }
    )
    assert response["result"]["isError"] is True
    assert "approval_id=" in response["result"]["content"][0]["text"]
    approval = gateway.audit_store.list_approvals(session.run_id)[0]
    assert approval["status"] == "pending"


def test_approved_identical_retry_consumes_once(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway, source=source, task="approval")
    initialized(session)
    params = {
        "name": "patch_text",
        "arguments": {
            "path": "math_utils.py",
            "old": "return a + b",
            "new": "return a - b",
        },
    }
    first = session.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    )
    approval_id = gateway.audit_store.list_approvals(session.run_id)[0]["id"]
    gateway.audit_store.decide_approval(approval_id, "approved", "reviewer", "ok")
    second = session.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": params}
    )
    third = session.handle(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": params}
    )
    assert first["result"]["isError"] is True
    assert second["result"]["isError"] is False
    assert third["result"]["isError"] is True
    approvals = gateway.audit_store.list_approvals(session.run_id)
    assert approvals[0]["id"] == approval_id
    assert approvals[0]["status"] == "consumed"
    events = gateway.audit_store.get_events(session.run_id)
    assert (
        len(
            [
                event
                for event in events
                if event["type"] == "tool_executed"
                and event["tool_name"] == "patch_text"
            ]
        )
        == 1
    )


def test_approved_retry_does_not_execute_if_terminal_finish_wins_resume_race(
    tmp_path, monkeypatch
):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway, source=source, task="approval resume race")
    initialized(session)
    params = {
        "name": "patch_text",
        "arguments": {
            "path": "math_utils.py",
            "old": "return a + b",
            "new": "return a - b",
        },
    }
    session.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    )
    approval_id = gateway.audit_store.list_approvals(session.run_id)[0]["id"]
    gateway.audit_store.decide_approval(approval_id, "approved", "reviewer", "ok")
    original_resume = gateway.resume_run

    def finish_before_resume(run_id: str) -> bool:
        gateway.audit_store.finish_run(run_id, "failed", message="race winner")
        return original_resume(run_id)

    monkeypatch.setattr(gateway, "resume_run", finish_before_resume)
    response = session.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": params}
    )

    assert response["result"]["isError"] is True
    assert "no longer waiting for approval" in response["result"]["content"][0]["text"]
    run = gateway.audit_store.get_run(session.run_id)
    assert run["status"] == "failed"
    assert "return a + b" in (Path(run["workspace_path"]) / "math_utils.py").read_text(
        encoding="utf-8"
    )
    events = gateway.audit_store.get_events(session.run_id)
    assert not any(event["type"] == "tool_executed" for event in events)
    assert len([event for event in events if event["type"] == "run_finished"]) == 1


def test_pending_retry_is_stable_and_different_call_cannot_resume_run(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway, source=source, task="approval")
    initialized(session)
    params = {
        "name": "patch_text",
        "arguments": {
            "path": "math_utils.py",
            "old": "return a + b",
            "new": "return a - b",
        },
    }

    first = session.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    )
    approval_id = gateway.audit_store.list_approvals(session.run_id)[0]["id"]
    retry = session.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": params}
    )
    different = session.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "list_files", "arguments": {}},
        }
    )

    assert f"approval_id={approval_id}" in first["result"]["content"][0]["text"]
    assert f"approval_id={approval_id}" in retry["result"]["content"][0]["text"]
    assert "waiting for approval" in different["result"]["content"][0]["text"].lower()
    assert (
        gateway.audit_store.get_run(session.run_id)["status"] == "waiting_for_approval"
    )
    approval = gateway.audit_store.get_approval(approval_id)
    assert approval and approval["status"] == "pending"
    events = gateway.audit_store.get_events(session.run_id)
    assert len([event for event in events if event["type"] == "run_paused"]) == 1
    assert not [
        event
        for event in events
        if event["type"] == "tool_executed" and event["tool_name"] == "list_files"
    ]


def test_terminal_tool_failure_is_persisted_before_response_and_blocks_later_tools(
    tmp_path,
):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway, source=source, task="immediate failure")
    initialized(session)

    failed = session.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "missing.txt"}},
        }
    )

    assert failed["result"]["isError"] is True
    assert gateway.audit_store.get_run(session.run_id)["status"] == "failed"
    later = session.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "list_files", "arguments": {}},
        }
    )
    assert later["result"]["isError"] is True
    assert "failed" in later["result"]["content"][0]["text"].lower()
    assert (
        len(
            [
                event
                for event in gateway.audit_store.get_events(session.run_id)
                if event["type"] == "run_finished"
            ]
        )
        == 1
    )


def test_policy_denial_is_persisted_failed_before_response(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway, source=source, task="immediate denial")
    initialized(session)

    denied = session.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": ".env"}},
        }
    )

    assert denied["result"]["isError"] is True
    assert "denied" in denied["result"]["content"][0]["text"].lower()
    assert gateway.audit_store.get_run(session.run_id)["status"] == "failed"


def test_rejected_pending_approval_is_persisted_failed_on_clean_eof(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway, source=source, task="rejected eof")
    initialized(session)
    params = {
        "name": "patch_text",
        "arguments": {
            "path": "math_utils.py",
            "old": "return a + b",
            "new": "return a - b",
        },
    }
    session.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    )
    approval_id = gateway.audit_store.list_approvals(session.run_id)[0]["id"]
    gateway.audit_store.decide_approval(approval_id, "rejected", "reviewer", "no")

    session.close()

    assert gateway.audit_store.get_run(session.run_id)["status"] == "failed"
    assert (
        len(
            [
                event
                for event in gateway.audit_store.get_events(session.run_id)
                if event["type"] == "run_finished"
            ]
        )
        == 1
    )


def test_approved_pending_approval_stays_waiting_until_identical_retry(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway, source=source, task="approved eof")
    initialized(session)
    params = {
        "name": "patch_text",
        "arguments": {
            "path": "math_utils.py",
            "old": "return a + b",
            "new": "return a - b",
        },
    }
    session.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    )
    approval_id = gateway.audit_store.list_approvals(session.run_id)[0]["id"]
    gateway.audit_store.decide_approval(approval_id, "approved", "reviewer", "ok")

    session.close()

    assert (
        gateway.audit_store.get_run(session.run_id)["status"] == "waiting_for_approval"
    )


def test_approval_approved_before_pause_keeps_run_waiting_until_retry(
    tmp_path, monkeypatch
):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway, source=source, task="approved before pause")
    initialized(session)
    params = {
        "name": "patch_text",
        "arguments": {
            "path": "math_utils.py",
            "old": "return a + b",
            "new": "return a - b",
        },
    }
    original_pause = gateway.pause_run

    def approve_before_pause(
        run_id: str,
        status: str = "waiting_for_approval",
        approval_id: int | None = None,
    ) -> None:
        selected_id = approval_id or gateway.audit_store.list_approvals(run_id)[0]["id"]
        gateway.audit_store.decide_approval(selected_id, "approved", "reviewer", "ok")
        original_pause(run_id, status, selected_id)

    monkeypatch.setattr(gateway, "pause_run", approve_before_pause)
    first = session.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    )

    assert first["result"]["isError"] is True
    assert (
        gateway.audit_store.get_run(session.run_id)["status"] == "waiting_for_approval"
    )
    workspace = Path(gateway.audit_store.get_run(session.run_id)["workspace_path"])
    assert "return a - b" not in (workspace / "math_utils.py").read_text(
        encoding="utf-8"
    )
    assert not [
        event
        for event in gateway.audit_store.get_events(session.run_id)
        if event["type"] == "run_finished"
    ]

    session.close()
    assert (
        gateway.audit_store.get_run(session.run_id)["status"] == "waiting_for_approval"
    )
    assert not [
        event
        for event in gateway.audit_store.get_events(session.run_id)
        if event["type"] == "run_finished"
    ]

    second = session.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": params}
    )
    assert second["result"]["isError"] is False
    workspace = Path(gateway.audit_store.get_run(session.run_id)["workspace_path"])
    assert "return a - b" in (workspace / "math_utils.py").read_text(encoding="utf-8")
    assert gateway.audit_store.list_approvals(session.run_id)[0]["status"] == "consumed"
    assert (
        len(
            [
                event
                for event in gateway.audit_store.get_events(session.run_id)
                if event["type"] == "tool_executed"
            ]
        )
        == 1
    )


def test_public_rejection_after_clean_eof_fails_waiting_run(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway, source=source, task="reject after eof")
    initialized(session)
    params = {
        "name": "patch_text",
        "arguments": {
            "path": "math_utils.py",
            "old": "return a + b",
            "new": "return a - b",
        },
    }

    session.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    )
    approval_id = gateway.audit_store.list_approvals(session.run_id)[0]["id"]
    session.close()

    assert (
        gateway.audit_store.get_run(session.run_id)["status"] == "waiting_for_approval"
    )
    gateway.audit_store.decide_approval(approval_id, "rejected", "reviewer", "no")

    assert gateway.audit_store.get_run(session.run_id)["status"] == "failed"
    finished = [
        event
        for event in gateway.audit_store.get_events(session.run_id)
        if event["type"] == "run_finished"
    ]
    assert len(finished) == 1
    assert finished[0]["payload"] == {
        "reason": "approval_rejected",
        "approval_id": approval_id,
    }


def test_rejection_between_close_read_and_finish_keeps_one_terminal_event(
    tmp_path, monkeypatch
):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway, source=source, task="rejection close race")
    initialized(session)
    params = {
        "name": "patch_text",
        "arguments": {
            "path": "math_utils.py",
            "old": "return a + b",
            "new": "return a - b",
        },
    }
    session.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    )
    approval_id = gateway.audit_store.list_approvals(session.run_id)[0]["id"]
    gateway.audit_store.resume_run(session.run_id)
    original_snapshot = gateway.workspace_manager.snapshot
    rejected = False

    def reject_during_snapshot(*args, **kwargs):
        nonlocal rejected
        if not rejected:
            rejected = True
            gateway.audit_store.decide_approval(
                approval_id, "rejected", "reviewer", "no"
            )
        return original_snapshot(*args, **kwargs)

    monkeypatch.setattr(gateway.workspace_manager, "snapshot", reject_during_snapshot)
    session.close()

    run = gateway.audit_store.get_run(session.run_id)
    finished = [
        event
        for event in gateway.audit_store.get_events(session.run_id)
        if event["type"] == "run_finished"
    ]
    assert run["status"] == "failed"
    assert len(finished) == 1
    assert finished[0]["payload"] == {
        "reason": "approval_rejected",
        "approval_id": approval_id,
    }


def test_different_tool_after_external_rejection_fails_without_execution(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway, source=source, task="different after rejection")
    initialized(session)
    params = {
        "name": "patch_text",
        "arguments": {
            "path": "math_utils.py",
            "old": "return a + b",
            "new": "return a - b",
        },
    }
    session.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    )
    approval_id = gateway.audit_store.list_approvals(session.run_id)[0]["id"]
    gateway.audit_store.decide_approval(approval_id, "rejected", "reviewer", "no")

    different = session.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "list_files", "arguments": {}},
        }
    )

    assert different["result"]["isError"] is True
    assert "failed" in different["result"]["content"][0]["text"].lower()
    assert gateway.audit_store.get_run(session.run_id)["status"] == "failed"
    assert not [
        event
        for event in gateway.audit_store.get_events(session.run_id)
        if event["type"] == "tool_executed"
    ]


def test_rejected_pending_retry_fails_run_and_blocks_later_tools(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway, source=source, task="approval")
    initialized(session)
    params = {
        "name": "patch_text",
        "arguments": {
            "path": "math_utils.py",
            "old": "return a + b",
            "new": "return a - b",
        },
    }
    session.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    )
    approval_id = gateway.audit_store.list_approvals(session.run_id)[0]["id"]
    gateway.audit_store.decide_approval(approval_id, "rejected", "reviewer", "no")

    rejected = session.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": params}
    )
    later = session.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "list_files", "arguments": {}},
        }
    )
    session.close()

    assert rejected["result"]["isError"] is True
    assert "failed" in rejected["result"]["content"][0]["text"].lower()
    assert later["result"]["isError"] is True
    assert "failed" in later["result"]["content"][0]["text"].lower()
    assert gateway.audit_store.get_run(session.run_id)["status"] == "failed"
    assert not [
        event
        for event in gateway.audit_store.get_events(session.run_id)
        if event["type"] == "tool_executed"
    ]


def test_clean_eof_finishes_running_session_success(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    input_stream = StringIO(
        "\n".join(
            [
                json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
                ),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "list_files", "arguments": {}},
                    }
                ),
            ]
        )
    )
    output_stream = StringIO()
    serve_json_lines(gateway, input_stream, output_stream, source=source, task="eof")
    responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert responses[-1]["result"]["isError"] is False
    run = gateway.audit_store.list_runs()[0]
    assert run["status"] == "success"


def test_clean_eof_preserves_waiting_approval(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    input_stream = StringIO(
        "\n".join(
            [
                json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
                ),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "write_file",
                            "arguments": {"path": "new.txt", "content": "x"},
                        },
                    }
                ),
            ]
        )
    )
    serve_json_lines(gateway, input_stream, StringIO(), source=source, task="wait")
    assert gateway.audit_store.list_runs()[0]["status"] == "waiting_for_approval"


def test_input_transport_failure_marks_running_run_failed_and_reraises(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")

    class BrokenInput:
        def __iter__(self):
            yield json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
            )
            yield json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
            yield json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "list_files", "arguments": {}},
                }
            )
            raise OSError("broken transport")

    with pytest.raises(OSError, match="broken transport"):
        serve_json_lines(
            gateway, BrokenInput(), StringIO(), source=source, task="broken"
        )
    assert gateway.audit_store.list_runs()[0]["status"] == "failed"


def test_output_transport_failure_marks_running_run_failed_and_reraises(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    input_stream = StringIO(
        "\n".join(
            [
                json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
                ),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "list_files", "arguments": {}},
                    }
                ),
            ]
        )
    )

    class BrokenOutput(StringIO):
        def flush(self):
            if gateway.audit_store.list_runs():
                raise BrokenPipeError("closed output")
            return super().flush()

    with pytest.raises(BrokenPipeError, match="closed output"):
        serve_json_lines(
            gateway, input_stream, BrokenOutput(), source=source, task="broken output"
        )
    assert gateway.audit_store.list_runs()[0]["status"] == "failed"


def test_failed_tool_outcome_is_not_overwritten_by_clean_eof(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    input_stream = StringIO(
        "\n".join(
            [
                json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
                ),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "read_file",
                            "arguments": {"path": "missing.txt"},
                        },
                    }
                ),
            ]
        )
    )

    serve_json_lines(
        gateway, input_stream, StringIO(), source=source, task="failed tool"
    )

    assert gateway.audit_store.list_runs()[0]["status"] == "failed"


def test_unexpected_tool_exception_fails_run_before_structured_error_and_eof(
    tmp_path, monkeypatch
):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway, source=source, task="unexpected tool error")
    initialized(session)

    def raise_unexpected(*_args, **_kwargs):
        raise RuntimeError("executor exploded")

    monkeypatch.setattr(gateway, "execute_tool", raise_unexpected)
    response = session.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "list_files", "arguments": {}},
        }
    )

    assert response["error"] == {"code": -32000, "message": "executor exploded"}
    assert gateway.audit_store.get_run(session.run_id)["status"] == "failed"
    session.close()
    assert gateway.audit_store.get_run(session.run_id)["status"] == "failed"
    assert (
        len(
            [
                event
                for event in gateway.audit_store.get_events(session.run_id)
                if event["type"] == "run_finished"
            ]
        )
        == 1
    )


def test_tools_call_validation_stays_in_mcp_content_shape(tmp_path):
    session = McpStdioSession(
        RuntimeGateway.from_home(tmp_path / "project"), task="validation"
    )
    initialized(session)
    response = session.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}}
    )
    assert response["result"]["isError"] is True
    assert "name is required" in response["result"]["content"][0]["text"]


def test_mcp_cli_subprocess_protocol_round_trip(tmp_path):
    source = make_sample_repo(tmp_path)
    repo_root = Path(__file__).resolve().parents[1]
    request_lines = "\n".join(
        [
            json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
            ),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "read_file",
                        "arguments": {"path": "math_utils.py"},
                    },
                }
            ),
        ]
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentpermit",
            "--home",
            str(tmp_path / "home"),
            "mcp",
            "--source",
            str(source),
            "--task",
            "subprocess",
        ],
        cwd=tmp_path,
        input=request_lines,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(repo_root)},
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert responses[-1]["result"]["isError"] is False
    store = AuditStore(tmp_path / "home" / ".agentpermit" / "runs.sqlite")
    runs = store.list_runs()
    assert len(runs) == 1
    assert runs[0]["status"] == "success"
    assert not (tmp_path / ".agentpermit").exists()


def test_mcp_cli_subprocess_persists_failed_status_before_eof(tmp_path):
    source = make_sample_repo(tmp_path)
    repo_root = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agentpermit",
            "--home",
            str(home),
            "mcp",
            "--source",
            str(source),
            "--task",
            "subprocess failure",
        ],
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(repo_root)},
    )
    assert process.stdin is not None
    assert process.stdout is not None
    try:
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "read_file", "arguments": {"path": "missing.txt"}},
            },
        ]
        process.stdin.write(
            "\n".join(json.dumps(request) for request in requests) + "\n"
        )
        process.stdin.flush()
        assert (
            json.loads(process.stdout.readline())["result"]["serverInfo"]["name"]
            == "agentpermit"
        )
        assert json.loads(process.stdout.readline())["result"]["isError"] is True

        store = AuditStore(home / ".agentpermit" / "runs.sqlite")
        assert store.list_runs()[0]["status"] == "failed"
    finally:
        process.stdin.close()
        process.wait(timeout=10)
