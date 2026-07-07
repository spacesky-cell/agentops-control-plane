import json
from io import StringIO
from pathlib import Path

from agentops_control_plane.gateway import RuntimeGateway
from agentops_control_plane.mcp_stdio import McpStdioSession, serve_json_lines


def make_sample_repo(root: Path) -> Path:
    source = root / "sample_repo"
    source.mkdir()
    (source / "math_utils.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    return source


def test_mcp_stdio_session_calls_tool_through_gateway(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway)

    start = session.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "run.start",
            "params": {"task": "stdio read", "agent_name": "stdio-agent", "source": str(source)},
        }
    )
    call = session.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tool.call",
            "params": {"name": "read_file", "arguments": {"path": "math_utils.py"}},
        }
    )

    assert start["result"]["run_id"].startswith("run_")
    assert start["result"]["status"] == "running"
    assert call["result"]["status"] == "ok"
    assert call["result"]["output"] == "def add(a, b):\n    return a + b\n"
    assert gateway.audit_store.get_run(start["result"]["run_id"])["agent_name"] == "stdio-agent"


def test_mcp_stdio_json_lines_handles_requests(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    input_stream = StringIO(
        "\n".join(
            [
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "start",
                        "method": "run.start",
                        "params": {
                            "task": "json-lines read",
                            "agent_name": "stdio-agent",
                            "source": str(source),
                        },
                    }
                ),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "read",
                        "method": "tool.call",
                        "params": {"name": "read_file", "arguments": {"path": "math_utils.py"}},
                    }
                ),
            ]
        )
    )
    output_stream = StringIO()

    serve_json_lines(gateway, input_stream, output_stream)
    responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]

    assert responses[0]["id"] == "start"
    assert responses[0]["result"]["status"] == "running"
    assert responses[1]["id"] == "read"
    assert responses[1]["result"]["status"] == "ok"


def test_mcp_stdio_lists_tools_with_input_schemas(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway)

    response = session.handle({"jsonrpc": "2.0", "id": "tools", "method": "tools/list"})

    tools = response["result"]["tools"]
    by_name = {tool["name"]: tool for tool in tools}
    assert set(by_name) == {"list_files", "read_file", "write_file", "patch_text", "run_command"}
    assert by_name["read_file"]["inputSchema"]["required"] == ["path"]
    assert by_name["patch_text"]["inputSchema"]["required"] == ["path", "old", "new"]


def test_mcp_stdio_tools_call_returns_mcp_content_shape(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway)
    session.handle(
        {
            "jsonrpc": "2.0",
            "id": "start",
            "method": "run.start",
            "params": {"task": "stdio read", "agent_name": "stdio-agent", "source": str(source)},
        }
    )

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


def test_mcp_stdio_tools_call_reports_pending_approval_as_mcp_error(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway)
    session.handle(
        {
            "jsonrpc": "2.0",
            "id": "start",
            "method": "run.start",
            "params": {"task": "stdio patch", "agent_name": "stdio-agent", "source": str(source)},
        }
    )

    response = session.handle(
        {
            "jsonrpc": "2.0",
            "id": "patch",
            "method": "tools/call",
            "params": {
                "name": "patch_text",
                "arguments": {"path": "math_utils.py", "old": "return a + b", "new": "return a - b"},
            },
        }
    )

    result = response["result"]
    assert result["isError"] is True
    assert "pending_approval" in result["content"][0]["text"]
    assert "approval_id" in result["content"][0]["text"]
