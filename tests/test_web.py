from __future__ import annotations

import zipfile
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlencode

import pytest

from agentpermit.audit import AuditStore
from agentpermit.snapshot_diff import DiffLimits, compare_snapshot_archives
from agentpermit.web import (
    Dashboard,
    DashboardHTTPServer,
    is_loopback_host,
    render_approvals,
    render_index,
    render_run,
    safe_return_to,
    validate_loopback_host,
)


def request_once(
    store: AuditStore,
    method: str,
    target: str,
    *,
    token: str = "test-csrf-token",
    form: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Dashboard(store, csrf_token=token).app())
    thread = Thread(target=server.handle_request)
    thread.start()
    conn = HTTPConnection("127.0.0.1", server.server_port)
    body = urlencode(form).encode("utf-8") if form is not None else None
    request_headers = {"Content-Type": "application/x-www-form-urlencoded"} if body is not None else {}
    if headers:
        request_headers.update(headers)
    conn.request(method, target, body=body, headers=request_headers)
    response = conn.getresponse()
    response_body = response.read().decode("utf-8")
    response_headers = {key.lower(): value for key, value in response.getheaders()}
    conn.close()
    thread.join(timeout=5)
    server.server_close()
    return response.status, response_headers, response_body


def create_pending_approval(store: AuditStore, root: Path) -> tuple[str, int]:
    run_id = store.start_run("review patch", "agent", root / "workspace")
    approval_id = store.create_approval(
        run_id,
        "patch_text",
        {
            "args": {"path": "math_utils.py", "old": "return a - b", "new": "return a + b"},
            "request_fingerprint": "abc123",
        },
        "Writes require approval.",
    )
    return run_id, approval_id


def write_snapshot(path: Path, files: dict[str, bytes | str]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return path


def add_snapshot_events(store: AuditStore, run_id: str, before: Path, after: Path) -> None:
    store.add_event(run_id, "run_started", "Run started.", {"snapshot": str(before)})
    store.finish_run(run_id, "success", payload={"snapshot": str(after)})


@pytest.mark.parametrize("host", ["127.0.0.1", "127.19.4.2", "::1", "localhost"])
def test_loopback_hosts_are_accepted(host):
    assert is_loopback_host(host)
    validate_loopback_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "example.com", ""])
def test_non_loopback_and_wildcard_hosts_are_rejected_before_binding(host):
    assert not is_loopback_host(host)
    with pytest.raises(ValueError, match="loopback"):
        validate_loopback_host(host)


def test_index_links_to_approvals_and_renders_summary(tmp_path):
    store = AuditStore(tmp_path / "runs.sqlite")
    running_id = store.start_run("active task", "agent", tmp_path / "workspace")
    pending_id, _ = create_pending_approval(store, tmp_path)
    store.pause_run(pending_id)
    store.finish_run(running_id, "success")

    page = render_index(store)

    assert 'href="/approvals"' in page
    assert "Total runs" in page
    assert "Waiting approvals" in page
    assert "Pending requests" in page
    assert "class='status-badge status-success'" in page
    assert "class='status-badge status-waiting-for-approval'" in page


def test_approval_forms_include_csrf_and_required_reviewer_reason(tmp_path):
    store = AuditStore(tmp_path / "runs.sqlite")
    _, approval_id = create_pending_approval(store, tmp_path)

    page = render_approvals(store, csrf_token="<secret&token>")

    assert f"action='/approvals/{approval_id}/approve?return_to=/approvals'" in page
    assert f"formaction='/approvals/{approval_id}/reject?return_to=/approvals'" in page
    assert "name='csrf_token' value='&lt;secret&amp;token&gt;'" in page
    assert "name='reviewer'" in page
    assert "name='reason'" in page
    assert page.count("required") >= 2


@pytest.mark.parametrize("csrf", [None, "wrong-token"])
def test_approval_post_rejects_missing_or_invalid_csrf_without_mutation(tmp_path, csrf):
    store = AuditStore(tmp_path / "runs.sqlite")
    run_id, approval_id = create_pending_approval(store, tmp_path)
    form = {"reviewer": "Alice", "reason": "Reviewed exact patch"}
    if csrf is not None:
        form["csrf_token"] = csrf

    status, _, _ = request_once(store, "POST", f"/approvals/{approval_id}/approve", form=form)

    assert status == 403
    assert store.list_approvals(run_id)[0]["status"] == "pending"


