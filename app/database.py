from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from .config import DEFAULT_PUBLIC_LINK_BASE_URL, PUBLIC_LINK_BASE_URLS, settings
from .model_profiles import MAX_DURATION_SECONDS, capabilities_for, suggest_profile
from .security import secret_box


PUBLIC_VIDEO_DOWNLOAD_LIMIT = 50
PUBLIC_VIDEO_LINK_TTL_SECONDS = 24 * 60 * 60


settings.data_dir.mkdir(parents=True, exist_ok=True)
DB_PATH = settings.data_dir / "adapter.db"


def _new_relay_request_id(prefix: str = "vrq") -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _encrypt_json(value: Any) -> str:
    return secret_box.encrypt(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _decrypt_json(value: str | None) -> Any:
    if not value:
        return None
    return json.loads(secret_box.decrypt(value))


def _encrypt_text(value: str | None) -> str | None:
    return secret_box.encrypt(value) if value else None


def _decrypt_text(value: str | None) -> str | None:
    return secret_box.decrypt(value) if value else None


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
                upstream_model TEXT NOT NULL,
                protocol TEXT NOT NULL CHECK(protocol IN ('videos', 'seedance')),
                profile TEXT NOT NULL DEFAULT 'default',
                duration_override INTEGER,
                UNIQUE(upstream_id, model)
            );
            CREATE INDEX IF NOT EXISTS idx_model_routes_model ON model_routes(model);
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                upstream_id INTEGER NOT NULL REFERENCES upstreams(id),
                relay_request_id TEXT,
                model TEXT NOT NULL,
                protocol TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                source_video_url TEXT,
                error TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at DESC);
            CREATE TABLE IF NOT EXISTS audit_requests (
                relay_request_id TEXT PRIMARY KEY,
                upstream_id INTEGER NOT NULL REFERENCES upstreams(id),
                upstream_task_id TEXT,
                public_task_id TEXT,
                model TEXT NOT NULL,
                protocol TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                request_payload_encrypted TEXT,
                source_video_url_encrypted TEXT,
                error TEXT,
                public_download_count INTEGER NOT NULL DEFAULT 0,
                public_download_expires_at INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_audit_requests_created_at
                ON audit_requests(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_audit_requests_upstream_task_id
                ON audit_requests(upstream_task_id);
            CREATE INDEX IF NOT EXISTS idx_audit_requests_public_task_id
                ON audit_requests(public_task_id);
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY,
                relay_request_id TEXT NOT NULL REFERENCES audit_requests(relay_request_id) ON DELETE CASCADE,
                phase TEXT NOT NULL,
                http_status INTEGER,
                upstream_body_encrypted TEXT,
                sanitized_body_encrypted TEXT,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_audit_events_request
                ON audit_events(relay_request_id, id DESC);
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """
        )
        default_public_link_base_url = (
            settings.new_api_public_base_url
            if settings.new_api_public_base_url in PUBLIC_LINK_BASE_URLS
            else DEFAULT_PUBLIC_LINK_BASE_URL
        )
        conn.execute(
            "INSERT OR IGNORE INTO app_settings(key, value, updated_at) VALUES('public_link_base_url', ?, ?)",
            (default_public_link_base_url, int(time.time())),
        )
        task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "relay_request_id" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN relay_request_id TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_relay_request_id ON tasks(relay_request_id)")

        audit_columns = {row["name"] for row in conn.execute("PRAGMA table_info(audit_requests)").fetchall()}
        if "public_download_count" not in audit_columns:
            conn.execute(
                "ALTER TABLE audit_requests ADD COLUMN public_download_count INTEGER NOT NULL DEFAULT 0"
            )
        if "public_download_expires_at" not in audit_columns:
            conn.execute("ALTER TABLE audit_requests ADD COLUMN public_download_expires_at INTEGER")
        conn.execute(
            """
            UPDATE audit_requests
            SET public_download_expires_at = updated_at + ?
            WHERE public_task_id IS NOT NULL AND public_download_expires_at IS NULL
            """,
            (PUBLIC_VIDEO_LINK_TTL_SECONDS,),
        )

        route_columns = {row["name"] for row in conn.execute("PRAGMA table_info(model_routes)").fetchall()}
        if "upstream_model" not in route_columns:
            conn.execute("ALTER TABLE model_routes ADD COLUMN upstream_model TEXT")
            conn.execute("UPDATE model_routes SET upstream_model = model WHERE upstream_model IS NULL OR upstream_model = ''")
        profile_added = "profile" not in route_columns
        if profile_added:
            conn.execute("ALTER TABLE model_routes ADD COLUMN profile TEXT NOT NULL DEFAULT 'default'")
        if "duration_override" not in route_columns:
            conn.execute("ALTER TABLE model_routes ADD COLUMN duration_override INTEGER")
        if "durations_json" not in route_columns:
            conn.execute("ALTER TABLE model_routes ADD COLUMN durations_json TEXT NOT NULL DEFAULT '[]'")
            legacy_routes = conn.execute(
                "SELECT id, duration_override FROM model_routes WHERE duration_override IS NOT NULL"
            ).fetchall()
            for route in legacy_routes:
                if 1 <= route["duration_override"] <= MAX_DURATION_SECONDS:
                    conn.execute(
                        "UPDATE model_routes SET durations_json = ? WHERE id = ?",
                        (json.dumps([route["duration_override"]]), route["id"]),
                    )
        for column in ("supports_image", "supports_video", "supports_audio"):
            if column not in route_columns:
                conn.execute(f"ALTER TABLE model_routes ADD COLUMN {column} INTEGER NOT NULL DEFAULT 1")
        if "image_count" not in route_columns:
            conn.execute("ALTER TABLE model_routes ADD COLUMN image_count INTEGER")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_model_routes_upstream_model ON model_routes(upstream_id, upstream_model)"
        )
        if profile_added:
            for model in (
                "gemini-omni-flash",
                "sora2",
                "veo31-fast",
                "manxue-900",
                "manxue-933",
                "grok-imagine-1.0-video",
                "grok-imagine-video-1.5-fast",
                "grok-imagine-video-1.5-preview",
            ):
                conn.execute(
                    "UPDATE model_routes SET profile = ? WHERE model = ? AND profile = 'default'",
                    (suggest_profile(model, "videos"), model),
                )

        legacy_tasks = conn.execute(
            """
            SELECT task_id, upstream_id, model, protocol, status, source_video_url, error, created_at, updated_at
            FROM tasks WHERE relay_request_id IS NULL OR relay_request_id = ''
            """
        ).fetchall()
        for task in legacy_tasks:
            relay_request_id = _new_relay_request_id("vrq_legacy")
            conn.execute("UPDATE tasks SET relay_request_id = ? WHERE task_id = ?", (relay_request_id, task["task_id"]))
            conn.execute(
                """
                INSERT OR IGNORE INTO audit_requests(
                    relay_request_id, upstream_id, upstream_task_id, model, protocol, status,
                    source_video_url_encrypted, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    relay_request_id,
                    task["upstream_id"],
                    task["task_id"],
                    task["model"],
                    task["protocol"],
                    task["status"],
                    _encrypt_text(task["source_video_url"]),
                    task["error"],
                    task["created_at"],
                    task["updated_at"],
                ),
            )


def _route_rows(conn: sqlite3.Connection, upstream_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT model, upstream_model, protocol, profile, duration_override, durations_json,
               image_count, supports_image, supports_video, supports_audio
        FROM model_routes WHERE upstream_id = ? ORDER BY model
        """,
        (upstream_id,),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["durations"] = _decode_durations(item.pop("durations_json"), item["duration_override"])
        if item["image_count"] is None:
            item["image_count"] = capabilities_for(
                item["profile"],
                item["durations"],
                bool(item["supports_image"]),
                bool(item["supports_video"]),
                bool(item["supports_audio"]),
            ).get("maxImages", 0)
        item["supports_image"] = bool(item["supports_image"])
        item["supports_video"] = bool(item["supports_video"])
        item["supports_audio"] = bool(item["supports_audio"])
        item["mapped_upstream_model"] = "" if item["model"] == item["upstream_model"] else item["upstream_model"]
        result.append(item)
    return result


def _decode_durations(value: str | None, legacy_duration: int | None = None) -> list[int]:
    try:
        durations = json.loads(value or "[]")
    except (TypeError, ValueError):
        durations = []
    if not isinstance(durations, list):
        durations = []
    normalized = sorted({duration for duration in durations if isinstance(duration, int) and 1 <= duration <= MAX_DURATION_SECONDS})
    if normalized:
        return normalized
    if isinstance(legacy_duration, int) and 1 <= legacy_duration <= MAX_DURATION_SECONDS:
        return [legacy_duration]
    return []


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
    prepared_routes = [
        (
            route,
            route.get("durations")
            or ([route["duration_override"]] if isinstance(route.get("duration_override"), int) and 1 <= route["duration_override"] <= MAX_DURATION_SECONDS else []),
        )
        for route in routes
    ]
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
            """
            INSERT INTO model_routes(
                upstream_id, model, upstream_model, protocol, profile, duration_override, durations_json,
                image_count, supports_image, supports_video, supports_audio
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    upstream_id,
                    route["model"],
                    route.get("upstream_model") or route["model"],
                    route["protocol"],
                    route.get("profile", "default"),
                    durations[0] if len(durations) == 1 else None,
                    json.dumps(durations),
                    route.get("image_count"),
                    int(route.get("supports_image", route.get("image_count") is None or route.get("image_count", 0) > 0)),
                    int(route.get("supports_video", True)),
                    int(route.get("supports_audio", True)),
                )
                for route, durations in prepared_routes
            ],
        )
    item = get_upstream(upstream_id)
    if item is None:
        raise RuntimeError("failed_to_save_upstream")
    return item


