from __future__ import annotations

import html
import json
from pathlib import Path

from .audit import AuditStore


def export_json(store: AuditStore, run_id: str, out: str | Path) -> Path:
    run = store.get_run(run_id)
    if not run:
        raise ValueError(f"Run not found: {run_id}")
    payload = {
        "run": run,
        "events": store.get_events(run_id),
        "approvals": store.list_approvals(run_id),
    }
    output = Path(out)
    output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return output


def export_html(store: AuditStore, run_id: str, out: str | Path) -> Path:
    run = store.get_run(run_id)
    if not run:
        raise ValueError(f"Run not found: {run_id}")
    events = store.get_events(run_id)
    approvals = store.list_approvals(run_id)
    rows = []
    for event in events:
        payload = html.escape(
            json.dumps(event["payload"], ensure_ascii=False, indent=2)
        )
        rows.append(
            "<tr>"
            f"<td>{event['id']}</td>"
            f"<td>{html.escape(event['ts'])}</td>"
            f"<td>{html.escape(event['type'])}</td>"
            f"<td>{html.escape(str(event.get('tool_name') or ''))}</td>"
            f"<td>{html.escape(str(event.get('risk') or ''))}</td>"
            f"<td>{html.escape(event['message'])}<pre>{payload}</pre></td>"
            "</tr>"
        )
    approval_items = "".join(
        f"<li>#{item['id']} {html.escape(item['tool_name'])}: "
        f"{html.escape(item['status'])} "
        f"(Policy reason: {html.escape(item.get('policy_reason') or '')}; "
        f"Reviewer reason: {html.escape(item.get('reviewer_reason') or '')})</li>"
        for item in approvals
    )
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>AgentPermit Run {html.escape(run_id)}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 32px; color: #17202a; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #d5d8dc; padding: 8px; vertical-align: top; }}
    th {{ background: #f4f6f7; text-align: left; }}
    pre {{ white-space: pre-wrap; background: #f8f9f9; padding: 8px; }}
    .status {{ display: inline-block; padding: 2px 8px; background: #eaf2f8; }}
  </style>
</head>
<body>
  <h1>AgentPermit Run {html.escape(run_id)}</h1>
  <p><strong>Task:</strong> {html.escape(run["task"])}</p>
  <p><strong>Agent:</strong> {html.escape(run["agent_name"])}</p>
  <p><strong>Status:</strong> <span class="status">{html.escape(run["status"])}</span></p>
  <p><strong>Workspace:</strong> {html.escape(run["workspace_path"])}</p>
  <h2>Approvals</h2>
  <ul>{approval_items or "<li>No approvals</li>"}</ul>
  <h2>Trace Events</h2>
  <table>
    <thead>
      <tr><th>ID</th><th>Time</th><th>Type</th><th>Tool</th><th>Risk</th><th>Message</th></tr>
    </thead>
    <tbody>
      {"".join(rows)}
    </tbody>
  </table>
</body>
</html>
"""
    output = Path(out)
    output.write_text(document, encoding="utf-8")
    return output
