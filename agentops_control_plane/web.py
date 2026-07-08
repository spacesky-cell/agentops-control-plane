from __future__ import annotations

import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .audit import ApprovalNotFoundError, ApprovalStateConflictError, AuditStore
from .config import PolicyConfig
from .gateway import RuntimeGateway
from .mcp_adapter import McpPlanAdapter
from .policy import PolicyEngine
from .tools import ToolExecutor
from .workspace import WorkspaceManager


class Dashboard:
    def __init__(self, store: AuditStore, policy_config: PolicyConfig | None = None) -> None:
        self.store = store
        self.policy_config = policy_config or PolicyConfig()

    def app(self) -> type[BaseHTTPRequestHandler]:
        store = self.store
        policy_config = self.policy_config

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
                    except ApprovalStateConflictError:
                        self.send_response(409)
                        self.end_headers()
                        return
                    self._redirect(safe_return_to(parsed.query))
                    return
                if len(parts) == 3 and parts[0] == "runs" and parts[2] == "resume":
                    resume_status = resume_run(store, parts[1], policy_config)
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


def serve(
    store: AuditStore,
    host: str,
    port: int,
    policy_config: PolicyConfig | None = None,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Dashboard(store, policy_config).app())
    server.serve_forever()
    return server


def render_shell(title: str, content: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%232f6f62'/%3E%3Cpath d='M9 17l5 5 9-12' fill='none' stroke='white' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
  <style>
    :root {{
      --bg: #f5f7f8;
      --surface: #ffffff;
      --surface-alt: #edf2f4;
      --ink: #1b2326;
      --muted: #607078;
      --line: #d7e0e3;
      --accent: #2f6f62;
      --accent-strong: #1e574c;
      --warning-bg: #fff1d6;
      --warning-ink: #835200;
      --success-bg: #dff3e8;
      --success-ink: #1c6842;
      --danger-bg: #f8ded9;
      --danger-ink: #96372c;
      --neutral-bg: #e8ece9;
      --neutral-ink: #46514a;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      min-width: 320px;
      background: var(--bg);
      color: var(--ink);
      font-family: ui-sans-serif, "Segoe UI", Arial, sans-serif;
      font-variant-numeric: tabular-nums;
    }}
    a {{ color: var(--accent-strong); text-decoration-thickness: 1px; text-underline-offset: 3px; }}
    a:hover {{ color: var(--accent); }}
    button {{
      border: 0;
      background: var(--accent);
      color: #fff;
      padding: 7px 11px;
      border-radius: 6px;
      font: inherit;
      font-weight: 600;
      cursor: pointer;
      transition: background 160ms ease, transform 160ms ease;
    }}
    button:hover {{ background: var(--accent-strong); }}
    button:active {{ transform: translateY(1px); }}
    button:focus-visible, a:focus-visible {{ outline: 3px solid #9ccdc2; outline-offset: 2px; }}
    main {{ width: min(1180px, calc(100% - 32px)); margin: 0 auto; padding: 28px 0 48px; }}
    .topbar {{
      border-bottom: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.92);
    }}
    .topbar-inner {{
      width: min(1180px, calc(100% - 32px));
      margin: 0 auto;
      min-height: 58px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }}
    .brand {{ font-weight: 700; color: var(--ink); text-decoration: none; }}
    .nav-links {{ display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }}
    .page-header {{ margin: 8px 0 24px; }}
    .page-header h1 {{ margin: 0 0 8px; font-size: clamp(28px, 4vw, 44px); line-height: 1.05; }}
    .page-header p {{ margin: 0; color: var(--muted); max-width: 70ch; }}
    .metric-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin: 18px 0 24px;
    }}
    .metric-card {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }}
    .metric-card span {{ display: block; color: var(--muted); font-size: 13px; }}
    .metric-card strong {{ display: block; margin-top: 8px; font-size: 28px; line-height: 1; }}
    .panel {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      margin: 18px 0;
      overflow-x: auto;
    }}
    .panel h2 {{ margin: 0 0 14px; font-size: 19px; }}
    table {{ border-collapse: collapse; width: 100%; min-width: 760px; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 10px 9px; vertical-align: top; text-align: left; }}
    th {{ background: var(--surface-alt); font-size: 13px; color: var(--muted); font-weight: 700; }}
    tr:last-child td {{ border-bottom: 0; }}
    pre {{
      white-space: pre-wrap;
      background: #f7f9fa;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      max-width: 72ch;
      overflow-x: auto;
    }}
    .status-badge {{
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 3px 8px;
      border-radius: 6px;
      background: var(--neutral-bg);
      color: var(--neutral-ink);
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .status-success, .status-approved, .status-consumed {{ background: var(--success-bg); color: var(--success-ink); }}
    .status-waiting-for-approval, .status-pending, .status-running {{ background: var(--warning-bg); color: var(--warning-ink); }}
    .status-failed, .status-rejected, .status-denied {{ background: var(--danger-bg); color: var(--danger-ink); }}
    .run-summary {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 12px;
      margin: 18px 0;
    }}
    .summary-item {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      min-width: 0;
    }}
    .summary-item span {{ display: block; color: var(--muted); font-size: 13px; margin-bottom: 6px; }}
    .summary-item strong {{ display: block; overflow-wrap: anywhere; }}
    .empty-state {{ color: var(--muted); padding: 18px; }}
    .empty-state strong {{ color: var(--ink); display: block; margin-bottom: 4px; }}
    .inline-form {{ display: inline; }}
    .action-row {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }}
    .skip-link {{ position: absolute; left: -999px; top: 8px; }}
    .skip-link:focus {{ left: 8px; background: var(--surface); padding: 8px; z-index: 2; }}
    @media (max-width: 720px) {{
      .topbar-inner {{ align-items: flex-start; flex-direction: column; padding: 12px 0; }}
      main {{ width: min(100% - 20px, 1180px); padding-top: 20px; }}
      table {{ min-width: 640px; }}
    }}
  </style>