def delete_upstream(upstream_id: int) -> None:
    with connection() as conn:
        task_count = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM tasks WHERE upstream_id = ?)
                + (SELECT COUNT(*) FROM audit_requests WHERE upstream_id = ?)
            """,
            (upstream_id, upstream_id),
        ).fetchone()[0]
        if task_count:
            raise ValueError("upstream_has_tasks")
        cursor = conn.execute("DELETE FROM upstreams WHERE id = ?", (upstream_id,))
        if cursor.rowcount == 0:
            raise KeyError("upstream_not_found")


def select_upstream(model: str) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(
            """
            SELECT u.*, r.protocol, r.profile, r.duration_override, r.upstream_model
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


def start_audit_request(upstream_id: int, model: str, protocol: str, request_payload: dict[str, Any]) -> str:
    relay_request_id = _new_relay_request_id()
    now = int(time.time())
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO audit_requests(
                relay_request_id, upstream_id, model, protocol, status,
                request_payload_encrypted, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)
            """,
            (relay_request_id, upstream_id, model, protocol, _encrypt_json(request_payload), now, now),
        )
    return relay_request_id


def record_audit_event(
    relay_request_id: str,
    phase: str,
    http_status: int | None,
    upstream_body: str | None,
    sanitized_body: dict[str, Any] | None,
) -> None:
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO audit_events(
                relay_request_id, phase, http_status, upstream_body_encrypted,
                sanitized_body_encrypted, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                relay_request_id,
                phase,
                http_status,
                _encrypt_text(upstream_body),
                _encrypt_json(sanitized_body) if sanitized_body is not None else None,
                int(time.time()),
            ),
        )


def fail_audit_request(relay_request_id: str, error: str) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE audit_requests SET status = 'failed', error = ?, updated_at = ? WHERE relay_request_id = ?",
            (error, int(time.time()), relay_request_id),
        )


def create_task(
    task_id: str,
    upstream_id: int,
    relay_request_id: str,
    model: str,
    protocol: str,
    status: str,
) -> None:
    now = int(time.time())
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO tasks(
                task_id, upstream_id, relay_request_id, model, protocol, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                upstream_id = excluded.upstream_id,
                relay_request_id = excluded.relay_request_id,
                model = excluded.model,
                protocol = excluded.protocol,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (task_id, upstream_id, relay_request_id, model, protocol, status, now, now),
        )
        conn.execute(
            """
            UPDATE audit_requests
            SET upstream_task_id = ?, status = ?, updated_at = ?
            WHERE relay_request_id = ?
            """,
            (task_id, status, now, relay_request_id),
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
        task = conn.execute("SELECT relay_request_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if task is not None and task["relay_request_id"]:
            conn.execute(
                """
                UPDATE audit_requests
                SET status = ?, source_video_url_encrypted = COALESCE(?, source_video_url_encrypted),
                    error = ?, updated_at = ?
                WHERE relay_request_id = ?
                """,
                (
                    status,
                    _encrypt_text(source_video_url),
                    error,
                    int(time.time()),
                    task["relay_request_id"],
                ),
            )


def list_audit_requests(query: str = "", status: str = "", limit: int = 50) -> list[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    if query:
        pattern = f"%{query}%"
        clauses.append(
            "(a.relay_request_id LIKE ? OR a.upstream_task_id LIKE ? OR a.public_task_id LIKE ? OR a.model LIKE ?)"
        )
        params.extend([pattern, pattern, pattern, pattern])
    if status:
        clauses.append("a.status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(limit, 200)))
    with connection() as conn:
        rows = conn.execute(
            f"""
            SELECT a.relay_request_id, a.upstream_task_id, a.public_task_id, a.model, a.protocol,
                   a.status, a.error, a.created_at, a.updated_at, u.name AS upstream_name
            FROM audit_requests a JOIN upstreams u ON u.id = a.upstream_id
            {where}
            ORDER BY a.created_at DESC LIMIT ?
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def get_audit_request(relay_request_id: str) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(
            """
            SELECT a.*, u.name AS upstream_name, u.base_url
            FROM audit_requests a JOIN upstreams u ON u.id = a.upstream_id
            WHERE a.relay_request_id = ?
            """,
            (relay_request_id,),
        ).fetchone()
        if row is None:
            return None
        events = conn.execute(
            """
            SELECT id, phase, http_status, upstream_body_encrypted,
                   sanitized_body_encrypted, created_at
            FROM audit_events WHERE relay_request_id = ? ORDER BY id DESC
            """,
            (relay_request_id,),
        ).fetchall()

    result = dict(row)
    result["request_payload"] = _decrypt_json(result.pop("request_payload_encrypted"))
    result["source_video_url"] = _decrypt_text(result.pop("source_video_url_encrypted"))
    if result["status"] == "completed" and result["public_task_id"]:
        result["sanitized_video_url"] = public_video_url(result["public_task_id"])
    else:
        result["sanitized_video_url"] = None
    result["events"] = []
    for event_row in events:
        event = dict(event_row)
        event["upstream_body"] = _decrypt_text(event.pop("upstream_body_encrypted"))
        event["sanitized_body"] = _decrypt_json(event.pop("sanitized_body_encrypted"))
        if (
            result["sanitized_video_url"]
            and isinstance(event["sanitized_body"], dict)
            and event["sanitized_body"].get("status") == "completed"
        ):
            event["sanitized_body"].update(
                {
                    "url": result["sanitized_video_url"],
                    "video_url": result["sanitized_video_url"],
                    "result_url": result["sanitized_video_url"],
                    "download_url": result["sanitized_video_url"],
                }
            )
        result["events"].append(event)
    return result