@pytest.mark.parametrize("header", [{"Host": "attacker.example"}, {"Origin": "http://attacker.example"}])
def test_approval_post_rejects_foreign_host_authority_before_csrf(tmp_path, header):
    store = AuditStore(tmp_path / "runs.sqlite")
    run_id, approval_id = create_pending_approval(store, tmp_path)
    form = {
        "csrf_token": "test-csrf-token",
        "reviewer": "Alice",
        "reason": "Reviewed exact patch",
    }

    status, _, _ = request_once(
        store,
        "POST",
        f"/approvals/{approval_id}/approve",
        form=form,
        headers=header,
    )

    assert status == 403
    assert store.list_approvals(run_id)[0]["status"] == "pending"


def test_unknown_approval_post_returns_404_before_form_validation(tmp_path):
    store = AuditStore(tmp_path / "runs.sqlite")

    status, _, body = request_once(
        store,
        "POST",
        "/approvals/999999/approve",
        form={"csrf_token": "test-csrf-token"},
    )

    assert status == 404
    assert "Approval not found" in body


@pytest.mark.parametrize(
    ("field", "value"),
    [("reviewer", ""), ("reviewer", "   "), ("reason", ""), ("reason", "   ")],
)
def test_approval_post_requires_reviewer_and_reason_without_mutation(tmp_path, field, value):
    store = AuditStore(tmp_path / "runs.sqlite")
    run_id, approval_id = create_pending_approval(store, tmp_path)
    form = {
        "csrf_token": "test-csrf-token",
        "reviewer": "Alice <admin>",
        "reason": "Reviewed exact patch",
    }
    form[field] = value

    status, _, body = request_once(store, "POST", f"/approvals/{approval_id}/approve", form=form)

    assert status == 422
    assert "Reviewer and reason are required." in body
    assert store.list_approvals(run_id)[0]["status"] == "pending"


def test_approval_post_persists_reviewer_and_reason_and_escapes_them(tmp_path):
    store = AuditStore(tmp_path / "runs.sqlite")
    run_id, approval_id = create_pending_approval(store, tmp_path)
    form = {
        "csrf_token": "test-csrf-token",
        "reviewer": "Alice <admin>",
        "reason": "Reviewed & accepted <script>alert(1)</script>",
    }

    status, headers, _ = request_once(
        store,
        "POST",
        f"/approvals/{approval_id}/approve?return_to=/runs/{run_id}",
        form=form,
    )

    approval = store.list_approvals(run_id)[0]
    assert status == 303
    assert headers["location"] == f"/runs/{run_id}"
    assert approval["status"] == "approved"
    assert approval["approver"] == "Alice <admin>"
    assert approval["reviewer_reason"] == "Reviewed & accepted <script>alert(1)</script>"
    page = render_run(store, run_id, csrf_token="token")
    assert "Alice &lt;admin&gt;" in page
    assert "Reviewed &amp; accepted &lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<script>alert(1)</script>" not in page


def test_approval_mutation_is_post_only(tmp_path):
    store = AuditStore(tmp_path / "runs.sqlite")
    run_id, approval_id = create_pending_approval(store, tmp_path)

    status, headers, _ = request_once(store, "GET", f"/approvals/{approval_id}/approve")

    assert status == 405
    assert headers["allow"] == "POST"
    assert store.list_approvals(run_id)[0]["status"] == "pending"


def test_dashboard_returns_404_for_unknown_run_api_and_extra_segments(tmp_path):
    store = AuditStore(tmp_path / "runs.sqlite")
    run_id = store.start_run("trace", "agent", tmp_path / "workspace")

    for target in ("/missing", "/runs/unknown", "/api/runs/unknown", f"/runs/{run_id}/extra"):
        status, _, _ = request_once(store, "GET", target)
        assert status == 404


def test_dashboard_adds_defensive_response_headers(tmp_path):
    store = AuditStore(tmp_path / "runs.sqlite")

    status, headers, _ = request_once(store, "GET", "/")

    assert status == 200
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert "default-src 'none'" in headers["content-security-policy"]
    assert headers["cache-control"] == "no-store"


