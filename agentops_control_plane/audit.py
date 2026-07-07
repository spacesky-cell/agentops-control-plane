from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import new_id, utc_now


SCHEMA_VERSION = 1


class ApprovalNotFoundError(ValueError):
    pass


class AuditStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    task TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    workspace_path TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    type TEXT NOT NULL,
                    tool_name TEXT,
                    decision TEXT,
                    risk TEXT,
                    message TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );

                CREATE TABLE IF NOT EXISTS approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    decided_at TEXT,
                    approver TEXT,
                    reason TEXT,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );

                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                INSERT INTO meta (key, value)
                VALUES ('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(SCHEMA_VERSION),),
            )
            self._ensure_column(conn, "runs", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def get_schema_version(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                ("schema_version",),
            ).fetchone()
        if not row:
            return 0
        return int(row["value"])

    def set_run_metadata(self, run_id: str, metadata: dict[str, Any]) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE runs SET metadata_json = ? WHERE id = ?",
                (json.dumps(metadata, ensure_ascii=False), run_id),
            )
        if cursor.rowcount == 0:
            raise ValueError(f"Run not found: {run_id}")

    def get_run_metadata(self, run_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT metadata_json FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if not row:
            return {}
        return json.loads(row["metadata_json"])

    def start_run(self, task: str, agent_name: str, workspace_path: Path) -> str:
        run_id = new_id("run")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (id, task, agent_name, status, started_at, workspace_path)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, task, agent_name, "running", utc_now(), str(workspace_path)),
            )
        return run_id

    def finish_run(self, run_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET status = ?, ended_at = ? WHERE id = ?",
                (status, utc_now(), run_id),
            )

    def pause_run(self, run_id: str, status: str = "waiting_for_approval") -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET status = ?, ended_at = NULL WHERE id = ?",
                (status, run_id),
            )

    def add_event(
        self,
        run_id: str,
        event_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
        tool_name: str | None = None,
        decision: str | None = None,
        risk: str | None = None,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO events (
                    run_id, ts, type, tool_name, decision, risk, message, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    utc_now(),
                    event_type,
                    tool_name,
                    decision,
                    risk,
                    message,
                    json.dumps(payload or {}, ensure_ascii=False),
                ),
            )
            return int(cursor.lastrowid)

    def create_approval(
        self,
        run_id: str,
        tool_name: str,
        payload: dict[str, Any],
        reason: str,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO approvals (
                    run_id, tool_name, status, requested_at, reason, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    tool_name,
                    "pending",
                    utc_now(),
                    reason,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            return int(cursor.lastrowid)

    def decide_approval(self, approval_id: int, status: str, approver: str, reason: str = "") -> None:
        if status not in {"approved", "rejected"}:
            raise ValueError("status must be 'approved' or 'rejected'")
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE approvals
                SET status = ?, decided_at = ?, approver = ?, reason = ?
                WHERE id = ?
                """,
                (status, utc_now(), approver, reason, approval_id),
            )
        if cursor.rowcount == 0:
            raise ApprovalNotFoundError(f"Approval not found: {approval_id}")

    def consume_approval(self, approval_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE approvals SET status = ? WHERE id = ? AND status = ?",
                ("consumed", approval_id, "approved"),
            )

    def list_runs(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM runs ORDER BY started_at DESC").fetchall()
        return [dict(row) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def get_events(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY id ASC",
                (run_id,),
            ).fetchall()
        events = [dict(row) for row in rows]
        for event in events:
            event["payload"] = json.loads(event.pop("payload_json"))
        return events

    def list_approvals(self, run_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if run_id:
                rows = conn.execute(
                    "SELECT * FROM approvals WHERE run_id = ? ORDER BY id ASC",
                    (run_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM approvals ORDER BY id DESC").fetchall()
        approvals = [dict(row) for row in rows]
        for approval in approvals:
            approval["payload"] = json.loads(approval.pop("payload_json"))
        return approvals