def set_public_task_id(relay_request_id: str, public_task_id: str) -> bool:
    normalized_public_task_id = public_task_id or None
    now = int(time.time())
    with connection() as conn:
        existing = conn.execute(
            "SELECT public_task_id FROM audit_requests WHERE relay_request_id = ?",
            (relay_request_id,),
        ).fetchone()
        if existing is None:
            return False
        if normalized_public_task_id:
            conflict = conn.execute(
                """
                SELECT relay_request_id FROM audit_requests
                WHERE public_task_id = ? AND relay_request_id != ?
                """,
                (normalized_public_task_id, relay_request_id),
            ).fetchone()
            if conflict is not None:
                raise ValueError("public_task_id_in_use")
        cursor = conn.execute(
            """
            UPDATE audit_requests
            SET public_task_id = ?, public_download_count = 0,
                public_download_expires_at = ?, updated_at = ?
            WHERE relay_request_id = ?
            """,
            (
                normalized_public_task_id,
                now + PUBLIC_VIDEO_LINK_TTL_SECONDS if normalized_public_task_id else None,
                now,
                relay_request_id,
            ),
        )
    return cursor.rowcount > 0


def get_public_link_base_url() -> str:
    with connection() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = 'public_link_base_url'",
        ).fetchone()
    if row is None or row["value"] not in PUBLIC_LINK_BASE_URLS:
        return DEFAULT_PUBLIC_LINK_BASE_URL
    return row["value"]


