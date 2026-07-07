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
