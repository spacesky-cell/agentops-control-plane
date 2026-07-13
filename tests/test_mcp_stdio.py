import json
from io import StringIO
from pathlib import Path

from agentpermit import __version__
from agentpermit.gateway import RuntimeGateway
from agentpermit.mcp_stdio import McpStdioSession, serve_json_lines


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
    session.handle({"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}})
    session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})

    response = session.handle({"jsonrpc": "2.0", "id": "tools", "method": "tools/list"})

    tools = response["result"]["tools"]
    by_name = {tool["name"]: tool for tool in tools}
    assert set(by_name) == {"list_files", "read_file", "write_file", "patch_text", "run_command"}
    assert by_name["read_file"]["inputSchema"]["required"] == ["path"]
    assert by_name["patch_text"]["inputSchema"]["required"] == ["path", "old", "new"]


def test_mcp_stdio_lists_empty_resources_after_initialize(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway)
    session.handle({"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}})
    session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})

    response = session.handle({"jsonrpc": "2.0", "id": "resources", "method": "resources/list"})

    assert response == {"jsonrpc": "2.0", "id": "resources", "result": {"resources": []}}


def test_mcp_stdio_lists_empty_prompts_after_initialize(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway)
    session.handle({"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}})
    session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})

    response = session.handle({"jsonrpc": "2.0", "id": "prompts", "method": "prompts/list"})

    assert response == {"jsonrpc": "2.0", "id": "prompts", "result": {"prompts": []}}


def test_mcp_stdio_initialize_returns_server_capabilities(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway)

    response = session.handle({"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}})

    assert response["result"]["protocolVersion"] == "2025-06-18"
    assert response["result"]["serverInfo"] == {
        "name": "agentpermit",
        "version": __version__,
    }
    assert response["result"]["capabilities"]["tools"] == {"listChanged": False}
    assert response["result"]["capabilities"]["resources"] == {"listChanged": False}
    assert response["result"]["capabilities"]["prompts"] == {"listChanged": False}


def test_mcp_stdio_json_lines_skips_initialized_notification_response(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    input_stream = StringIO(
        "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}}),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                json.dumps({"jsonrpc": "2.0", "id": "tools", "method": "tools/list"}),
            ]
        )
    )
    output_stream = StringIO()

    serve_json_lines(gateway, input_stream, output_stream)
    responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]

    assert [response["id"] for response in responses] == ["init", "tools"]
    assert responses[1]["result"]["tools"]


def test_mcp_stdio_requires_initialize_before_mcp_methods(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway)

    response = session.handle({"jsonrpc": "2.0", "id": "tools", "method": "tools/list"})

    assert response["error"] == {
        "code": -32002,
        "message": "Session is not initialized.",
    }


def test_mcp_stdio_requires_initialized_notification_before_mcp_methods(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway)
    session.handle({"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}})

    response = session.handle({"jsonrpc": "2.0", "id": "tools", "method": "tools/list"})

    assert response["error"] == {
        "code": -32002,
        "message": "Session is not initialized.",
    }


def test_mcp_stdio_ignores_initialized_notification_before_initialize(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway)

    notification = session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    response = session.handle({"jsonrpc": "2.0", "id": "tools", "method": "tools/list"})

    assert notification is None
    assert response["error"] == {
        "code": -32002,
        "message": "Session is not initialized.",
    }


def test_mcp_stdio_tools_call_requires_initialized_session(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway)

    response = session.handle(
        {
            "jsonrpc": "2.0",
            "id": "read",
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "math_utils.py"}},
        }
    )

    assert response["error"] == {
        "code": -32002,
        "message": "Session is not initialized.",
    }


def test_mcp_stdio_ping_works_before_initialize(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway)

    response = session.handle({"jsonrpc": "2.0", "id": "ping", "method": "ping"})

    assert response == {"jsonrpc": "2.0", "id": "ping", "result": {}}


def test_mcp_stdio_unknown_method_uses_json_rpc_method_not_found(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway)
    session.handle({"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}})
    session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})

    response = session.handle({"jsonrpc": "2.0", "id": "bad", "method": "unknown/method"})

    assert response["error"] == {
        "code": -32601,
        "message": "Method not found: unknown/method",
    }


