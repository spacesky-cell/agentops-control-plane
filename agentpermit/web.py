from __future__ import annotations

import hmac
import html
import ipaddress
import json
import secrets
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socket import socket as Socket
from socketserver import ThreadingMixIn
from threading import BoundedSemaphore, Thread
from typing import Any, cast
from urllib.parse import parse_qs, urlparse, urlsplit

from .audit import ApprovalNotFoundError, ApprovalStateConflictError, AuditStore
from .snapshot_diff import SnapshotDiff, compare_snapshot_archives


MAX_FORM_BYTES = 16 * 1024
MAX_REVIEWER_CHARS = 120
MAX_REASON_CHARS = 2_000
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def is_loopback_host(host: str) -> bool:
    if not host:
        return False
    try:
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    if not addresses:
        return False
    try:
        return all(
            ipaddress.ip_address(str(address[4][0]).split("%", 1)[0]).is_loopback
            for address in addresses
        )
    except ValueError:
        return False


def validate_loopback_host(host: str) -> None:
    if not is_loopback_host(host):
        raise ValueError(
            f"Dashboard host must resolve only to loopback addresses: {host or '<empty>'}"
        )


def _format_authority(host: str, port: int) -> str:
    normalized = host.strip().lower().strip("[]")
    if ":" in normalized:
        return f"[{normalized}]:{port}"
    return f"{normalized}:{port}"


