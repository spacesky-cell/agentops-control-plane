from __future__ import annotations

import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .audit import ApprovalNotFoundError, AuditStore
from .config import PolicyConfig
from .gateway import RuntimeGateway
from .mcp_adapter import McpPlanAdapter
from .policy import PolicyEngine
from .tools import ToolExecutor
from .workspace import WorkspaceManager


class Dashboard:
    def __init__(self, store: AuditStore) -> None:
        self.store = store

    def app(self) -> type[BaseHTTPRequestHandler]:
        store = self.store

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    self._send_html(render_index(store))
                    return
                if parsed.path == "/approvals":
                    self._send_html(render_approvals(store))
                    return
                if parsed.path.startswith("/runs/"):
                    run_id = parsed.path.split("/", 2)[2]
                    self._send_html(render_run(store, run_id))
                    return
                if parsed.path.startswith("/api/runs/"):
                    run_id = parsed.path.split("/", 3)[3]
                    payload = {
                        "run": store.get_run(run_id),
                        "events": store.get_events(run_id),
                        "approvals": store.list_approvals(run_id),
                    }
                    self._send_json(payload)
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
                parsed = urlparse(self.path)
                parts = [part for part in parsed.path.split("/") if part]
                if len(parts) == 3 and parts[0] == "approvals" and parts[2] in {"approve", "reject"}:
                    try:
                        approval_id = int(parts[1])
                    except ValueError:
                        self.send_response(404)
                        self.end_headers()
                        return
                    status = "approved" if parts[2] == "approve" else "rejected"
                    try:
                        store.decide_approval(approval_id, status, "dashboard", f"{status} from dashboard")
                    except ApprovalNotFoundError:
                        self.send_response(404)
                        self.end_headers()
                        return
                    self._redirect(safe_return_to(parsed.query))
                    return
                if len(parts) == 3 and parts[0] == "runs" and parts[2] == "resume":
                    resume_status = resume_run(store, parts[1])
                    if resume_status == "resumed":
                        self._redirect(f"/runs/{parts[1]}")
                    elif resume_status == "not_found":
                        self.send_response(404)
                        self.end_headers()
                    else:
                        self.send_response(409)
                        self.end_headers()
                    return
                self.send_response(404)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

            def _send_html(self, body: str) -> None:
                encoded = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def _send_json(self, payload: object) -> None:
                encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def _redirect(self, location: str) -> None:
                self.send_response(303)
                self.send_header("Location", location)
                self.end_headers()

        return Handler


def serve(store: AuditStore, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Dashboard(store).app())
    server.serve_forever()
    return server


