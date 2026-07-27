from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from .config import settings
from .security import secret_box


settings.data_dir.mkdir(parents=True, exist_ok=True)
DB_PATH = settings.data_dir / "adapter.db"


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def initialize() -> None:
    with connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS upstreams (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                base_url TEXT NOT NULL,
                api_key_encrypted TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                priority INTEGER NOT NULL DEFAULT 100,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_used_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS model_routes (
                id INTEGER PRIMARY KEY,
                upstream_id INTEGER NOT NULL REFERENCES upstreams(id) ON DELETE CASCADE,
                model TEXT NOT NULL,
                protocol TEXT NOT NULL CHECK(protocol IN ('videos', 'seedance')),
                UNIQUE(upstream_id, model)
            );
            CREATE INDEX IF NOT EXISTS idx_model_routes_model ON model_routes(model);
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                upstream_id INTEGER NOT NULL REFERENCES upstreams(id),
                model TEXT NOT NULL,
                protocol TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                source_video_url TEXT,
                error TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at DESC);
            """
        )


def _route_rows(conn: sqlite3.Connection, upstream_id: int) -> list[dict[str, str]]:
    rows = conn.execute(
        "SELECT model, protocol FROM model_routes WHERE upstream_id = ? ORDER BY model", (upstream_id,)
    ).fetchall()
    return [dict(row) for row in rows]


def list_upstreams(include_keys: bool = False) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute("SELECT * FROM upstreams ORDER BY priority, id").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["enabled"] = bool(item["enabled"])
            item["routes"] = _route_rows(conn, item["id"])
            if include_keys:
                item["api_key"] = secret_box.decrypt(item.pop("api_key_encrypted"))
            else:
                item.pop("api_key_encrypted")
                item["api_key_set"] = True
            result.append(item)
        return result


def get_upstream(upstream_id: int, include_key: bool = False) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM upstreams WHERE id = ?", (upstream_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        item["routes"] = _route_rows(conn, upstream_id)
        if include_key:
            item["api_key"] = secret_box.decrypt(item.pop("api_key_encrypted"))
        else:
            item.pop("api_key_encrypted")
        return item


def save_upstream(payload: dict[str, Any], upstream_id: int | None = None) -> dict[str, Any]:
    now = int(time.time())
    routes = payload["routes"]
    with connection() as conn:
        if upstream_id is None:
            cursor = conn.execute(
                """
                INSERT INTO upstreams(name, base_url, api_key_encrypted, enabled, priority, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["name"],
                    payload["base_url"],
                    secret_box.encrypt(payload["api_key"]),
                    int(payload["enabled"]),
                    payload["priority"],
                    now,
                    now,
                ),
            )
            upstream_id = int(cursor.lastrowid)
        else:
            existing = conn.execute("SELECT api_key_encrypted FROM upstreams WHERE id = ?", (upstream_id,)).fetchone()
            if existing is None:
                raise KeyError("upstream_not_found")
            encrypted_key = existing["api_key_encrypted"]
            if payload.get("api_key"):
                encrypted_key = secret_box.encrypt(payload["api_key"])
            conn.execute(
                """
                UPDATE upstreams
                SET name = ?, base_url = ?, api_key_encrypted = ?, enabled = ?, priority = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    payload["name"],
                    payload["base_url"],
                    encrypted_key,
                    int(payload["enabled"]),
                    payload["priority"],
                    now,
                    upstream_id,
                ),
            )
            conn.execute("DELETE FROM model_routes WHERE upstream_id = ?", (upstream_id,))

        conn.executemany(
            "INSERT INTO model_routes(upstream_id, model, protocol) VALUES (?, ?, ?)",
            [(upstream_id, route["model"], route["protocol"]) for route in routes],
        )
    item = get_upstream(upstream_id)
    if item is None:
        raise RuntimeError("failed_to_save_upstream")
    return item


def delete_upstream(upstream_id: int) -> None:
    with connection() as conn:
        task_count = conn.execute("SELECT COUNT(*) FROM tasks WHERE upstream_id = ?", (upstream_id,)).fetchone()[0]
        if task_count:
            raise ValueError("upstream_has_tasks")
        cursor = conn.execute("DELETE FROM upstreams WHERE id = ?", (upstream_id,))
        if cursor.rowcount == 0:
            raise KeyError("upstream_not_found")


def select_upstream(model: str) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(
            """
            SELECT u.*, r.protocol
            FROM upstreams u
            JOIN model_routes r ON r.upstream_id = u.id
            WHERE u.enabled = 1 AND r.model = ?
            ORDER BY u.priority ASC, COALESCE(u.last_used_at, 0) ASC, u.id ASC
            LIMIT 1
            """,
            (model,),
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE upstreams SET last_used_at = ? WHERE id = ?", (int(time.time()), row["id"]))
        item = dict(row)
        item["api_key"] = secret_box.decrypt(item.pop("api_key_encrypted"))
        return item


def create_task(task_id: str, upstream_id: int, model: str, protocol: str, status: str) -> None:
    now = int(time.time())
    with connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO tasks(task_id, upstream_id, model, protocol, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM tasks WHERE task_id = ?), ?), ?)
            """,
            (task_id, upstream_id, model, protocol, status, task_id, now, now),
        )


def get_task(task_id: str) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(
            """
            SELECT t.*, u.name AS upstream_name, u.base_url, u.api_key_encrypted
            FROM tasks t JOIN upstreams u ON u.id = t.upstream_id
            WHERE t.task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["api_key"] = secret_box.decrypt(item.pop("api_key_encrypted"))
        return item


def update_task(task_id: str, status: str, source_video_url: str | None, error: str | None) -> None:
    with connection() as conn:
        conn.execute(
            """
            UPDATE tasks SET status = ?, source_video_url = COALESCE(?, source_video_url),
                error = ?, updated_at = ? WHERE task_id = ?
            """,
            (status, source_video_url, error, int(time.time()), task_id),
        )


def dashboard_data() -> dict[str, Any]:
    with connection() as conn:
        upstreams = conn.execute("SELECT COUNT(*) FROM upstreams").fetchone()[0]
        enabled = conn.execute("SELECT COUNT(*) FROM upstreams WHERE enabled = 1").fetchone()[0]
        models = conn.execute("SELECT COUNT(DISTINCT model) FROM model_routes").fetchone()[0]
        tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        recent_rows = conn.execute(
            """
            SELECT t.task_id, t.model, t.status, t.error, t.created_at, t.updated_at, u.name AS upstream_name
            FROM tasks t JOIN upstreams u ON u.id = t.upstream_id
            ORDER BY t.created_at DESC LIMIT 50
            """
        ).fetchall()
    return {
        "stats": {"upstreams": upstreams, "enabled": enabled, "models": models, "tasks": tasks},
        "upstreams": list_upstreams(),
        "tasks": [dict(row) for row in recent_rows],
    }


def list_models() -> list[str]:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT r.model FROM model_routes r
            JOIN upstreams u ON u.id = r.upstream_id
            WHERE u.enabled = 1 ORDER BY r.model
            """
        ).fetchall()
    return [row["model"] for row in rows]

