from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any


def _connect() -> sqlite3.Connection:
    path = os.getenv("JOBS_DB_PATH", "/var/lib/api-pdf/jobs.sqlite3")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS extraction_jobs (
            job_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            filename TEXT NOT NULL,
            document_type TEXT NOT NULL,
            locale TEXT NOT NULL,
            content BLOB,
            result_json TEXT,
            error TEXT
        )"""
    )
    return connection


def create_job(job_id: str, filename: str, document_type: str, locale: str, content: bytes) -> None:
    now = time.time()
    with _connect() as db:
        retention_days = max(1, int(os.getenv("JOB_RETENTION_DAYS", "7")))
        db.execute("DELETE FROM extraction_jobs WHERE updated_at < ?", (now - retention_days * 86400,))
        db.execute(
            "INSERT INTO extraction_jobs (job_id,status,created_at,updated_at,filename,document_type,locale,content) "
            "VALUES (?, 'queued', ?, ?, ?, ?, ?, ?)",
            (job_id, now, now, filename, document_type, locale, content),
        )


def get_job(job_id: str, *, include_content: bool = False) -> dict[str, Any] | None:
    with _connect() as db:
        row = db.execute("SELECT * FROM extraction_jobs WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    if not include_content:
        result.pop("content", None)
    if result.get("result_json"):
        result["result"] = json.loads(result.pop("result_json"))
    else:
        result.pop("result_json", None)
    return result


def set_job_status(job_id: str, status: str, *, result: dict[str, Any] | None = None, error: str | None = None) -> None:
    with _connect() as db:
        db.execute(
            "UPDATE extraction_jobs SET status=?, updated_at=?, result_json=?, error=? WHERE job_id=?",
            (status, time.time(), json.dumps(result, ensure_ascii=False) if result is not None else None, error, job_id),
        )


def delete_job(job_id: str) -> None:
    with _connect() as db:
        db.execute("DELETE FROM extraction_jobs WHERE job_id=?", (job_id,))


def discard_job_content(job_id: str) -> None:
    with _connect() as db:
        db.execute(
            "UPDATE extraction_jobs SET content=NULL, updated_at=? WHERE job_id=?",
            (time.time(), job_id),
        )