def render_shell(title: str, content: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 32px; color: #17202a; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #d5d8dc; padding: 8px; vertical-align: top; }}
    th {{ background: #f4f6f7; text-align: left; }}
    a {{ color: #1f618d; }}
    pre {{ white-space: pre-wrap; background: #f8f9f9; padding: 8px; }}
  </style>
</head>
<body>{content}</body>
</html>"""


def render_index(store: AuditStore) -> str:
    rows = []
    for run in store.list_runs():
        rows.append(
            "<tr>"
            f"<td><a href='/runs/{html.escape(run['id'])}'>{html.escape(run['id'])}</a></td>"
            f"<td>{html.escape(run['status'])}</td>"
            f"<td>{html.escape(run['agent_name'])}</td>"
            f"<td>{html.escape(run['started_at'])}</td>"
            f"<td>{html.escape(run['task'])}</td>"
            "</tr>"
        )
    content = (
        "<h1>AgentOps Control Plane</h1>"
        "<p><a href='/approvals'>Approvals</a></p>"
        "<table><thead><tr><th>Run</th><th>Status</th><th>Agent</th>"
        "<th>Started</th><th>Task</th></tr></thead><tbody>"
        f"{''.join(rows) or '<tr><td colspan=5>No runs yet</td></tr>'}"
        "</tbody></table>"
    )
    return render_shell("AgentOps Control Plane", content)


def render_approvals(store: AuditStore) -> str:
    rows = render_approval_rows(store.list_approvals(), return_to="/approvals")
    content = (
        "<p><a href='/'>Back</a></p><h1>Approvals</h1>"
        "<table><thead><tr><th>ID</th><th>Run</th><th>Status</th><th>Tool</th>"
        "<th>Requested</th><th>Request</th></tr></thead><tbody>"
        f"{rows or '<tr><td colspan=6>No approvals yet</td></tr>'}"
        "</tbody></table>"
    )
    return render_shell("Approvals", content)


def render_approval_rows(approvals: list[dict[str, object]], return_to: str) -> str:
    rows = []
    for approval in approvals:
        payload = html.escape(json.dumps(approval["payload"], ensure_ascii=False, indent=2))
        run_id = str(approval["run_id"])
        escaped_run_id = html.escape(run_id)
        actions = render_approval_actions(int(approval["id"]), str(approval["status"]), return_to)
        rows.append(
            "<tr>"
            f"<td>{approval['id']}</td>"
            f"<td><a href='/runs/{escaped_run_id}'>{escaped_run_id}</a></td>"
            f"<td>{html.escape(approval['status'])}</td>"
            f"<td>{html.escape(approval['tool_name'])}</td>"
            f"<td>{html.escape(approval['requested_at'])}</td>"
            f"<td>{html.escape(str(approval.get('reason') or ''))}<pre>{payload}</pre>{actions}</td>"
            "</tr>"
        )
    return "".join(rows)


def render_approval_actions(approval_id: int, status: str, return_to: str) -> str:
    if status != "pending":
        return ""
    escaped_return_to = html.escape(return_to, quote=True)
    return (
        "<div>"
        f"<form method='post' action='/approvals/{approval_id}/approve?return_to={escaped_return_to}' "
        "style='display:inline'>"
        "<button type='submit'>Approve</button>"
        "</form> "
        f"<form method='post' action='/approvals/{approval_id}/reject?return_to={escaped_return_to}' "
        "style='display:inline'>"
        "<button type='submit'>Reject</button>"
        "</form>"
        "</div>"
    )


def render_run(store: AuditStore, run_id: str) -> str:
    run = store.get_run(run_id)
    if not run:
        return render_shell("Run not found", "<h1>Run not found</h1>")
    event_rows = []
    events = store.get_events(run_id)
    for event in events:
        payload = html.escape(json.dumps(event["payload"], ensure_ascii=False, indent=2))
        event_rows.append(
            "<tr>"
            f"<td>{event['id']}</td>"
            f"<td>{html.escape(event['type'])}</td>"
            f"<td>{html.escape(str(event.get('tool_name') or ''))}</td>"
            f"<td>{html.escape(str(event.get('risk') or ''))}</td>"
            f"<td>{html.escape(event['message'])}<pre>{payload}</pre></td>"
            "</tr>"
        )
    diff_rows = render_patch_diffs(events)
    approval_rows = render_approval_rows(store.list_approvals(run_id), return_to=f"/runs/{run_id}")
    approval_table = (
        "<h2>Approvals</h2><table><thead><tr><th>ID</th><th>Run</th><th>Status</th><th>Tool</th>"
        "<th>Requested</th><th>Request</th></tr></thead><tbody>"
        f"{approval_rows or '<tr><td colspan=6>No approvals for this run</td></tr>'}"
        "</tbody></table>"
    )
    resume_action = render_resume_action(store, run)
    content = (
        f"<p><a href='/'>Back</a></p><h1>{html.escape(run_id)}</h1>"
        f"<p><strong>Status:</strong> {html.escape(run['status'])}</p>"
        f"<p><strong>Task:</strong> {html.escape(run['task'])}</p>"
        f"<p><strong>Workspace:</strong> {html.escape(run['workspace_path'])}</p>"
        f"{resume_action}"
        f"{diff_rows}"
        f"{approval_table}"
        "<h2>Events</h2><table><thead><tr><th>ID</th><th>Type</th><th>Tool</th>"
        "<th>Risk</th><th>Message</th></tr></thead><tbody>"
        f"{''.join(event_rows)}</tbody></table>"
    )
    return render_shell(f"Run {run_id}", content)


def render_resume_action(store: AuditStore, run: dict[str, object]) -> str:
    metadata = store.get_run_metadata(str(run["id"]))
    if run["status"] != "waiting_for_approval" or metadata.get("adapter") != "mcp-plan":
        return ""
    run_id = html.escape(str(run["id"]))
    return (
        "<form method='post' "
        f"action='/runs/{run_id}/resume'>"
        "<button type='submit'>Resume</button>"
        "</form>"
    )


def render_patch_diffs(events: list[dict[str, object]]) -> str:
    rows = []
    for event in events:
        if event.get("tool_name") != "patch_text":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        args = payload.get("args")
        if not isinstance(args, dict):
            continue
        path = args.get("path")
        old = args.get("old")
        new = args.get("new")
        if not all(isinstance(value, str) for value in [path, old, new]):
            continue
        rows.append(
            "<tr>"
            f"<td>{html.escape(path)}</td>"
            f"<td><pre>{html.escape('- ' + old)}</pre></td>"
            f"<td><pre>{html.escape('+ ' + new)}</pre></td>"
            "</tr>"
        )
    if not rows:
        return ""
    return (
        "<h2>Patch Diff</h2><table><thead><tr><th>Path</th><th>Old</th><th>New</th></tr></thead><tbody>"
        f"{''.join(rows)}</tbody></table>"
    )


def safe_return_to(query: str) -> str:
    requested = parse_qs(query).get("return_to", ["/approvals"])[0]
    if requested == "/approvals":
        return requested
    if requested.startswith("/runs/run_"):
        return requested
    return "/approvals"


def resume_run(store: AuditStore, run_id: str) -> str:
    run = store.get_run(run_id)
    if not run:
        return "not_found"
    metadata = store.get_run_metadata(run_id)
    if metadata.get("adapter") != "mcp-plan":
        return "not_found"
    plan_path = str(metadata.get("plan_path") or "")
    if not plan_path:
        return "not_found"
    adapter = McpPlanAdapter.from_file(plan_path)
    gateway = gateway_from_store(store)
    try:
        adapter.resume(gateway, run_id, approver="dashboard")
    except ValueError:
        return "conflict"
    return "resumed"


def gateway_from_store(store: AuditStore) -> RuntimeGateway:
    agentops_dir = store.db_path.parent
    policy = PolicyConfig()
    workspace_manager = WorkspaceManager(agentops_dir)
    return RuntimeGateway(
        audit_store=store,
        workspace_manager=workspace_manager,
        policy_engine=PolicyEngine(policy),
        tool_executor=ToolExecutor(workspace_manager, policy),
    )


def default_store(project_root: str | Path) -> AuditStore:
    return AuditStore(Path(project_root) / ".agentops" / "runs.sqlite")