def test_mcp_stdio_tools_call_returns_mcp_content_shape(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway)
    session.handle({"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}})
    session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
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
    session.handle({"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}})
    session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
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


def test_mcp_stdio_tools_call_reports_missing_name_as_mcp_error(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway)
    session.handle({"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}})
    session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    session.handle(
        {
            "jsonrpc": "2.0",
            "id": "start",
            "method": "run.start",
            "params": {"task": "stdio invalid", "agent_name": "stdio-agent", "source": str(source)},
        }
    )

    response = session.handle({"jsonrpc": "2.0", "id": "read", "method": "tools/call", "params": {}})

    assert "error" not in response
    assert response["result"]["isError"] is True
    assert "name" in response["result"]["content"][0]["text"]


def test_mcp_stdio_tools_call_reports_non_object_arguments_as_mcp_error(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway)
    session.handle({"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}})
    session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    session.handle(
        {
            "jsonrpc": "2.0",
            "id": "start",
            "method": "run.start",
            "params": {"task": "stdio invalid", "agent_name": "stdio-agent", "source": str(source)},
        }
    )

    response = session.handle(
        {
            "jsonrpc": "2.0",
            "id": "read",
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": "math_utils.py"},
        }
    )

    assert "error" not in response
    assert response["result"]["isError"] is True
    assert "arguments" in response["result"]["content"][0]["text"]


def test_mcp_stdio_tools_call_reports_missing_required_argument_as_mcp_error(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway)
    session.handle({"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}})
    session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    session.handle(
        {
            "jsonrpc": "2.0",
            "id": "start",
            "method": "run.start",
            "params": {"task": "stdio invalid", "agent_name": "stdio-agent", "source": str(source)},
        }
    )

    response = session.handle(
        {"jsonrpc": "2.0", "id": "read", "method": "tools/call", "params": {"name": "read_file"}}
    )

    assert "error" not in response
    assert response["result"]["isError"] is True
    assert "path" in response["result"]["content"][0]["text"]
    assert "required" in response["result"]["content"][0]["text"]


def test_mcp_stdio_tools_call_reports_unexpected_argument_as_mcp_error(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway)
    session.handle({"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}})
    session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    session.handle(
        {
            "jsonrpc": "2.0",
            "id": "start",
            "method": "run.start",
            "params": {"task": "stdio invalid", "agent_name": "stdio-agent", "source": str(source)},
        }
    )

    response = session.handle(
        {
            "jsonrpc": "2.0",
            "id": "read",
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "math_utils.py", "extra": True}},
        }
    )

    assert "error" not in response
    assert response["result"]["isError"] is True
    assert "extra" in response["result"]["content"][0]["text"]
    assert "unexpected" in response["result"]["content"][0]["text"]


def test_mcp_stdio_tools_call_reports_wrong_argument_type_as_mcp_error(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway)
    session.handle({"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}})
    session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    session.handle(
        {
            "jsonrpc": "2.0",
            "id": "start",
            "method": "run.start",
            "params": {"task": "stdio invalid", "agent_name": "stdio-agent", "source": str(source)},
        }
    )

    response = session.handle(
        {
            "jsonrpc": "2.0",
            "id": "read",
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": 42}},
        }
    )

    assert "error" not in response
    assert response["result"]["isError"] is True
    assert "path" in response["result"]["content"][0]["text"]
    assert "string" in response["result"]["content"][0]["text"]


def test_mcp_stdio_tools_call_reports_unknown_tool_as_mcp_error(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway)
    session.handle({"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}})
    session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    session.handle(
        {
            "jsonrpc": "2.0",
            "id": "start",
            "method": "run.start",
            "params": {"task": "stdio invalid", "agent_name": "stdio-agent", "source": str(source)},
        }
    )

    response = session.handle(
        {"jsonrpc": "2.0", "id": "read", "method": "tools/call", "params": {"name": "missing_tool"}}
    )

    assert "error" not in response
    assert response["result"]["isError"] is True
    assert response["result"]["content"][0]["text"] == "Invalid tools/call params: unknown tool: missing_tool."


def test_mcp_stdio_missing_method_uses_invalid_request_error(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway)

    response = session.handle({"jsonrpc": "2.0", "id": "missing"})

    assert response["error"] == {
        "code": -32600,
        "message": "Invalid Request: missing method.",
    }


def test_mcp_stdio_ignores_unknown_notifications(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway)

    response = session.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}})

    assert response is None


def test_mcp_stdio_rejects_invalid_jsonrpc_version(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway)

    response = session.handle({"jsonrpc": "1.0", "id": "bad-version", "method": "ping"})

    assert response == {
        "jsonrpc": "2.0",
        "id": "bad-version",
        "error": {"code": -32600, "message": "Invalid Request: jsonrpc must be '2.0'."},
    }


def test_mcp_stdio_rejects_missing_request_id_for_response_methods(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway)

    response = session.handle({"jsonrpc": "2.0", "method": "ping"})

    assert response == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "Invalid Request: id is required."},
    }


def test_mcp_stdio_rejects_invalid_request_id_type(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway)

    response = session.handle({"jsonrpc": "2.0", "id": {"nested": "bad"}, "method": "ping"})

    assert response == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "Invalid Request: id must be a string or integer."},
    }


def test_mcp_stdio_rejects_non_object_params(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    session = McpStdioSession(gateway)

    response = session.handle({"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": []})

    assert response == {
        "jsonrpc": "2.0",
        "id": "init",
        "error": {"code": -32600, "message": "Invalid Request: params must be an object."},
    }


def test_mcp_stdio_json_lines_rejects_non_object_request(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    input_stream = StringIO(json.dumps(["ping"]) + "\n")
    output_stream = StringIO()

    serve_json_lines(gateway, input_stream, output_stream)
    response = json.loads(output_stream.getvalue())

    assert response == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "Invalid Request: request must be an object."},
    }


def test_mcp_stdio_json_lines_returns_parse_error_for_invalid_json(tmp_path):
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    input_stream = StringIO("{not-json}\n")
    output_stream = StringIO()

    serve_json_lines(gateway, input_stream, output_stream)
    response = json.loads(output_stream.getvalue())

    assert response == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": "Parse error."},
    }
