from __future__ import annotations

import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .audit import AuditStore


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
        "<table><thead><tr><th>Run</th><th>Status</th><th>Agent</th>"
        "<th>Started</th><th>Task</th></tr></thead><tbody>"
        f"{''.join(rows) or '<tr><td colspan=5>No runs yet</td></tr>'}"
        "</tbody></table>"
    )
    return render_shell("AgentOps Control Plane", content)


def render_run(store: AuditStore, run_id: str) -> str:
    run = store.get_run(run_id)
    if not run:
        return render_shell("Run not found", "<h1>Run not found</h1>")
    event_rows = []
    for event in store.get_events(run_id):
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
    content = (
        f"<p><a href='/'>Back</a></p><h1>{html.escape(run_id)}</h1>"
        f"<p><strong>Status:</strong> {html.escape(run['status'])}</p>"
        f"<p><strong>Task:</strong> {html.escape(run['task'])}</p>"
        f"<p><strong>Workspace:</strong> {html.escape(run['workspace_path'])}</p>"
        "<h2>Events</h2><table><thead><tr><th>ID</th><th>Type</th><th>Tool</th>"
        "<th>Risk</th><th>Message</th></tr></thead><tbody>"
        f"{''.join(event_rows)}</tbody></table>"
    )
    return render_shell(f"Run {run_id}", content)


def default_store(project_root: str | Path) -> AuditStore:
    return AuditStore(Path(project_root) / ".agentops" / "runs.sqlite")