def test_snapshot_diff_reports_created_modified_deleted_and_unchanged_once(tmp_path):
    before = write_snapshot(
        tmp_path / "before.zip",
        {"modified.txt": "old\n", "deleted.txt": "gone\n", "same.txt": "same\n"},
    )
    after = write_snapshot(
        tmp_path / "after.zip",
        {"modified.txt": "new\n", "created.txt": "hello\n", "same.txt": "same\n"},
    )

    result = compare_snapshot_archives(before, after)

    assert result.available
    assert result.counts == {"created": 1, "modified": 1, "deleted": 1, "unchanged": 1}
    assert [entry.path for entry in result.entries] == ["created.txt", "deleted.txt", "modified.txt"]
    assert [entry.path for entry in result.entries].count("modified.txt") == 1
    assert "-old" in result.entries[2].diff
    assert "+new" in result.entries[2].diff


def test_snapshot_diff_renders_binary_and_oversized_files_as_metadata(tmp_path):
    before = write_snapshot(
        tmp_path / "before.zip",
        {"binary.dat": b"\x00before", "large.txt": "a" * 100},
    )
    after = write_snapshot(
        tmp_path / "after.zip",
        {"binary.dat": b"\x00after", "large.txt": "b" * 100},
    )

    result = compare_snapshot_archives(
        before,
        after,
        limits=DiffLimits(max_text_bytes=32, max_total_bytes=1024, max_files=20),
    )

    entries = {entry.path: entry for entry in result.entries}
    assert entries["binary.dat"].display == "binary"
    assert entries["binary.dat"].diff is None
    assert entries["large.txt"].display == "oversized"
    assert entries["large.txt"].diff is None
    assert entries["large.txt"].before_size == 100
    assert entries["large.txt"].after_size == 100


def test_snapshot_diff_fails_closed_for_missing_corrupt_and_excessive_archives(tmp_path):
    valid = write_snapshot(tmp_path / "valid.zip", {"a.txt": "a"})
    corrupt = tmp_path / "corrupt.zip"
    corrupt.write_bytes(b"not a zip")
    excessive = write_snapshot(tmp_path / "excessive.zip", {"a.txt": "a", "b.txt": "b"})

    missing_result = compare_snapshot_archives(tmp_path / "missing.zip", valid)
    corrupt_result = compare_snapshot_archives(corrupt, valid)
    excessive_result = compare_snapshot_archives(
        valid, excessive, limits=DiffLimits(max_files=1, max_total_bytes=1024, max_text_bytes=32)
    )

    assert not missing_result.available and missing_result.reason == "snapshot_missing"
    assert not corrupt_result.available and corrupt_result.reason == "snapshot_invalid"
    assert not excessive_result.available and excessive_result.reason == "snapshot_limits_exceeded"


@pytest.mark.parametrize(
    ("before_text", "after_text"),
    [("same\n", "same"), ("same\r\n", "same\n")],
)
def test_snapshot_diff_renders_newline_only_changes_as_human_readable(tmp_path, before_text, after_text):
    before = write_snapshot(tmp_path / "before.zip", {"same.txt": before_text})
    after = write_snapshot(tmp_path / "after.zip", {"same.txt": after_text})

    result = compare_snapshot_archives(before, after)

    entry = result.entries[0]
    assert entry.status == "modified"
    assert entry.display == "text"
    assert entry.diff
    assert "newline" in entry.diff.lower()


@pytest.mark.parametrize(
    ("before_files", "after_files", "expected_status"),
    [({}, {"empty.txt": b""}, "created"), ({"empty.txt": b""}, {}, "deleted")],
)
def test_snapshot_diff_handles_created_and_deleted_empty_files(
    tmp_path, before_files, after_files, expected_status
):
    before = write_snapshot(tmp_path / "before.zip", before_files)
    after = write_snapshot(tmp_path / "after.zip", after_files)

    result = compare_snapshot_archives(before, after)

    entry = result.entries[0]
    assert entry.status == expected_status
    assert entry.display == "text"
    assert entry.diff
    assert "empty.txt" in entry.diff
    assert "empty" in entry.diff.lower()