def public_video_url(task_id: str) -> str:
    return f"{get_public_link_base_url()}/public/videos/{task_id}/content"


def get_task_by_public_task_id(public_task_id: str) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(
            """
            SELECT t.task_id
            FROM tasks t
            JOIN audit_requests a ON a.relay_request_id = t.relay_request_id
            WHERE a.public_task_id = ?
              AND a.status = 'completed'
              AND t.status = 'completed'
            LIMIT 1
            """,
            (public_task_id,),
        ).fetchone()
    if row is None:
        return None
    return get_task(row["task_id"])


def reserve_public_video_download(public_task_id: str, now: int | None = None) -> str:
    current_time = int(time.time()) if now is None else now
    with connection() as conn:
        cursor = conn.execute(
            """
            UPDATE audit_requests
            SET public_download_count = public_download_count + 1
            WHERE public_task_id = ?
              AND status = 'completed'
              AND public_download_expires_at > ?
              AND public_download_count < ?
            """,
            (public_task_id, current_time, PUBLIC_VIDEO_DOWNLOAD_LIMIT),
        )
        if cursor.rowcount > 0:
            return "reserved"
        row = conn.execute(
            """
            SELECT status, public_download_expires_at, public_download_count
            FROM audit_requests WHERE public_task_id = ?
            """,
            (public_task_id,),
        ).fetchone()
    if row is None:
        return "not_found"
    if row["status"] != "completed":
        return "not_completed"
    if row["public_download_expires_at"] is None or row["public_download_expires_at"] <= current_time:
        return "expired"
    if row["public_download_count"] >= PUBLIC_VIDEO_DOWNLOAD_LIMIT:
        return "limit_reached"
    return "unavailable"


