from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from threading import Thread

from agentops_control_plane.audit import AuditStore
from agentops_control_plane.gateway import RuntimeGateway
from agentops_control_plane.mcp_adapter import McpPlanAdapter
from agentops_control_plane.web import Dashboard, render_approvals, render_index, render_run, safe_return_to


def make_sample_repo(root):
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


def write_mcp_plan(path, calls):
    import json

    path.write_text(json.dumps({"name": "web-mcp-agent", "tool_calls": calls}), encoding="utf-8")
    return path


def test_index_links_to_approvals(tmp_path):
    store = AuditStore(tmp_path / "runs.sqlite")

    html = render_index(store)

    assert "href='/approvals'" in html
    assert "Approvals" in html


def test_approvals_page_lists_pending_requests(tmp_path):
    store = AuditStore(tmp_path / "runs.sqlite")
    run_id = store.start_run("review patch", "agent", tmp_path / "workspace")
    approval_id = store.create_approval(
        run_id,
        "patch_text",
        {
            "args": {"path": "math_utils.py", "old": "return a - b", "new": "return a + b"},
            "requested_by": "agent",
            "request_fingerprint": "abc123",
        },
        "Writes require approval.",
    )

    html = render_approvals(store)

    assert str(approval_id) in html
    assert run_id in html
    assert "pending" in html
    assert "patch_text" in html
    assert "math_utils.py" in html
    assert "return a + b" in html
    assert f"action='/approvals/{approval_id}/approve?return_to=/approvals'" in html
    assert f"action='/approvals/{approval_id}/reject?return_to=/approvals'" in html


def test_run_page_shows_patch_diff_from_audit_events(tmp_path):
    store = AuditStore(tmp_path / "runs.sqlite")
    run_id = store.start_run("fix add", "agent", tmp_path / "workspace")
    store.add_event(
        run_id,
        "policy_decision",
        "Patch requires approval.",
        {
            "args": {"path": "math_utils.py", "old": "return a - b", "new": "return a + b"},
        },
        tool_name="patch_text",
    )

    html = render_run(store, run_id)

    assert "Patch Diff" in html
    assert "math_utils.py" in html
    assert "- return a - b" in html
    assert "+ return a + b" in html


def test_run_page_lists_approvals_for_that_run(tmp_path):
    store = AuditStore(tmp_path / "runs.sqlite")
    run_id = store.start_run("review patch", "agent", tmp_path / "workspace")
    other_run_id = store.start_run("other run", "agent", tmp_path / "other")
    approval_id = store.create_approval(
        run_id,
        "patch_text",
        {"args": {"path": "math_utils.py"}, "request_fingerprint": "abc123"},
        "Patch requires approval.",
    )
    store.create_approval(
        other_run_id,
        "patch_text",
        {"args": {"path": "other.py"}, "request_fingerprint": "def456"},
        "Other approval.",
    )

    html = render_run(store, run_id)

    assert "Approvals" in html
    assert "Patch requires approval." in html
    assert "math_utils.py" in html
    assert "other.py" not in html
    assert f"action='/approvals/{approval_id}/approve?return_to=/runs/{run_id}'" in html
    assert f"action='/approvals/{approval_id}/reject?return_to=/runs/{run_id}'" in html


def test_run_page_shows_resume_for_waiting_mcp_plan(tmp_path):
    store = AuditStore(tmp_path / "runs.sqlite")
    run_id = store.start_run("review patch", "agent", tmp_path / "workspace")
    store.pause_run(run_id)
    store.set_run_metadata(run_id, {"adapter": "mcp-plan", "plan_path": str(tmp_path / "plan.json")})

    html = render_run(store, run_id)

    assert f"action='/runs/{run_id}/resume'" in html
    assert "Resume" in html


def test_dashboard_routes_approvals_page(tmp_path):
    store = AuditStore(tmp_path / "runs.sqlite")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Dashboard(store).app())
    thread = Thread(target=server.handle_request)
    thread.start()
    conn = HTTPConnection("127.0.0.1", server.server_port)

    conn.request("GET", "/approvals")
    response = conn.getresponse()
    body = response.read().decode("utf-8")

    conn.close()
    thread.join(timeout=5)
    server.server_close()
    assert response.status == 200
    assert "<h1>Approvals</h1>" in body


def test_dashboard_post_approves_pending_request(tmp_path):
    store = AuditStore(tmp_path / "runs.sqlite")
    run_id = store.start_run("review patch", "agent", tmp_path / "workspace")
    approval_id = store.create_approval(
        run_id,
        "patch_text",
        {"args": {"path": "math_utils.py"}, "request_fingerprint": "abc123"},
        "Patch requires approval.",
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), Dashboard(store).app())
    thread = Thread(target=server.handle_request)
    thread.start()
    conn = HTTPConnection("127.0.0.1", server.server_port)

    conn.request("POST", f"/approvals/{approval_id}/approve")
    response = conn.getresponse()
    response.read()

    conn.close()
    thread.join(timeout=5)
    server.server_close()
    approval = store.list_approvals(run_id)[0]
    assert response.status == 303
    assert response.getheader("Location") == "/approvals"
    assert approval["status"] == "approved"
    assert approval["approver"] == "dashboard"


