from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from threading import Thread

from agentops_control_plane.audit import AuditStore
from agentops_control_plane.web import Dashboard, render_approvals, render_index, render_run


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