def release_public_video_download(public_task_id: str) -> None:
    with connection() as conn:
        conn.execute(
            """
            UPDATE audit_requests
            SET public_download_count = public_download_count - 1
            WHERE public_task_id = ? AND public_download_count > 0
            """,
            (public_task_id,),
        )


def set_public_link_base_url(value: str) -> str:
    if value not in PUBLIC_LINK_BASE_URLS:
        raise ValueError("Unsupported public link base URL")
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO app_settings(key, value, updated_at) VALUES('public_link_base_url', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (value, int(time.time())),
        )
    return value


def get_task_by_relay_request_id(relay_request_id: str) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute("SELECT task_id FROM tasks WHERE relay_request_id = ?", (relay_request_id,)).fetchone()
    if row is None:
        return None
    return get_task(row["task_id"])


def dashboard_data() -> dict[str, Any]:
    with connection() as conn:
        upstreams = conn.execute("SELECT COUNT(*) FROM upstreams").fetchone()[0]
        enabled = conn.execute("SELECT COUNT(*) FROM upstreams WHERE enabled = 1").fetchone()[0]
        models = conn.execute("SELECT COUNT(DISTINCT model) FROM model_routes").fetchone()[0]
        tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    return {
        "stats": {"upstreams": upstreams, "enabled": enabled, "models": models, "tasks": tasks},
        "upstreams": list_upstreams(),
        "tasks": list_audit_requests(),
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


def list_model_capabilities() -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT r.model, r.profile, r.duration_override, r.durations_json,
                   r.image_count, r.supports_image, r.supports_video, r.supports_audio
            FROM model_routes r
            JOIN upstreams u ON u.id = r.upstream_id
            WHERE u.enabled = 1
            ORDER BY r.model, u.priority, u.id
            """
        ).fetchall()
    result = []
    seen = set()
    for row in rows:
        if row["model"] in seen:
            continue
        seen.add(row["model"])
        result.append({
            "id": row["model"],
            "capabilities": capabilities_for(
                row["profile"],
                _decode_durations(row["durations_json"], row["duration_override"]),
                bool(row["supports_image"]),
                bool(row["supports_video"]),
                bool(row["supports_audio"]),
                row["image_count"],
            ),
        })
    return result