</head>
<body>
  <a class="skip-link" href="#main">Skip to content</a>
  <header class="topbar">
    <div class="topbar-inner">
      <a class="brand" href="/">AgentOps Control Plane</a>
      <nav class="nav-links" aria-label="Dashboard navigation">
        <a href="/">Runs</a>
        <a href="/approvals">Approvals</a>
      </nav>
    </div>
  </header>
  <main id="main">{content}</main>
</body>
</html>"""


def render_index(store: AuditStore) -> str:
    rows = []
    runs = store.list_runs()
    approvals = store.list_approvals()
    for run in runs:
        rows.append(
            "<tr>"
            f"<td><a href='/runs/{html.escape(run['id'])}'>{html.escape(run['id'])}</a></td>"
            f"<td>{status_badge(run['status'])}</td>"
            f"<td>{html.escape(run['agent_name'])}</td>"
            f"<td>{html.escape(run['started_at'])}</td>"
            f"<td>{html.escape(run['task'])}</td>"
            "</tr>"
        )
    waiting_count = sum(1 for run in runs if run["status"] == "waiting_for_approval")
    pending_count = sum(1 for approval in approvals if approval["status"] == "pending")
    content = (
        "<section class='page-header'>"
        "<h1>AgentOps Control Plane</h1>"
        "<p>Governed local agent runs with policy decisions, approvals, isolated workspaces, and audit traces.</p>"
        "</section>"
        "<section class='metric-grid' aria-label='Operational summary'>"
        f"{metric_card('Total runs', len(runs))}"
        f"{metric_card('Waiting approvals', waiting_count)}"
        f"{metric_card('Pending requests', pending_count)}"
        "</section>"
        "<section class='panel'><h2>Recent runs</h2>"
        "<table><thead><tr><th>Run</th><th>Status</th><th>Agent</th>"
        "<th>Started</th><th>Task</th></tr></thead><tbody>"
        f"{''.join(rows) or empty_row(5, 'No runs yet', 'Start with run-script or run-mcp-plan to create an auditable run.')}"
        "</tbody></table></section>"
    )
    return render_shell("AgentOps Control Plane", content)


def metric_card(label: str, value: int) -> str:
    return f"<article class='metric-card'><span>{html.escape(label)}</span><strong>{value}</strong></article>"


def empty_row(colspan: int, title: str, detail: str) -> str:
    return (
        f"<tr><td colspan='{colspan}'><div class='empty-state'>"
        f"<strong>{html.escape(title)}</strong>{html.escape(detail)}"
        "</div></td></tr>"
    )


def render_approvals(store: AuditStore) -> str:
    rows = render_approval_rows(store.list_approvals(), return_to="/approvals")
    content = (
        "<section class='page-header'><p><a href='/'>Back</a></p><h1>Approvals</h1>"
        "<p>Review pending tool requests and inspect the exact redacted action payload.</p></section>"
        "<section class='panel'>"
        "<table><thead><tr><th>ID</th><th>Run</th><th>Status</th><th>Tool</th>"
        "<th>Requested</th><th>Request</th></tr></thead><tbody>"
        f"{rows or empty_row(6, 'No approvals yet', 'Approval requests appear here when policy requires a human decision.')}"
        "</tbody></table></section>"
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
            f"<td>{status_badge(str(approval['status']))}</td>"
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
        "<div class='action-row'>"
        f"<form class='inline-form' method='post' action='/approvals/{approval_id}/approve?return_to={escaped_return_to}'>"
        "<button type='submit'>Approve</button>"
        "</form> "
        f"<form class='inline-form' method='post' action='/approvals/{approval_id}/reject?return_to={escaped_return_to}'>"
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
            f"<td>{status_badge(str(event.get('risk') or 'n/a'))}</td>"
            f"<td>{html.escape(event['message'])}<pre>{payload}</pre></td>"
            "</tr>"
        )
    diff_rows = render_patch_diffs(events)
    approval_rows = render_approval_rows(store.list_approvals(run_id), return_to=f"/runs/{run_id}")
    approval_table = (
        "<section class='panel'><h2>Approvals</h2><table><thead><tr><th>ID</th><th>Run</th><th>Status</th><th>Tool</th>"
        "<th>Requested</th><th>Request</th></tr></thead><tbody>"
        f"{approval_rows or empty_row(6, 'No approvals for this run', 'This run has not triggered a human approval gate.')}"
        "</tbody></table></section>"
    )
    resume_action = render_resume_action(store, run)
    content = (
        f"<section class='page-header'><p><a href='/'>Back</a></p><h1>{html.escape(run_id)}</h1>"
        "<p>Run trace, approvals, policy decisions, and captured tool payloads.</p></section>"
        "<section class='run-summary'>"
        f"<div class='summary-item'><span>Status</span><strong>{status_badge(run['status'])}</strong></div>"
        f"<div class='summary-item'><span>Task</span><strong>{html.escape(run['task'])}</strong></div>"
        f"<div class='summary-item'><span>Workspace</span><strong>{html.escape(run['workspace_path'])}</strong></div>"
        "</section>"
        f"{resume_action}"
        f"{diff_rows}"
        f"{approval_table}"
        "<section class='panel'><h2>Events</h2><table><thead><tr><th>ID</th><th>Type</th><th>Tool</th>"
        "<th>Risk</th><th>Message</th></tr></thead><tbody>"
        f"{''.join(event_rows) or empty_row(5, 'No events yet', 'Events appear as the agent crosses policy and tool boundaries.')}"
        "</tbody></table></section>"
    )
    return render_shell(f"Run {run_id}", content)


def render_resume_action(store: AuditStore, run: dict[str, object]) -> str:
    run_id_raw = str(run["id"])
    metadata = store.get_run_metadata(run_id_raw)
    if run["status"] != "waiting_for_approval" or metadata.get("adapter") != "mcp-plan":
        return ""
    has_approved_approval = any(
        approval["status"] == "approved"
        for approval in store.list_approvals(run_id_raw)
    )
    if not has_approved_approval:
        return ""
    run_id = html.escape(run_id_raw)
    return (
        "<div class='panel'><form method='post' "
        f"action='/runs/{run_id}/resume'>"
        "<button type='submit'>Resume</button>"
        "</form></div>"
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
        "<section class='panel'><h2>Patch Diff</h2><table><thead><tr><th>Path</th><th>Old</th><th>New</th></tr></thead><tbody>"
        f"{''.join(rows)}</tbody></table></section>"
    )


def status_badge(status: object) -> str:
    label = str(status or "n/a")
    css = label.lower().replace("_", "-").replace(" ", "-")
    return f"<span class='status-badge status-{html.escape(css)}'>{html.escape(label)}</span>"


def safe_return_to(query: str) -> str:
    requested = parse_qs(query).get("return_to", ["/approvals"])[0]
    if requested == "/approvals":
        return requested
    if requested.startswith("/runs/run_"):
        return requested
    return "/approvals"


def resume_run(store: AuditStore, run_id: str, policy_config: PolicyConfig | None = None) -> str:
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
    gateway = gateway_from_store(store, policy_config)
    try:
        adapter.resume(gateway, run_id, approver="dashboard")
    except ValueError:
        return "conflict"
    return "resumed"


def gateway_from_store(store: AuditStore, policy_config: PolicyConfig | None = None) -> RuntimeGateway:
    agentops_dir = store.db_path.parent
    policy = policy_config or PolicyConfig()
    workspace_manager = WorkspaceManager(agentops_dir)
    return RuntimeGateway(
        audit_store=store,
        workspace_manager=workspace_manager,
        policy_engine=PolicyEngine(policy),
        tool_executor=ToolExecutor(workspace_manager, policy),
    )


def default_store(project_root: str | Path) -> AuditStore:
    return AuditStore(Path(project_root) / ".agentops" / "runs.sqlite")

