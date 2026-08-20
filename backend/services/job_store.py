from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
STREAM_END_STATUSES = TERMINAL_STATUSES | {"awaiting_reference"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    """Small SQLite repository; every operation owns a short-lived connection."""

    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        with self._init_lock, self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    current_step TEXT NOT NULL DEFAULT '',
                    step_index INTEGER NOT NULL DEFAULT 0,
                    total_steps INTEGER NOT NULL DEFAULT 9,
                    segments_done INTEGER NOT NULL DEFAULT 0,
                    segments_total INTEGER NOT NULL DEFAULT 0,
                    source_kind TEXT NOT NULL,
                    source_value TEXT NOT NULL,
                    reference_path TEXT,
                    settings_json TEXT NOT NULL,
                    work_dir TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS job_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_job_logs_job_id ON job_logs(job_id, id);
                CREATE INDEX IF NOT EXISTS idx_jobs_updated_at ON jobs(updated_at DESC);
                """
            )

    def create_job(
        self,
        *,
        job_id: str,
        source_kind: str,
        source_value: str,
        reference_path: str | None,
        settings: dict[str, Any],
        work_dir: str,
    ) -> dict[str, Any]:
        timestamp = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, status, source_kind, source_value, reference_path,
                    settings_json, work_dir, created_at, updated_at
                ) VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    source_kind,
                    source_value,
                    reference_path,
                    json.dumps(settings, ensure_ascii=False),
                    work_dir,
                    timestamp,
                    timestamp,
                ),
            )
        return self.get_job(job_id)

    def update_job(self, job_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {
            "status",
            "progress",
            "current_step",
            "step_index",
            "total_steps",
            "segments_done",
            "segments_total",
            "result_json",
            "error",
            "cancel_requested",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unsupported job fields: {sorted(unknown)}")
        if not fields:
            return self.get_job(job_id, include_logs=False)
        if "result_json" in fields and not isinstance(fields["result_json"], str):
            fields["result_json"] = json.dumps(fields["result_json"], ensure_ascii=False)
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = [*fields.values(), job_id]
        with self.connect() as connection:
            cursor = connection.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", values)
            if cursor.rowcount != 1:
                raise KeyError(job_id)
        # Progress updates can happen once per segment.  Returning historical
        # logs here makes a long job repeatedly fetch and serialize the same
        # rows, turning the event path into O(number_of_updates * log_history).
        return self.get_job(job_id, include_logs=False)

    def update_settings(self, job_id: str, settings: dict[str, Any]) -> dict[str, Any]:
        """Replace immutable-at-run settings while a terminal job is being retried."""

        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET settings_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(settings, ensure_ascii=False), utc_now(), job_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(job_id)
        return self.get_job(job_id)

    def select_reference(self, job_id: str, settings: dict[str, Any]) -> dict[str, Any]:
        """Atomically claim an awaiting job and persist its reference selection."""

        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET settings_json = ?, status = 'queued',
                    current_step = '参考片段已选择，等待继续处理',
                    error = NULL, cancel_requested = 0, updated_at = ?
                WHERE id = ? AND status = 'awaiting_reference'
                """,
                (json.dumps(settings, ensure_ascii=False), utc_now(), job_id),
            )
            if cursor.rowcount != 1:
                exists = connection.execute(
                    "SELECT 1 FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if exists is None:
                    raise KeyError(job_id)
                raise RuntimeError("任务已被继续或状态已变更")
        return self.get_job(job_id, include_logs=False)

    def add_log(
        self,
        job_id: str,
        message: str,
        level: str = "INFO",
    ) -> dict[str, Any] | None:
        value = message.strip()
        if not value:
            return None
        timestamp = utc_now()
        normalized_level = level.upper()
        stored_message = value[-8000:]
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO job_logs(job_id, created_at, level, message) VALUES (?, ?, ?, ?)",
                (job_id, timestamp, normalized_level, stored_message),
            )
            log_id = int(cursor.lastrowid)
        return {
            "id": log_id,
            "created_at": timestamp,
            "level": normalized_level,
            "message": stored_message,
        }

    def get_logs(self, job_id: str, *, limit: int = 400) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, created_at, level, message FROM (
                    SELECT id, created_at, level, message
                    FROM job_logs WHERE job_id = ? ORDER BY id DESC LIMIT ?
                ) ORDER BY id ASC
                """,
                (job_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_job(self, job_id: str, *, include_logs: bool = True) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        result = self._decode(row)
        if include_logs:
            result["logs"] = self.get_logs(job_id)
        return result

    def list_jobs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._decode(row) for row in rows]

    def find_active_by_source(self, source_kind: str, source_value: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE source_kind = ? AND source_value = ?
                    AND status IN ('queued', 'running', 'awaiting_reference')
                ORDER BY created_at DESC LIMIT 1
                """,
                (source_kind, source_value),
            ).fetchone()
        return self._decode(row) if row else None

    def is_cancel_requested(self, job_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return bool(row and row[0])

    def mark_interrupted_jobs(self) -> list[str]:
        """Make jobs left active by a previous process restart retryable.

        Worker tasks live only in the FastAPI process.  If that process exits
        unexpectedly, persisted ``queued``/``running`` rows cannot still have a
        worker behind them.  Preserve every cache file, but move those rows to a
        terminal state so the normal retry endpoint can resume them.
        """

        timestamp = utc_now()
        message = "上次运行意外中断；阶段缓存已保留，可以直接重试"
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM jobs WHERE status IN ('queued', 'running') ORDER BY created_at"
            ).fetchall()
            job_ids = [str(row["id"]) for row in rows]
            if not job_ids:
                return []
            placeholders = ",".join("?" for _ in job_ids)
            connection.execute(
                f"""
                UPDATE jobs
                SET status = 'failed', current_step = '上次运行意外中断',
                    error = ?, cancel_requested = 0, updated_at = ?
                WHERE id IN ({placeholders})
                """,
                [message, timestamp, *job_ids],
            )
            connection.executemany(
                "INSERT INTO job_logs(job_id, created_at, level, message) VALUES (?, ?, 'WARNING', ?)",
                [(job_id, timestamp, message) for job_id in job_ids],
            )
        return job_ids

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["settings"] = json.loads(result.pop("settings_json") or "{}")
        result["result"] = json.loads(result.pop("result_json") or "{}")
        result["cancel_requested"] = bool(result["cancel_requested"])
        return result