def test_dashboard_post_approves_from_run_page_and_redirects_back(tmp_path):
    store = AuditStore(tmp_path / "runs.sqlite")
    run_id = store.start_run("review patch", "agent", tmp_path / "workspace")
    approval_id = store.create_approval(
        run_id,
        "patch_text",
        {"args": {"path": "math_utils.py"}, "request_fingerprint": "abc123"},
        "Patch requires approval.",
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), Dashboard(store).app())
    thread = Thread(target=server.handle_request)
    thread.start()
    conn = HTTPConnection("127.0.0.1", server.server_port)

    conn.request("POST", f"/approvals/{approval_id}/approve?return_to=/runs/{run_id}")
    response = conn.getresponse()
    response.read()

    conn.close()
    thread.join(timeout=5)
    server.server_close()
    approval = store.list_approvals(run_id)[0]
    assert response.status == 303
    assert response.getheader("Location") == f"/runs/{run_id}"
    assert approval["status"] == "approved"


def test_dashboard_post_rejects_pending_request(tmp_path):
    store = AuditStore(tmp_path / "runs.sqlite")
    run_id = store.start_run("review patch", "agent", tmp_path / "workspace")
    approval_id = store.create_approval(
        run_id,
        "patch_text",
        {"args": {"path": "math_utils.py"}, "request_fingerprint": "abc123"},
        "Patch requires approval.",
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), Dashboard(store).app())
    thread = Thread(target=server.handle_request)
    thread.start()
    conn = HTTPConnection("127.0.0.1", server.server_port)

    conn.request("POST", f"/approvals/{approval_id}/reject")
    response = conn.getresponse()
    response.read()

    conn.close()
    thread.join(timeout=5)
    server.server_close()
    approval = store.list_approvals(run_id)[0]
    assert response.status == 303
    assert response.getheader("Location") == "/approvals"
    assert approval["status"] == "rejected"
    assert approval["approver"] == "dashboard"


def test_dashboard_post_unknown_approval_returns_not_found(tmp_path):
    store = AuditStore(tmp_path / "runs.sqlite")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Dashboard(store).app())
    thread = Thread(target=server.handle_request)
    thread.start()
    conn = HTTPConnection("127.0.0.1", server.server_port)

    conn.request("POST", "/approvals/999/approve")
    response = conn.getresponse()
    response.read()

    conn.close()
    thread.join(timeout=5)
    server.server_close()
    assert response.status == 404


def test_dashboard_post_ignores_external_return_to(tmp_path):
    store = AuditStore(tmp_path / "runs.sqlite")
    run_id = store.start_run("review patch", "agent", tmp_path / "workspace")
    approval_id = store.create_approval(
        run_id,
        "patch_text",
        {"args": {"path": "math_utils.py"}, "request_fingerprint": "abc123"},
        "Patch requires approval.",
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), Dashboard(store).app())
    thread = Thread(target=server.handle_request)
    thread.start()
    conn = HTTPConnection("127.0.0.1", server.server_port)

    conn.request("POST", f"/approvals/{approval_id}/approve?return_to=https://example.com")
    response = conn.getresponse()
    response.read()

    conn.close()
    thread.join(timeout=5)
    server.server_close()
    assert response.status == 303
    assert response.getheader("Location") == "/approvals"


def test_dashboard_post_resumes_approved_mcp_plan(tmp_path):
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
    run_id = adapter.run(gateway, "web resume mcp plan", source=source, auto_approve=False)
    approval = gateway.audit_store.list_approvals(run_id)[0]
    gateway.audit_store.decide_approval(approval["id"], "approved", "dashboard", "approved from test")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Dashboard(gateway.audit_store).app())
    thread = Thread(target=server.handle_request)
    thread.start()
    conn = HTTPConnection("127.0.0.1", server.server_port)

    conn.request("POST", f"/runs/{run_id}/resume")
    response = conn.getresponse()
    response.read()

    conn.close()
    thread.join(timeout=5)
    server.server_close()
    run = gateway.audit_store.get_run(run_id)
    approvals = gateway.audit_store.list_approvals(run_id)
    assert response.status == 303
    assert response.getheader("Location") == f"/runs/{run_id}"
    assert run["status"] == "success"
    assert approvals[0]["status"] == "consumed"


def test_dashboard_post_resume_without_approval_returns_conflict(tmp_path):
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
    run_id = adapter.run(gateway, "web resume mcp plan", source=source, auto_approve=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Dashboard(gateway.audit_store).app())
    thread = Thread(target=server.handle_request)
    thread.start()
    conn = HTTPConnection("127.0.0.1", server.server_port)

    conn.request("POST", f"/runs/{run_id}/resume")
    response = conn.getresponse()
    response.read()

    conn.close()
    thread.join(timeout=5)
    server.server_close()
    run = gateway.audit_store.get_run(run_id)
    assert response.status == 409
    assert run["status"] == "waiting_for_approval"


def test_safe_return_to_rejects_protocol_relative_paths():
    assert safe_return_to("return_to=//example.com") == "/approvals"
    assert safe_return_to("return_to=/runs/run_123") == "/runs/run_123"
    assert safe_return_to("return_to=/runs/not-a-run-id") == "/approvals"