def _normalize_authority(value: str) -> str | None:
    candidate = value.strip().lower()
    if not candidate or any(character.isspace() for character in candidate):
        return None
    try:
        parsed = urlsplit(f"//{candidate}")
        if (
            parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return None
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if host is None or port is None:
        return None
    return _format_authority(host, port)


def _resolve_loopback_bind(host: str) -> tuple[int, str, set[str]]:
    if not host:
        raise ValueError(
            "Dashboard host must resolve only to loopback addresses: <empty>"
        )
    try:
        addresses = socket.getaddrinfo(
            host,
            0,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise ValueError(
            f"Dashboard host must resolve only to loopback addresses: {host}"
        ) from exc
    resolved: list[tuple[int, str]] = []
    for address_family, _, _, _, sockaddr in addresses:
        address = str(sockaddr[0]).split("%", 1)[0]
        try:
            if not ipaddress.ip_address(address).is_loopback:
                raise ValueError(
                    f"Dashboard host must resolve only to loopback addresses: {host}"
                )
            resolved.append((int(address_family), address))
        except ValueError:
            raise ValueError(
                f"Dashboard host must resolve only to loopback addresses: {host}"
            )
    if resolved:
        family, address = resolved[0]
        return family, address, {host, address}
    raise ValueError(f"Dashboard host must resolve only to loopback addresses: {host}")


class DashboardHTTPServer(ThreadingMixIn, HTTPServer):
    """Bounded, timeout-protected standard-library dashboard server."""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16
    max_workers = 16
    request_timeout = 10.0
    allowed_authorities: set[str]

    def __init__(
        self,
        server_address: tuple[str | bytes | bytearray, int],
        handler_class: type[BaseHTTPRequestHandler],
    ):
        self._worker_slots = BoundedSemaphore(self.max_workers)
        super().__init__(server_address, handler_class)

    def process_request(self, request: Socket, client_address: object) -> None:  # type: ignore[override]
        if not self._worker_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return

        def run() -> None:
            try:
                self.process_request_thread(request, client_address)
            finally:
                self._worker_slots.release()

        thread = Thread(target=run, daemon=self.daemon_threads)
        thread.start()


class _IPv6DashboardHTTPServer(DashboardHTTPServer):
    address_family = socket.AF_INET6


class Dashboard:
    def __init__(self, store: AuditStore, *, csrf_token: str | None = None) -> None:
        self.store = store
        self.csrf_token = csrf_token or secrets.token_urlsafe(32)

    def app(self) -> type[BaseHTTPRequestHandler]:
        store = self.store
        csrf_token = self.csrf_token

        class Handler(BaseHTTPRequestHandler):
            def setup(self) -> None:
                super().setup()
                timeout = getattr(self.server, "request_timeout", 10.0)
                self.connection.settimeout(timeout)

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
                if not self._request_authority_is_valid():
                    self._send_status(403, "Invalid request authority")
                    return
                parsed = urlparse(self.path)
                parts = [part for part in parsed.path.split("/") if part]
                if parsed.path == "/":
                    self._send_html(render_index(store))
                    return
                if parsed.path == "/approvals":
                    self._send_html(render_approvals(store, csrf_token=csrf_token))
                    return
                if (
                    len(parts) == 3
                    and parts[0] == "approvals"
                    and parts[2] in {"approve", "reject"}
                ):
                    self._send_status(405, "Method not allowed", allow="POST")
                    return
                if len(parts) == 2 and parts[0] == "runs":
                    run_id = parts[1]
                    if store.get_run(run_id) is None:
                        self._send_status(404, "Run not found")
                        return
                    query = parse_qs(parsed.query)
                    event_type = query.get("event_type", [None])[0]
                    self._send_html(
                        render_run(
                            store,
                            run_id,
                            csrf_token=csrf_token,
                            event_type=event_type,
                        )
                    )
                    return
                if len(parts) == 3 and parts[:2] == ["api", "runs"]:
                    run_id = parts[2]
                    run = store.get_run(run_id)
                    if run is None:
                        self._send_status(404, "Run not found")
                        return
                    self._send_json(
                        {
                            "run": run,
                            "events": store.get_events(run_id),
                            "approvals": store.list_approvals(run_id),
                        }
                    )
                    return
                self._send_status(404, "Resource not found")

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
                parsed = urlparse(self.path)
                parts = [part for part in parsed.path.split("/") if part]
                if not (
                    len(parts) == 3
                    and parts[0] == "approvals"
                    and parts[2] in {"approve", "reject"}
                ):
                    self._send_status(404, "Resource not found")
                    return
                if not self._request_authority_is_valid():
                    self._send_status(403, "Invalid request authority")
                    return
                form = self._read_form()
                if form is None:
                    return
                supplied_token = form.get("csrf_token", [""])[0]
                if not hmac.compare_digest(supplied_token, csrf_token):
                    self._send_status(403, "Invalid CSRF token")
                    return
                try:
                    approval_id = int(parts[1])
                except ValueError:
                    self._send_status(404, "Approval not found")
                    return
                if store.get_approval(approval_id) is None:
                    self._send_status(404, "Approval not found")
                    return
                reviewer = form.get("reviewer", [""])[0].strip()
                reason = form.get("reason", [""])[0].strip()
                if not reviewer or not reason:
                    self._send_decision_error(
                        parsed.query,
                        approval_id,
                        "Reviewer and reason are required.",
                        reviewer,
                        reason,
                    )
                    return
                if len(reviewer) > MAX_REVIEWER_CHARS or len(reason) > MAX_REASON_CHARS:
                    self._send_decision_error(
                        parsed.query,
                        approval_id,
                        "Reviewer or reason exceeds the allowed length.",
                        reviewer,
                        reason,
                    )
                    return
                status = "approved" if parts[2] == "approve" else "rejected"
                try:
                    store.decide_approval(
                        approval_id,
                        status,
                        reviewer,
                        reason,
                    )
                except ApprovalNotFoundError:
                    self._send_status(404, "Approval not found")
                    return
                except ApprovalStateConflictError:
                    self._send_status(409, "Approval is no longer pending")
                    return
                self._redirect(safe_return_to(parsed.query))

            def _read_form(self) -> dict[str, list[str]] | None:
                content_type = self.headers.get("Content-Type", "")
                if content_type.split(";", 1)[0].strip().lower() != (
                    "application/x-www-form-urlencoded"
                ):
                    self._send_status(415, "Expected a form-encoded request")
                    return None
                try:
                    content_length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    self._send_status(400, "Invalid request length")
                    return None
                if content_length < 0 or content_length > MAX_FORM_BYTES:
                    self._send_status(413, "Request body is too large")
                    return None
                try:
                    body = self.rfile.read(content_length).decode("utf-8")
                except UnicodeDecodeError:
                    self._send_status(400, "Request body must be UTF-8")
                    return None
                return parse_qs(body, keep_blank_values=True, strict_parsing=False)

            def _request_authority_is_valid(self) -> bool:
                host_values = self.headers.get_all("Host", [])
                if len(host_values) != 1:
                    return False
                allowed = getattr(self.server, "allowed_authorities", None)
                if allowed is None:
                    server_address = cast(tuple[Any, ...], self.server.server_address)
                    bound_host, bound_port = server_address[:2]
                    allowed = {_format_authority(str(bound_host), int(bound_port))}
                host = _normalize_authority(host_values[0])
                if host is None or host not in allowed:
                    return False
                origin = self.headers.get("Origin")
                if origin is None:
                    return True
                parsed = urlsplit(origin)
                if (
                    parsed.scheme not in {"http", "https"}
                    or parsed.username
                    or parsed.password
                ):
                    return False
                origin_authority = _normalize_authority(parsed.netloc)
                return origin_authority is not None and origin_authority in allowed

            def _send_decision_error(
                self,
                query: str,
                approval_id: int,
                message: str,
                reviewer: str,
                reason: str,
            ) -> None:
                return_to = safe_return_to(query)
                values = {"reviewer": reviewer, "reason": reason}
                if return_to.startswith("/runs/"):
                    run_id = return_to.split("/", 2)[2]
                    if store.get_run(run_id) is not None:
                        page = render_run(
                            store,
                            run_id,
                            csrf_token=csrf_token,
                            approval_error=(approval_id, message),
                            form_values=values,
                        )
                        self._send_html(page, status=422)
                        return
                self._send_html(
                    render_approvals(
                        store,
                        csrf_token=csrf_token,
                        approval_error=(approval_id, message),
                        form_values=values,
                    ),
                    status=422,
                )

            def log_message(self, format: str, *args: object) -> None:
                return

            def _send_html(self, body: str, *, status: int = 200) -> None:
                encoded = body.encode("utf-8")
                self.send_response(status)
                self._send_security_headers()
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def _send_json(self, payload: object) -> None:
                encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode(
                    "utf-8"
                )
                self.send_response(200)
                self._send_security_headers()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def _send_status(
                self,
                status: int,
                message: str,
                *,
                allow: str | None = None,
            ) -> None:
                body = render_shell(
                    message,
                    "<section class='page-header'>"
                    f"<h1>{html.escape(message)}</h1>"
                    "<p><a href='/'>Return to runs</a></p></section>",
                )
                encoded = body.encode("utf-8")
                self.send_response(status)
                self._send_security_headers()
                if allow:
                    self.send_header("Allow", allow)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def _redirect(self, location: str) -> None:
                self.send_response(303)
                self._send_security_headers()
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _send_security_headers(self) -> None:
                for name, value in SECURITY_HEADERS.items():
                    self.send_header(name, value)

        return Handler


def serve(store: AuditStore, host: str, port: int) -> DashboardHTTPServer:
    family, bind_host, aliases = _resolve_loopback_bind(host)
    server_class = (
        _IPv6DashboardHTTPServer if family == socket.AF_INET6 else DashboardHTTPServer
    )
    server = server_class((bind_host, port), Dashboard(store).app())
    server.allowed_authorities = {
        _format_authority(alias, server.server_port) for alias in aliases
    }
    server.serve_forever()
    return server


def render_shell(title: str, content: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --bg: #f4f6f7;
      --surface: #ffffff;
      --surface-alt: #eef2f3;
      --ink: #172126;
      --muted: #5d6b72;
      --line: #d5dee1;
      --accent: #206a5b;
      --accent-strong: #174f44;
      --warning-bg: #fff0cc;
      --warning-ink: #754b00;
      --success-bg: #dcefe4;
      --success-ink: #155c38;
      --danger-bg: #f7dfdc;
      --danger-ink: #8b3028;
      --neutral-bg: #e7ece9;
      --neutral-ink: #3f4c45;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-width: 320px;
      background: var(--bg);
      color: var(--ink);
      font-family: ui-sans-serif, "Segoe UI", Arial, sans-serif;
      font-size: 14px;
      font-variant-numeric: tabular-nums;
      line-height: 1.45;
    }}
    a {{ color: var(--accent-strong); text-underline-offset: 3px; }}
    button {{
      border: 0;
      border-radius: 6px;
      background: var(--accent);
      color: #fff;
      cursor: pointer;
      font: inherit;
      font-weight: 650;
      min-height: 36px;
      padding: 7px 12px;
    }}
    button:hover {{ background: var(--accent-strong); }}
    .button-danger {{ background: #993b32; }}
    button:focus-visible, a:focus-visible, select:focus-visible,
    input:focus-visible, textarea:focus-visible, summary:focus-visible {{
      outline: 3px solid #8fc5ba;
      outline-offset: 2px;
    }}
    .topbar {{ background: var(--surface); border-bottom: 1px solid var(--line); }}
    .topbar-inner {{
      align-items: center;
      display: flex;
      gap: 20px;
      justify-content: space-between;
      margin: 0 auto;
      min-height: 54px;
      width: min(1180px, calc(100% - 32px));
    }}
    .brand {{ color: var(--ink); font-size: 16px; font-weight: 750; text-decoration: none; }}
    .nav-links {{ display: flex; flex-wrap: wrap; gap: 14px; }}
    main {{ margin: 0 auto; padding: 22px 0 44px; width: min(1180px, calc(100% - 32px)); }}
    .page-header {{ margin: 0 0 18px; }}
    .page-header h1 {{ font-size: 28px; line-height: 1.15; margin: 5px 0 6px; overflow-wrap: anywhere; }}
    .page-header p {{ color: var(--muted); margin: 0; max-width: 78ch; }}
    .metric-grid, .run-summary {{
      display: grid;
      gap: 10px;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      margin: 14px 0 18px;
    }}
    .metric-card, .summary-item {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 6px;
      min-width: 0;
      padding: 12px;
    }}
    .metric-card span, .summary-item span {{ color: var(--muted); display: block; font-size: 12px; }}
    .metric-card strong {{ display: block; font-size: 22px; line-height: 1.1; margin-top: 5px; }}
    .summary-item strong {{ display: block; margin-top: 4px; overflow-wrap: anywhere; }}
    .panel {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 6px;
      margin: 14px 0;
      overflow-x: auto;
      padding: 14px;
    }}
    .panel h2 {{ font-size: 17px; margin: 0 0 10px; }}
    table {{ border-collapse: collapse; min-width: 720px; width: 100%; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 8px; text-align: left; vertical-align: top; }}
    th {{ background: var(--surface-alt); color: var(--muted); font-size: 12px; }}
    tr:last-child td {{ border-bottom: 0; }}
    pre {{
      background: #f7f9f9;
      border: 1px solid var(--line);
      border-radius: 5px;
      font-size: 12px;
      margin: 7px 0 0;
      max-width: 100%;
      overflow: auto;
      padding: 9px;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    details > summary {{ color: var(--accent-strong); cursor: pointer; font-weight: 650; margin-top: 6px; }}
    .status-badge {{
      align-items: center;
      background: var(--neutral-bg);
      border-radius: 5px;
      color: var(--neutral-ink);
      display: inline-flex;
      font-size: 12px;
      font-weight: 700;
      min-height: 23px;
      padding: 2px 7px;
      white-space: nowrap;
    }}
    .status-success, .status-approved, .status-consumed {{ background: var(--success-bg); color: var(--success-ink); }}
    .status-waiting-for-approval, .status-pending, .status-running {{ background: var(--warning-bg); color: var(--warning-ink); }}
    .status-failed, .status-rejected, .status-denied {{ background: var(--danger-bg); color: var(--danger-ink); }}
    .approval-form {{ display: grid; gap: 8px; margin-top: 10px; max-width: 520px; }}
    .approval-form label {{ color: var(--muted); display: grid; font-size: 12px; gap: 3px; }}
    input, textarea, select {{
      background: #fff;
      border: 1px solid #aebbc0;
      border-radius: 5px;
      color: var(--ink);
      font: inherit;
      max-width: 100%;
      padding: 7px 8px;
      width: 100%;
    }}
    textarea {{ min-height: 72px; resize: vertical; }}
    .action-row {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    .form-error, .evidence-unavailable {{
      background: var(--danger-bg);
      border-left: 3px solid var(--danger-ink);
      color: var(--danger-ink);
      margin: 8px 0;
      padding: 8px 10px;
    }}
    .filter-form {{ align-items: end; display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }}
    .filter-form label {{ color: var(--muted); display: grid; gap: 3px; min-width: 190px; }}
    .empty-state {{ color: var(--muted); padding: 12px; }}
    .empty-state strong {{ color: var(--ink); display: block; }}
    .file-meta {{ color: var(--muted); font-size: 12px; }}
    .skip-link {{ left: -999px; position: absolute; top: 6px; }}
    .skip-link:focus {{ background: #fff; left: 6px; padding: 7px; z-index: 2; }}
    @media (max-width: 600px) {{
      .topbar-inner {{ align-items: flex-start; flex-direction: column; gap: 5px; padding: 10px 0; }}
      main {{ padding-top: 16px; width: calc(100% - 20px); }}
      .page-header h1 {{ font-size: 23px; }}
      .metric-grid, .run-summary {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .panel {{ padding: 10px; }}
      table {{ min-width: 640px; }}
    }}
  </style>
</head>
<body>
  <a class="skip-link" href="#main">Skip to content</a>
  <header class="topbar"><div class="topbar-inner">
    <a class="brand" href="/">AgentPermit</a>
    <nav class="nav-links" aria-label="Dashboard navigation">
      <a href="/">Runs</a><a href="/approvals">Approvals</a>
    </nav>
  </div></header>
  <main id="main">{content}</main>
</body>
</html>"""


def render_index(store: AuditStore) -> str:
    runs = store.list_runs()
    approvals = store.list_approvals()
    rows = [
        "<tr>"
        f"<td><a href='/runs/{html.escape(str(run['id']), quote=True)}'>"
        f"{html.escape(str(run['id']))}</a></td>"
        f"<td>{status_badge(run['status'])}</td>"
        f"<td>{html.escape(str(run['agent_name']))}</td>"
        f"<td>{html.escape(str(run['started_at']))}</td>"
        f"<td>{html.escape(str(run['task']))}</td></tr>"
        for run in runs
    ]
    waiting = sum(run["status"] == "waiting_for_approval" for run in runs)
    pending = sum(approval["status"] == "pending" for approval in approvals)
    content = (
        "<section class='page-header'><h1>AgentPermit</h1>"
        "<p>Local policy decisions, approvals, and auditable run evidence.</p></section>"
        "<section class='metric-grid' aria-label='Operational summary'>"
        f"{metric_card('Total runs', len(runs))}"
        f"{metric_card('Waiting approvals', waiting)}"
        f"{metric_card('Pending requests', pending)}</section>"
        "<section class='panel'><h2>Recent runs</h2><table>"
        "<thead><tr><th>Run</th><th>Status</th><th>Agent</th>"
        "<th>Started</th><th>Task</th></tr></thead><tbody>"
        f"{''.join(rows) or empty_row(5, 'No runs yet', 'Start a governed run with the AgentPermit CLI or MCP server.')}"
        "</tbody></table></section>"
    )
    return render_shell("AgentPermit", content)


def render_approvals(
    store: AuditStore,
    *,
    csrf_token: str = "",
    approval_error: tuple[int, str] | None = None,
    form_values: dict[str, str] | None = None,
) -> str:
    rows = render_approval_rows(
        store.list_approvals(),
        return_to="/approvals",
        csrf_token=csrf_token,
        approval_error=approval_error,
        form_values=form_values,
        show_run=True,
    )
    content = (
        "<section class='page-header'><p><a href='/'>Back to runs</a></p>"
        "<h1>Approvals</h1><p>Review the policy reason and exact redacted request.</p></section>"
        "<section class='panel'><table><thead><tr><th>ID</th><th>Run</th>"
        "<th>Status</th><th>Tool</th><th>Requested</th><th>Review</th>"
        "</tr></thead><tbody>"
        f"{rows or empty_row(6, 'No approvals yet', 'Approval requests appear here when policy requires review.')}"
        "</tbody></table></section>"
    )
    return render_shell("Approvals", content)


def render_approval_rows(
    approvals: list[dict[str, object]],
    *,
    return_to: str,
    csrf_token: str,
    approval_error: tuple[int, str] | None = None,
    form_values: dict[str, str] | None = None,
    show_run: bool,
) -> str:
    rows: list[str] = []
    for approval in approvals:
        raw_approval_id = approval.get("id")
        if not isinstance(raw_approval_id, int):
            continue
        approval_id = raw_approval_id
        run_id = str(approval["run_id"])
        payload = approval.get("payload")
        args = payload.get("args") if isinstance(payload, dict) else payload
        details = html.escape(json.dumps(args, ensure_ascii=False, indent=2))
        error = None
        if approval_error and approval_error[0] == approval_id:
            error = approval_error[1]
        row_form_values = form_values if error is not None else None
        actions = render_approval_actions(
            approval_id,
            str(approval["status"]),
            return_to,
            csrf_token,
            error=error,
            form_values=row_form_values,
        )
        reviewer = approval.get("approver")
        reviewer_reason = approval.get("reviewer_reason")
        decided = ""
        if reviewer or reviewer_reason:
            decided = (
                "<dl><dt>Reviewer</dt>"
                f"<dd>{html.escape(str(reviewer or ''))}</dd>"
                "<dt>Reason</dt>"
                f"<dd>{html.escape(str(reviewer_reason or ''))}</dd></dl>"
            )
        run_cell = ""
        if show_run:
            escaped_run = html.escape(run_id)
            run_cell = f"<td><a href='/runs/{escaped_run}'>{escaped_run}</a></td>"
        rows.append(
            "<tr>"
            f"<td>{approval_id}</td>{run_cell}"
            f"<td>{status_badge(approval['status'])}</td>"
            f"<td>{html.escape(str(approval['tool_name']))}</td>"
            f"<td>{html.escape(str(approval['requested_at']))}</td>"
            "<td>"
            f"<strong>{html.escape(str(approval.get('policy_reason') or 'No policy reason recorded.'))}</strong>"
            "<details><summary>Request arguments</summary>"
            f"<pre>{details}</pre></details>{decided}{actions}</td></tr>"
        )
    return "".join(rows)


def render_approval_actions(
    approval_id: int,
    status: str,
    return_to: str,
    csrf_token: str,
    *,
    error: str | None = None,
    form_values: dict[str, str] | None = None,
) -> str:
    if status != "pending":
        return ""
    values = form_values or {}
    escaped_return = html.escape(return_to, quote=True)
    approve_action = f"/approvals/{approval_id}/approve?return_to={escaped_return}"
    reject_action = f"/approvals/{approval_id}/reject?return_to={escaped_return}"
    error_html = (
        f"<div class='form-error' role='alert'>{html.escape(error)}</div>"
        if error
        else ""
    )
    return (
        f"{error_html}<form class='approval-form' method='post' action='{approve_action}'>"
        f"<input type='hidden' name='csrf_token' value='{html.escape(csrf_token, quote=True)}'>"
        "<label>Reviewer"
        f"<input name='reviewer' maxlength='{MAX_REVIEWER_CHARS}' required value='{html.escape(values.get('reviewer', ''), quote=True)}'>"
        "</label><label>Reason"
        f"<textarea name='reason' maxlength='{MAX_REASON_CHARS}' required>{html.escape(values.get('reason', ''))}</textarea>"
        "</label><div class='action-row'>"
        "<button type='submit'>Approve</button>"
        f"<button class='button-danger' type='submit' formaction='{reject_action}'>Reject</button>"
        "</div></form>"
    )


def render_run(
    store: AuditStore,
    run_id: str,
    *,
    csrf_token: str = "",
    event_type: str | None = None,
    approval_error: tuple[int, str] | None = None,
    form_values: dict[str, str] | None = None,
) -> str:
    run = store.get_run(run_id)
    if run is None:
        return render_shell("Run not found", "<h1>Run not found</h1>")
    events = store.get_events(run_id)
    approvals = store.list_approvals(run_id)
    event_types = sorted({str(event["type"]) for event in events})
    unknown_filter = bool(event_type and event_type not in event_types)
    selected_type = None if unknown_filter else event_type
    visible_events = (
        [event for event in events if event["type"] == selected_type]
        if selected_type
        else events
    )
    event_rows = [_render_event_row(event) for event in visible_events]
    evidence = render_snapshot_evidence(events)
    approval_rows = render_approval_rows(
        approvals,
        return_to=f"/runs/{run_id}",
        csrf_token=csrf_token,
        approval_error=approval_error,
        form_values=form_values,
        show_run=False,
    )
    filter_options = ["<option value=''>All event types</option>"]
    for known_type in event_types:
        selected = " selected" if known_type == selected_type else ""
        filter_options.append(
            f"<option value='{html.escape(known_type, quote=True)}'{selected}>"
            f"{html.escape(known_type)}</option>"
        )
    filter_error = (
        "<div class='form-error' role='alert'>Unknown event filter; showing all events.</div>"
        if unknown_filter
        else ""
    )
    ended = str(run.get("ended_at") or "In progress")
    content = (
        "<section class='page-header'><p><a href='/'>Back to runs</a></p>"
        f"<h1>{html.escape(run_id)}</h1><p>Run evidence and policy trace.</p></section>"
        "<section class='run-summary' aria-label='Run summary'>"
        f"{summary_item('Status', status_badge(run['status']), escaped=True)}"
        f"{summary_item('Agent', str(run['agent_name']))}"
        f"{summary_item('Started', str(run['started_at']))}"
        f"{summary_item('Ended', ended)}"
        f"{summary_item('Events', str(len(events)))}"
        f"{summary_item('Approvals', str(len(approvals)))}</section>"
        "<section class='panel'><h2>Task</h2>"
        f"<p>{html.escape(str(run['task']))}</p>"
        f"<p class='file-meta'>Workspace: {html.escape(str(run['workspace_path']))}</p></section>"
        f"{evidence}"
        "<section class='panel'><h2>Approvals</h2><table><thead><tr>"
        "<th>ID</th><th>Status</th><th>Tool</th><th>Requested</th><th>Review</th>"
        "</tr></thead><tbody>"
        f"{approval_rows or empty_row(5, 'No approvals for this run', 'No human approval gate was triggered.')}"
        "</tbody></table></section>"
        "<section class='panel'><h2>Events</h2>"
        "<form class='filter-form' method='get'>"
        "<label>Event type<select name='event_type'>"
        f"{''.join(filter_options)}</select></label><button type='submit'>Filter</button>"
        "</form>"
        f"{filter_error}<table><thead><tr><th>ID</th><th>Type</th><th>Tool</th>"
        "<th>Risk</th><th>Message</th></tr></thead><tbody>"
        f"{''.join(event_rows) or empty_row(5, 'No matching events', 'Choose another event type.')}"
        "</tbody></table></section>"
    )
    return render_shell(f"Run {run_id}", content)


def _render_event_row(event: dict[str, Any]) -> str:
    payload = html.escape(json.dumps(event["payload"], ensure_ascii=False, indent=2))
    return (
        "<tr>"
        f"<td>{event['id']}</td><td>{html.escape(str(event['type']))}</td>"
        f"<td>{html.escape(str(event.get('tool_name') or ''))}</td>"
        f"<td>{status_badge(event.get('risk') or 'n/a')}</td>"
        f"<td>{html.escape(str(event['message']))}"
        "<details class='event-payload'><summary>Payload</summary>"
        f"<pre>{payload}</pre></details></td></tr>"
    )


def render_snapshot_evidence(events: list[dict[str, Any]]) -> str:
    before_path = _snapshot_path(events, "run_started")
    after_path = _snapshot_path(events, "run_finished")
    if before_path is None and after_path is None:
        return ""
    if before_path is None or after_path is None:
        return _render_unavailable_evidence("snapshot_reference_missing")
    result = compare_snapshot_archives(before_path, after_path)
    if not result.available:
        return _render_unavailable_evidence(result.reason or "snapshot_unavailable")
    return _render_snapshot_diff(result)


def _snapshot_path(events: list[dict[str, Any]], event_type: str) -> str | None:
    candidates: list[str] = []
    for event in events:
        if event.get("type") != event_type:
            continue
        payload = event.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("snapshot"), str):
            candidates.append(payload["snapshot"])
    return candidates[-1] if candidates else None


def _render_unavailable_evidence(reason: str) -> str:
    return (
        "<section class='panel'><h2>Snapshot changes</h2>"
        "<div class='evidence-unavailable'><strong>Snapshot evidence unavailable</strong>"
        f"<div>{html.escape(reason)}</div></div></section>"
    )


def _render_snapshot_diff(result: SnapshotDiff) -> str:
    rows: list[str] = []
    for entry in result.entries:
        size = f"{entry.before_size if entry.before_size is not None else '-'} -> {entry.after_size if entry.after_size is not None else '-'} bytes"
        if entry.diff is None:
            evidence = (
                f"<span class='file-meta'>{html.escape(entry.display)}; {size}</span>"
            )
        else:
            evidence = f"<pre>{html.escape(entry.diff)}</pre>"
        rows.append(
            f"<tr data-diff-path='{html.escape(entry.path, quote=True)}'>"
            f"<td>{html.escape(entry.path)}</td><td>{status_badge(entry.status)}</td>"
            f"<td>{evidence}</td></tr>"
        )
    content = (
        "<section class='panel'><h2>Snapshot changes</h2>"
        "<section class='metric-grid' aria-label='Snapshot summary'>"
        f"{metric_card('Created', result.counts['created'])}"
        f"{metric_card('Modified', result.counts['modified'])}"
        f"{metric_card('Deleted', result.counts['deleted'])}"
        f"{metric_card('Unchanged', result.counts['unchanged'])}</section>"
        "<table><thead><tr><th>Path</th><th>Change</th><th>Evidence</th>"
        "</tr></thead><tbody>"
        f"{''.join(rows) or empty_row(3, 'No changed files', 'The snapshots contain identical files.')}"
        "</tbody></table></section>"
    )
    return content


def metric_card(label: str, value: int) -> str:
    return (
        "<article class='metric-card'>"
        f"<span>{html.escape(label)}</span><strong>{value}</strong></article>"
    )


def summary_item(label: str, value: str, *, escaped: bool = False) -> str:
    rendered = value if escaped else html.escape(value)
    return (
        "<div class='summary-item'>"
        f"<span>{html.escape(label)}</span><strong>{rendered}</strong></div>"
    )


def empty_row(colspan: int, title: str, detail: str) -> str:
    return (
        f"<tr><td colspan='{colspan}'><div class='empty-state'>"
        f"<strong>{html.escape(title)}</strong>{html.escape(detail)}</div></td></tr>"
    )


def status_badge(status: object) -> str:
    label = str(status or "n/a")
    css = label.lower().replace("_", "-").replace(" ", "-")
    return (
        f"<span class='status-badge status-{html.escape(css, quote=True)}'>"
        f"{html.escape(label)}</span>"
    )


def safe_return_to(query: str) -> str:
    requested = parse_qs(query).get("return_to", ["/approvals"])[0]
    if requested == "/approvals":
        return requested
    if requested.startswith("/runs/run_") and "/" not in requested[6:]:
        return requested
    return "/approvals"


def default_store(project_root: str | Path) -> AuditStore:
    return AuditStore(Path(project_root) / ".agentpermit" / "runs.sqlite")
