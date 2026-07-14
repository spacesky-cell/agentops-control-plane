from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import new_id, utc_now
from .redaction import redact_durable, redact_text, redact_tool_payload


SCHEMA_VERSION = 2


class ApprovalNotFoundError(ValueError):
    pass


class ApprovalStateConflictError(ValueError):
    pass


@dataclass(frozen=True)
class ApprovalResolution:
    approval_id: int
    state: str
    created: bool = False
    approver: str | None = None


class AuditStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                tables = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                if "runs" not in tables:
                    self._create_schema_v2(conn)
                else:
                    version = self._schema_version(conn)
                    if version == 1:
                        self._migrate_v1_to_v2(conn)
                    elif version != SCHEMA_VERSION:
                        raise RuntimeError(
                            f"Unsupported audit schema version: {version or 'missing'}"
                        )
                self._ensure_column(
                    conn, "runs", "metadata_json", "TEXT NOT NULL DEFAULT '{}'"
                )
                self._ensure_column(conn, "runs", "workspace_identity", "TEXT")
                self._create_indexes(conn)
                conn.execute(
                    """
                    INSERT INTO meta (key, value) VALUES ('schema_version', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (str(SCHEMA_VERSION),),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _schema_version(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        return int(row["value"]) if row else 0

    def _create_schema_v2(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE runs (
                id TEXT PRIMARY KEY,
                task TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                workspace_path TEXT NOT NULL,
                workspace_identity TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE events (
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
            )
            """
        )
        self._create_approvals_table(conn)

    def _create_approvals_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE approvals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                status TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                decided_at TEXT,
                approver TEXT,
                request_fingerprint TEXT NOT NULL,
                policy_reason TEXT,
                reviewer_reason TEXT,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES runs(id)
            )
            """
        )

    def _migrate_v1_to_v2(self, conn: sqlite3.Connection) -> None:
        approval_rows = conn.execute("SELECT * FROM approvals ORDER BY id ASC").fetchall()
        conn.execute("ALTER TABLE approvals RENAME TO approvals_v1")
        self._create_approvals_table(conn)
        active_rows: dict[tuple[str, str, str], tuple[int, str]] = {}
        for row in approval_rows:
            raw_payload = row["payload_json"]
            try:
                parsed_payload = json.loads(raw_payload)
            except (TypeError, json.JSONDecodeError):
                parsed_payload = {
                    "migration_error": "Legacy approval payload was not valid JSON.",
                    "payload_sha256": hashlib.sha256(str(raw_payload).encode("utf-8")).hexdigest(),
                }
            fingerprint = ""
            if isinstance(parsed_payload, dict):
                fingerprint = str(parsed_payload.get("request_fingerprint") or "")
            if not fingerprint:
                fingerprint = self._fallback_fingerprint(
                    str(row["tool_name"]), parsed_payload
                )
            status = str(row["status"])
            if status in {"pending", "approved"}:
                active_key = (str(row["run_id"]), str(row["tool_name"]), fingerprint)
                existing = active_rows.get(active_key)
                if existing is None:
                    active_rows[active_key] = (int(row["id"]), status)
                elif status == "approved" and existing[1] == "pending":
                    conn.execute(
                        "UPDATE approvals SET status = 'superseded' WHERE id = ?",
                        (existing[0],),
                    )
                    active_rows[active_key] = (int(row["id"]), status)
                else:
                    status = "superseded"
            conn.execute(
                """
                INSERT INTO approvals (
                    id, run_id, tool_name, status, requested_at, decided_at, approver,
                    request_fingerprint, policy_reason, reviewer_reason, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["run_id"],
                    row["tool_name"],
                    status,
                    row["requested_at"],
                    row["decided_at"],
                    (
                        redact_text(row["approver"])
                        if row["approver"] is not None
                        else None
                    ),
                    fingerprint,
                    (
                        redact_text(row["reason"])
                        if row["reason"] is not None
                        else None
                    ),
                    None,
                    json.dumps(redact_durable(parsed_payload), ensure_ascii=False),
                ),
            )
        conn.execute("DROP TABLE approvals_v1")
        self._redact_legacy_runs(conn)
        self._redact_legacy_events(conn)

    def _redact_legacy_runs(self, conn: sqlite3.Connection) -> None:
        for row in conn.execute(
            "SELECT id, task, agent_name, metadata_json FROM runs"
        ).fetchall():
            raw_metadata = row["metadata_json"]
            try:
                metadata = json.loads(raw_metadata)
            except (TypeError, json.JSONDecodeError):
                metadata = {
                    "migration_error": "Legacy run metadata was not valid JSON.",
                    "metadata_sha256": hashlib.sha256(
                        str(raw_metadata).encode("utf-8")
                    ).hexdigest(),
                }
            conn.execute(
                "UPDATE runs SET task = ?, agent_name = ?, metadata_json = ? WHERE id = ?",
                (
                    redact_text(row["task"]),
                    redact_text(row["agent_name"]),
                    json.dumps(redact_durable(metadata), ensure_ascii=False),
                    row["id"],
                ),
            )

    def _redact_legacy_events(self, conn: sqlite3.Connection) -> None:
        for row in conn.execute("SELECT id, message, payload_json FROM events").fetchall():
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                payload = {
                    "migration_error": "Legacy event payload was not valid JSON.",
                    "payload_sha256": hashlib.sha256(
                        str(row["payload_json"]).encode("utf-8")
                    ).hexdigest(),
                }
            conn.execute(
                "UPDATE events SET message = ?, payload_json = ? WHERE id = ?",
                (
                    redact_text(row["message"]),
                    json.dumps(redact_durable(payload), ensure_ascii=False),
                    row["id"],
                ),
            )

    def _create_indexes(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_run_id_id ON events(run_id, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_approvals_run_id_id ON approvals(run_id, id)"
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_approvals_lookup
            ON approvals(run_id, tool_name, request_fingerprint, status, id)
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_approvals_active_unique
            ON approvals(run_id, tool_name, request_fingerprint)
            WHERE status IN ('pending', 'approved')
            """
        )

    def _ensure_column(
        self, conn: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def get_schema_version(self) -> int:
        with self._connect() as conn:
            return self._schema_version(conn)

    def set_run_metadata(self, run_id: str, metadata: dict[str, Any]) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE runs SET metadata_json = ? WHERE id = ?",
                (json.dumps(redact_durable(metadata), ensure_ascii=False), run_id),
            )
        if cursor.rowcount == 0:
            raise ValueError(f"Run not found: {run_id}")

    def get_run_metadata(self, run_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT metadata_json FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        return json.loads(row["metadata_json"]) if row else {}

    def start_run(
        self,
        task: str,
        agent_name: str,
        workspace_path: Path | None = None,
    ) -> str:
        run_id = new_id("run")
        status = "initializing" if workspace_path is None else "running"
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (id, task, agent_name, status, started_at, workspace_path)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    redact_text(task),
                    redact_text(agent_name),
                    status,
                    utc_now(),
                    str(workspace_path) if workspace_path is not None else "",
                ),
            )
        return run_id

    def activate_run_workspace(
        self,
        run_id: str,
        workspace_path: Path,
        workspace_identity: tuple[int, int],
    ) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE runs
                SET workspace_path = ?, workspace_identity = ?, status = 'running'
                WHERE id = ? AND status = 'initializing'
                """,
                (
                    str(workspace_path),
                    json.dumps(list(workspace_identity), separators=(",", ":")),
                    run_id,
                ),
            )
        if cursor.rowcount == 0:
            raise ValueError(f"Run is not initializing: {run_id}")

    def get_run_workspace_identity(self, run_id: str) -> tuple[int, int]:
        run = self.get_run(run_id)
        if not run:
            raise ValueError(f"Run not found: {run_id}")
        raw_identity = run.get("workspace_identity")
        if not raw_identity:
            raise ValueError(
                f"Run has no authoritative workspace identity: {run_id}"
            )
        try:
            parsed = json.loads(str(raw_identity))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Run has invalid authoritative workspace identity: {run_id}"
            ) from exc
        if (
            not isinstance(parsed, list)
            or len(parsed) != 2
            or any(not isinstance(value, int) for value in parsed)
        ):
            raise ValueError(
                f"Run has invalid authoritative workspace identity: {run_id}"
            )
        return int(parsed[0]), int(parsed[1])

    def finish_run(
        self,
        run_id: str,
        status: str,
        *,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
        decision: str | None = None,
    ) -> bool:
        """Atomically claim a non-terminal run and append its sole terminal event."""
        if status not in {"success", "failed"}:
            raise ValueError("terminal run status must be 'success' or 'failed'")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = self._finish_run_in_transaction(
                conn,
                run_id,
                status,
                message=message,
                payload=payload,
                decision=decision,
            )
            conn.commit()
            return changed

    def _finish_run_in_transaction(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        status: str,
        *,
        ended_at: str | None = None,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
        decision: str | None = None,
    ) -> bool:
        ended_at = ended_at or utc_now()
        cursor = conn.execute(
            """
            UPDATE runs
            SET status = ?, ended_at = ?
            WHERE id = ? AND status NOT IN ('success', 'failed')
            """,
            (status, ended_at, run_id),
        )
        if cursor.rowcount != 1:
            return False
        conn.execute(
            """
            INSERT INTO events (
                run_id, ts, type, tool_name, decision, risk, message, payload_json
            ) VALUES (?, ?, 'run_finished', NULL, ?, NULL, ?, ?)
            """,
            (
                run_id,
                ended_at,
                decision,
                redact_text(message or f"Run finished with status {status}."),
                json.dumps(redact_durable(payload or {}), ensure_ascii=False),
            ),
        )
        return True

    def pause_run(
        self,
        run_id: str,
        status: str = "waiting_for_approval",
        approval_id: int | None = None,
    ) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE runs
                SET status = ?, ended_at = NULL
                WHERE id = ?
                  AND status = 'running'
                  AND EXISTS (
                      SELECT 1 FROM approvals
                      WHERE approvals.run_id = runs.id
                        AND approvals.status IN ('pending', 'approved')
                        AND (? IS NULL OR approvals.id = ?)
                  )
                """,
                (status, run_id, approval_id, approval_id),
            )
            if cursor.rowcount == 1:
                conn.execute(
                    """
                    INSERT INTO events (
                        run_id, ts, type, tool_name, decision, risk, message, payload_json
                    ) VALUES (?, ?, 'run_paused', NULL, NULL, NULL, ?, '{}')
                    """,
                    (run_id, utc_now(), f"Run paused with status {status}."),
                )
            conn.commit()
            return cursor.rowcount == 1

    def resume_run(self, run_id: str) -> bool:
        """Atomically transition an approval-paused run back to running."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE runs
                SET status = 'running', ended_at = NULL
                WHERE id = ? AND status = 'waiting_for_approval'
                """,
                (run_id,),
            )
            return cursor.rowcount == 1

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
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    utc_now(),
                    event_type,
                    tool_name,
                    decision,
                    risk,
                    redact_text(message),
                    json.dumps(
                        redact_tool_payload(tool_name, payload or {}),
                        ensure_ascii=False,
                    ),
                ),
            )
            return int(cursor.lastrowid)

    def create_approval(
        self,
        run_id: str,
        tool_name: str,
        payload: dict[str, Any],
        reason: str,
        request_fingerprint: str | None = None,
    ) -> int:
        fingerprint = request_fingerprint or str(payload.get("request_fingerprint") or "")
        if not fingerprint:
            fingerprint = self._fallback_fingerprint(tool_name, payload)
        resolution = self.resolve_approval(
            run_id,
            tool_name,
            fingerprint,
            payload,
            reason,
        )
        return resolution.approval_id

    def resolve_approval(
        self,
        run_id: str,
        tool_name: str,
        request_fingerprint: str,
        payload: dict[str, Any],
        policy_reason: str,
        *,
        auto_approve: bool = False,
    ) -> ApprovalResolution:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rejected = self._matching_approval(
                conn, run_id, tool_name, request_fingerprint, "rejected"
            )
            if rejected:
                conn.commit()
                return ApprovalResolution(
                    int(rejected["id"]), "rejected", approver=rejected["approver"]
                )

            approved = self._matching_approval(
                conn, run_id, tool_name, request_fingerprint, "approved"
            )
            pending = self._matching_approval(
                conn, run_id, tool_name, request_fingerprint, "pending"
            )
            created = False
            active = approved or pending
            if active is None:
                cursor = conn.execute(
                    """
                    INSERT INTO approvals (
                        run_id, tool_name, status, requested_at, request_fingerprint,
                        policy_reason, payload_json
                    ) VALUES (?, ?, 'pending', ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        tool_name,
                        utc_now(),
                        request_fingerprint,
                        redact_text(policy_reason),
                        json.dumps(
                            redact_tool_payload(tool_name, payload),
                            ensure_ascii=False,
                        ),
                    ),
                )
                approval_id = int(cursor.lastrowid)
                created = True
                active_status = "pending"
            else:
                approval_id = int(active["id"])
                active_status = str(active["status"])

            if active_status == "approved":
                cursor = conn.execute(
                    "UPDATE approvals SET status = 'consumed' WHERE id = ? AND status = 'approved'",
                    (approval_id,),
                )
                if cursor.rowcount == 1:
                    approver = active["approver"] if active else None
                    conn.commit()
                    return ApprovalResolution(
                        approval_id, "consumed", created, approver
                    )

            if auto_approve:
                conn.execute(
                    """
                    UPDATE approvals
                    SET status = 'approved', decided_at = ?, approver = ?, reviewer_reason = ?
                    WHERE id = ? AND status IN ('pending', 'approved')
                    """,
                    (
                        utc_now(),
                        "auto-approve",
                        "Trusted server-side auto approval.",
                        approval_id,
                    ),
                )
                cursor = conn.execute(
                    "UPDATE approvals SET status = 'consumed' WHERE id = ? AND status = 'approved'",
                    (approval_id,),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    raise ApprovalStateConflictError(
                        f"Approval {approval_id} could not be consumed atomically."
                    )
                conn.commit()
                return ApprovalResolution(
                    approval_id, "consumed", created, "auto-approve"
                )

            conn.commit()
            return ApprovalResolution(approval_id, "pending", created)

    def _matching_approval(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        tool_name: str,
        request_fingerprint: str,
        status: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT * FROM approvals
            WHERE run_id = ? AND tool_name = ? AND request_fingerprint = ? AND status = ?
            ORDER BY id DESC LIMIT 1
            """,
            (run_id, tool_name, request_fingerprint, status),
        ).fetchone()

    def _fallback_fingerprint(self, tool_name: str, payload: Any) -> str:
        encoded = json.dumps(
            {"tool_name": tool_name, "payload": payload},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def decide_approval(
        self, approval_id: int, status: str, approver: str, reason: str = ""
    ) -> None:
        if status not in {"approved", "rejected"}:
            raise ValueError("status must be 'approved' or 'rejected'")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            decided_at = utc_now()
            cursor = conn.execute(
                """
                UPDATE approvals
                SET status = ?, decided_at = ?, approver = ?, reviewer_reason = ?
                WHERE id = ? AND status = 'pending'
                """,
                (
                    status,
                    decided_at,
                    redact_text(approver),
                    redact_text(reason),
                    approval_id,
                ),
            )
            if cursor.rowcount == 0:
                row = conn.execute(
                    "SELECT status FROM approvals WHERE id = ?", (approval_id,)
                ).fetchone()
                if row:
                    raise ApprovalStateConflictError(
                        f"Approval {approval_id} is not pending: {row['status']}"
                    )
            if status == "rejected":
                approval = conn.execute(
                    "SELECT run_id FROM approvals WHERE id = ?", (approval_id,)
                ).fetchone()
                if approval is not None:
                    run_id = str(approval["run_id"])
                    self._finish_run_in_transaction(
                        conn,
                        run_id,
                        "failed",
                        ended_at=decided_at,
                        message="Run finished with status failed after approval rejection.",
                        payload={
                            "reason": "approval_rejected",
                            "approval_id": approval_id,
                        },
                        decision="rejected",
                    )
        if cursor.rowcount == 0:
            raise ApprovalNotFoundError(f"Approval not found: {approval_id}")

    def consume_approval(self, approval_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE approvals SET status = 'consumed' WHERE id = ? AND status = 'approved'",
                (approval_id,),
            )
        return cursor.rowcount == 1

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
                "SELECT * FROM events WHERE run_id = ? ORDER BY id ASC", (run_id,)
            ).fetchall()
        events = [dict(row) for row in rows]
        for event in events:
            event["payload"] = json.loads(event.pop("payload_json"))
        return events

    def list_approvals(self, run_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if run_id:
                rows = conn.execute(
                    "SELECT * FROM approvals WHERE run_id = ? ORDER BY id ASC", (run_id,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM approvals ORDER BY id DESC").fetchall()
        approvals = [dict(row) for row in rows]
        for approval in approvals:
            approval["payload"] = json.loads(approval.pop("payload_json"))
        return approvals

    def get_approval(self, approval_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        if row is None:
            return None
        approval = dict(row)
        approval["payload"] = json.loads(approval.pop("payload_json"))
        return approval
