"""SQLite index of ingested sessions ($MOTO_DATA_DIR/index.sqlite)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    created_utc TEXT,
    imported_utc TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    app_version TEXT,
    ble_schema_version INTEGER,
    packet_count INTEGER,
    loss_percent REAL,
    status TEXT,
    has_imu INTEGER NOT NULL DEFAULT 0,
    defs_version TEXT
);
"""


@dataclass
class SessionIndexRow:
    session_id: str
    created_utc: str | None
    imported_utc: str
    content_hash: str
    app_version: str | None
    ble_schema_version: int | None
    packet_count: int | None
    loss_percent: float | None
    status: str | None
    has_imu: bool
    defs_version: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> SessionIndexRow:
        return cls(
            session_id=row["session_id"],
            created_utc=row["created_utc"],
            imported_utc=row["imported_utc"],
            content_hash=row["content_hash"],
            app_version=row["app_version"],
            ble_schema_version=row["ble_schema_version"],
            packet_count=row["packet_count"],
            loss_percent=row["loss_percent"],
            status=row["status"],
            has_imu=bool(row["has_imu"]),
            defs_version=row["defs_version"],
        )


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_session(db_path: Path, row: SessionIndexRow) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO sessions (
                session_id, created_utc, imported_utc, content_hash, app_version,
                ble_schema_version, packet_count, loss_percent, status, has_imu, defs_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                created_utc=excluded.created_utc,
                imported_utc=excluded.imported_utc,
                content_hash=excluded.content_hash,
                app_version=excluded.app_version,
                ble_schema_version=excluded.ble_schema_version,
                packet_count=excluded.packet_count,
                loss_percent=excluded.loss_percent,
                status=excluded.status,
                has_imu=excluded.has_imu,
                defs_version=excluded.defs_version
            """,
            (
                row.session_id,
                row.created_utc,
                row.imported_utc,
                row.content_hash,
                row.app_version,
                row.ble_schema_version,
                row.packet_count,
                row.loss_percent,
                row.status,
                int(row.has_imu),
                row.defs_version,
            ),
        )


def get_session(db_path: Path, session_id: str) -> SessionIndexRow | None:
    with _connect(db_path) as conn:
        cur = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
        row = cur.fetchone()
        return SessionIndexRow.from_row(row) if row else None


def list_sessions(db_path: Path) -> list[SessionIndexRow]:
    with _connect(db_path) as conn:
        cur = conn.execute("SELECT * FROM sessions ORDER BY imported_utc DESC")
        return [SessionIndexRow.from_row(row) for row in cur.fetchall()]


def as_dict(row: SessionIndexRow) -> dict[str, Any]:
    return {
        "session_id": row.session_id,
        "created_utc": row.created_utc,
        "imported_utc": row.imported_utc,
        "content_hash": row.content_hash,
        "app_version": row.app_version,
        "ble_schema_version": row.ble_schema_version,
        "packet_count": row.packet_count,
        "loss_percent": row.loss_percent,
        "status": row.status,
        "has_imu": row.has_imu,
        "defs_version": row.defs_version,
    }