def test_dashboard_server_bounds_threads_and_request_timeouts(tmp_path):
    store = AuditStore(tmp_path / "runs.sqlite")
    server = DashboardHTTPServer(("127.0.0.1", 0), Dashboard(store).app())
    try:
        assert server.max_workers <= 32
        assert server.request_timeout <= 30
        assert server.daemon_threads
    finally:
        server.server_close()


def test_validation_error_repopulates_only_matching_approval_row(tmp_path):
    store = AuditStore(tmp_path / "runs.sqlite")
    _, first_id = create_pending_approval(store, tmp_path)
    _, second_id = create_pending_approval(store, tmp_path)

    page = render_approvals(
        store,
        csrf_token="token",
        approval_error=(first_id, "Reviewer and reason are required."),
        form_values={"reviewer": "First reviewer", "reason": "First reason"},
    )

    def row_segment(approval_id: int) -> str:
        action_start = page.index(f"action='/approvals/{approval_id}/approve")
        row_start = page.rfind("<tr>", 0, action_start)
        row_end = page.index("</tr>", action_start)
        return page[row_start:row_end]

    first_segment = row_segment(first_id)
    second_segment = row_segment(second_id)
    assert "First reviewer" in first_segment
    assert "First reason" in first_segment
    assert "First reviewer" not in second_segment
    assert "First reason" not in second_segment


def test_run_page_uses_event_referenced_snapshots_and_shows_diff_metrics(tmp_path):
    store = AuditStore(tmp_path / "runs.sqlite")
    run_id = store.start_run("diff", "agent", tmp_path / "workspace")
    before = write_snapshot(tmp_path / "actual-before.zip", {"old.txt": "before\n"})
    after = write_snapshot(tmp_path / "actual-after.zip", {"old.txt": "after\n", "new.txt": "new\n"})
    add_snapshot_events(store, run_id, before, after)

    page = render_run(store, run_id, csrf_token="token")

    assert "Snapshot changes" in page
    assert "Created" in page and ">1<" in page
    assert "Modified" in page
    assert page.count("data-diff-path='old.txt'") == 1
    assert "new.txt" in page
    assert "-before" in page and "+after" in page


def test_run_page_shows_structured_unavailable_snapshot_evidence(tmp_path):
    store = AuditStore(tmp_path / "runs.sqlite")
    run_id = store.start_run("diff", "agent", tmp_path / "workspace")
    add_snapshot_events(store, run_id, tmp_path / "missing-before.zip", tmp_path / "missing-after.zip")

    page = render_run(store, run_id, csrf_token="token")

    assert "Snapshot evidence unavailable" in page
    assert "snapshot_missing" in page
    assert "Created</span><strong>0</strong>" not in page


def test_run_page_filters_events_and_uses_native_collapsible_payloads(tmp_path):
    store = AuditStore(tmp_path / "runs.sqlite")
    run_id = store.start_run("trace", "agent", tmp_path / "workspace")
    store.add_event(run_id, "policy_decision", "Allowed.", {"decision": "allow"})
    store.add_event(run_id, "tool_result", "Completed.", {"ok": True}, tool_name="read_file")

    page = render_run(store, run_id, csrf_token="token", event_type="tool_result")

    assert "value='tool_result' selected" in page
    assert "Completed." in page
    assert "Allowed." not in page
    assert "<details class='event-payload'>" in page
    assert "<summary>Payload</summary>" in page


def test_run_page_rejects_unknown_event_filter_without_hiding_audit_events(tmp_path):
    store = AuditStore(tmp_path / "runs.sqlite")
    run_id = store.start_run("trace", "agent", tmp_path / "workspace")
    store.add_event(run_id, "policy_decision", "Allowed.", {"decision": "allow"})

    page = render_run(store, run_id, csrf_token="token", event_type="not-present")

    assert "Allowed." in page
    assert "Unknown event filter" in page


def test_safe_return_to_rejects_external_and_protocol_relative_paths():
    assert safe_return_to("return_to=https://example.com") == "/approvals"
    assert safe_return_to("return_to=//example.com") == "/approvals"
    assert safe_return_to("return_to=/runs/run_123") == "/runs/run_123"
    assert safe_return_to("return_to=/runs/not-a-run-id") == "/approvals"
