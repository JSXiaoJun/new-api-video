from __future__ import annotations

import logging
import math
import shutil
import time
import uuid
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from . import database
from .config import settings
from .security import secret_box

logger = logging.getLogger("uvicorn.error")


HEALTH_SHORT_WINDOW_SECONDS = 90 * 60
HEALTH_LONG_WINDOW_SECONDS = 48 * 60 * 60
HEALTH_SHORT_SAMPLE_LIMIT = 20
HEALTH_LONG_SAMPLE_LIMIT = 200
HEALTH_DEFAULT_SCORE = 0.90
HEALTH_STREAK_PENALTY = 0.08
HEALTH_MAX_STREAK_PENALTY = 0.45
HEALTH_COST_EXPONENT = 4

# Stored image blobs are only reachable through their DB row, so a file younger
# than this grace window is never treated as an orphan: it may still belong to a
# row that is being committed by another request right now.
ORPHAN_FILE_GRACE_SECONDS = 3600
MAX_CLEANUP_BATCH = 1000
IMAGE_BLOB_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
}


def initialize() -> None:
    with database.connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS image_upstreams (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                base_url TEXT NOT NULL,
                api_key_encrypted TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                priority INTEGER NOT NULL DEFAULT 100,
                api_format TEXT NOT NULL DEFAULT 'openai',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS image_routes (
                id INTEGER PRIMARY KEY,
                upstream_id INTEGER NOT NULL REFERENCES image_upstreams(id) ON DELETE CASCADE,
                public_model TEXT NOT NULL,
                upstream_model TEXT NOT NULL,
                sizes_json TEXT NOT NULL,
                qualities_json TEXT NOT NULL,
                operations_json TEXT NOT NULL,
                cost_micros INTEGER NOT NULL DEFAULT 0,
                last_used_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_image_routes_public_model
                ON image_routes(public_model);
            CREATE TABLE IF NOT EXISTS image_request_logs (
                request_id TEXT PRIMARY KEY,
                route_id INTEGER REFERENCES image_routes(id) ON DELETE SET NULL,
                upstream_id INTEGER REFERENCES image_upstreams(id) ON DELETE SET NULL,
                upstream_name TEXT NOT NULL,
                operation TEXT NOT NULL,
                public_model TEXT NOT NULL,
                upstream_model TEXT NOT NULL,
                size TEXT,
                quality TEXT,
                cost_micros INTEGER NOT NULL DEFAULT 0,
                success INTEGER NOT NULL,
                health_outcome TEXT NOT NULL DEFAULT 'failure',
                http_status INTEGER,
                latency_ms INTEGER NOT NULL,
                error TEXT,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_image_request_logs_route_created
                ON image_request_logs(route_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_image_request_logs_upstream_model_created
                ON image_request_logs(upstream_id, public_model, upstream_model, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_image_request_logs_health
                ON image_request_logs(
                    upstream_id, public_model, upstream_model, operation, created_at DESC
                );
            CREATE INDEX IF NOT EXISTS idx_image_request_logs_created
                ON image_request_logs(created_at DESC);
            CREATE TABLE IF NOT EXISTS image_assets (
                asset_id TEXT PRIMARY KEY,
                source_url_encrypted TEXT NOT NULL,
                storage_kind TEXT NOT NULL DEFAULT 'upstream_url',
                storage_path TEXT,
                mime_type TEXT,
                byte_size INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_image_assets_created
                ON image_assets(created_at);
            """
        )
        upstream_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(image_upstreams)").fetchall()
        }
        if "api_format" not in upstream_columns:
            conn.execute("ALTER TABLE image_upstreams ADD COLUMN api_format TEXT NOT NULL DEFAULT 'openai'")
        asset_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(image_assets)").fetchall()
        }
        for column, definition in (
            ("storage_kind", "TEXT NOT NULL DEFAULT 'upstream_url'"),
            ("storage_path", "TEXT"),
            ("mime_type", "TEXT"),
            ("byte_size", "INTEGER NOT NULL DEFAULT 0"),
            ("expires_at", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in asset_columns:
                conn.execute(f"ALTER TABLE image_assets ADD COLUMN {column} {definition}")
        conn.execute(
            "UPDATE image_assets SET expires_at = created_at + ? WHERE expires_at = 0",
            (settings.image_asset_retention_seconds,),
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_image_assets_expires ON image_assets(expires_at)"
        )
        log_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(image_request_logs)").fetchall()
        }
        if "health_outcome" not in log_columns:
            conn.execute(
                "ALTER TABLE image_request_logs ADD COLUMN health_outcome TEXT NOT NULL DEFAULT 'failure'"
            )
            conn.execute(
                "UPDATE image_request_logs SET health_outcome = CASE WHEN success = 1 THEN 'success' ELSE 'failure' END"
            )


def image_storage_root() -> Path:
    return settings.image_storage_dir


def _asset_expiry(now: int) -> int:
    return now + settings.image_asset_retention_seconds


def create_image_url_asset(source_url: str) -> str:
    """Register an upstream image URL under an opaque, unguessable public id.

    Only the URL is recorded: the bytes stay on the upstream host, so a link
    stops working as soon as that host expires it.
    """
    now = int(time.time())
    asset_id = f"img_{uuid.uuid4().hex}"
    with database.connection() as conn:
        conn.execute(
            """
            INSERT INTO image_assets(
                asset_id, source_url_encrypted, storage_kind, created_at, expires_at
            ) VALUES (?, ?, 'upstream_url', ?, ?)
            """,
            (asset_id, secret_box.encrypt(source_url), now, _asset_expiry(now)),
        )
    return asset_id


def _blob_relative_path(asset_id: str, mime_type: str, now: int) -> Path:
    extension = IMAGE_BLOB_EXTENSIONS.get(mime_type.split(";")[0].strip().lower(), ".bin")
    stamp = time.gmtime(now)
    return Path(f"{stamp.tm_year:04d}") / f"{stamp.tm_mon:02d}" / f"{asset_id}{extension}"


def _free_disk_bytes(root: Path) -> int:
    probe = root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return 0


def _blob_bytes(conn) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(byte_size), 0) AS total
        FROM image_assets WHERE storage_kind = 'local_file'
        """
    ).fetchone()
    return int(row["total"] or 0)


def _delete_blob_rows(conn, rows) -> tuple[int, int]:
    """Delete stored files and their rows. A file that cannot be removed keeps
    its row, so the next cleanup pass retries instead of orphaning the file."""
    deleted_rows = 0
    deleted_files = 0
    root = image_storage_root()
    for row in rows:
        relative = row["storage_path"]
        if relative:
            try:
                (root / relative).unlink()
                deleted_files += 1
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning("image asset file delete failed: %s", exc)
                continue
        conn.execute("DELETE FROM image_assets WHERE asset_id = ?", (row["asset_id"],))
        deleted_rows += 1
    return deleted_rows, deleted_files


def _evict_oldest_blobs(conn, required_bytes: int) -> tuple[int, int]:
    """Free space for an incoming blob by dropping the oldest stored images.

    This is the capacity rule agreed for image storage: the retention window
    expires images, and the size ceiling expires the oldest ones early so the
    newest images stay viewable.
    """
    limit = settings.image_storage_max_bytes
    if not limit:
        return 0, 0
    total = _blob_bytes(conn)
    if total + required_bytes <= limit:
        return 0, 0
    selected = []
    for row in conn.execute(
        """
        SELECT asset_id, storage_path, byte_size FROM image_assets
        WHERE storage_kind = 'local_file'
        ORDER BY created_at, rowid LIMIT ?
        """,
        (MAX_CLEANUP_BATCH,),
    ).fetchall():
        selected.append(row)
        total -= int(row["byte_size"] or 0)
        if total + required_bytes <= limit:
            break
    return _delete_blob_rows(conn, selected)


def create_image_blob_asset(data: bytes, mime_type: str) -> str | None:
    """Persist generated image bytes and return the opaque public asset id.

    Returns None when the payload is empty or storage cannot take it. Callers
    must keep returning the upstream payload to the client unchanged either way.
    """
    if not data:
        return None
    size = len(data)
    limit = settings.image_storage_max_bytes
    if limit and size > limit:
        logger.warning("image blob refused: %s bytes exceed the storage ceiling", size)
        return None
    root = image_storage_root()
    if settings.image_storage_min_free_bytes and _free_disk_bytes(root) < settings.image_storage_min_free_bytes:
        logger.warning("image blob refused: free disk space below the configured floor")
        return None

    now = int(time.time())
    asset_id = f"img_{uuid.uuid4().hex}"
    relative = _blob_relative_path(asset_id, mime_type, now)
    path = root / relative
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError as exc:
        logger.warning("image blob write failed: %s", exc)
        return None
    try:
        with database.connection() as conn:
            _evict_oldest_blobs(conn, size)
            if limit and _blob_bytes(conn) + size > limit:
                path.unlink(missing_ok=True)
                logger.warning("image blob refused: storage capacity still exhausted")
                return None
            conn.execute(
                """
                INSERT INTO image_assets(
                    asset_id, source_url_encrypted, storage_kind, storage_path,
                    mime_type, byte_size, created_at, expires_at
                ) VALUES (?, ?, 'local_file', ?, ?, ?, ?, ?)
                """,
                (
                    asset_id,
                    secret_box.encrypt(""),
                    relative.as_posix(),
                    mime_type,
                    size,
                    now,
                    _asset_expiry(now),
                ),
            )
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return asset_id


def get_image_asset(asset_id: str) -> dict[str, Any] | None:
    """Resolve a public asset id. Returns None when unknown or expired.

    Expiry is enforced on read so a link stops working at its deadline even if
    the cleanup pass has not run yet.
    """
    with database.connection() as conn:
        row = conn.execute(
            """
            SELECT asset_id, source_url_encrypted, storage_kind, storage_path,
                   mime_type, byte_size, created_at, expires_at
            FROM image_assets WHERE asset_id = ?
            """,
            (asset_id,),
        ).fetchone()
    if row is None:
        return None
    asset = dict(row)
    expires_at = int(asset.pop("expires_at") or 0)
    if expires_at and expires_at <= int(time.time()):
        return None
    asset["expires_at"] = expires_at
    if asset["storage_kind"] == "local_file":
        stored = asset.pop("storage_path")
        asset["source_url"] = None
        asset["mime_type"] = asset["mime_type"] or "application/octet-stream"
        asset["local_path"] = image_storage_root() / stored if stored else None
    else:
        asset["storage_path"] = None
        asset["source_url"] = secret_box.decrypt(asset.pop("source_url_encrypted"))
        asset["local_path"] = None
    asset.pop("source_url_encrypted", None)
    return asset


def purge_expired_assets(now: int | None = None) -> tuple[int, int]:
    current = int(time.time()) if now is None else now
    with database.connection() as conn:
        rows = conn.execute(
            """
            SELECT asset_id, storage_path FROM image_assets
            WHERE expires_at > 0 AND expires_at <= ?
            ORDER BY expires_at, rowid LIMIT ?
            """,
            (current, MAX_CLEANUP_BATCH),
        ).fetchall()
        return _delete_blob_rows(conn, rows)


def purge_over_capacity() -> tuple[int, int]:
    with database.connection() as conn:
        return _evict_oldest_blobs(conn, 0)


def purge_orphan_files(now: int | None = None) -> int:
    """Delete stored files no live row references.

    Covers blobs written by a process that died before committing its row and
    files whose row was removed elsewhere. Files newer than the grace window are
    skipped so an in-flight upload is never deleted.
    """
    current = int(time.time()) if now is None else now
    root = image_storage_root()
    if not root.exists():
        return 0
    with database.connection() as conn:
        known = {
            row["storage_path"]
            for row in conn.execute(
                "SELECT storage_path FROM image_assets WHERE storage_path IS NOT NULL"
            ).fetchall()
        }
    removed = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.relative_to(root).as_posix() in known:
            continue
        try:
            if current - int(path.stat().st_mtime) < ORPHAN_FILE_GRACE_SECONDS:
                continue
            path.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("orphan image file delete failed: %s", exc)
    return removed


def purge_request_logs(now: int | None = None) -> int:
    current = int(time.time()) if now is None else now
    cutoff = current - settings.image_request_log_retention_seconds
    with database.connection() as conn:
        cursor = conn.execute("DELETE FROM image_request_logs WHERE created_at < ?", (cutoff,))
        return max(0, cursor.rowcount or 0)


def cleanup_storage() -> dict[str, int]:
    """Run one retention pass over image assets and image request logs."""
    now = int(time.time())
    expired_rows, expired_files = purge_expired_assets(now)
    evicted_rows, evicted_files = purge_over_capacity()
    summary = {
        "expired_assets": expired_rows,
        "expired_files": expired_files,
        "evicted_assets": evicted_rows,
        "evicted_files": evicted_files,
        "orphan_files": purge_orphan_files(now),
        "request_logs": purge_request_logs(now),
    }
    if any(summary.values()):
        logger.info("image storage cleanup: %s", summary)
    return summary


def storage_report() -> dict[str, Any]:
    root = image_storage_root()
    with database.connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS files, COALESCE(SUM(byte_size), 0) AS total,
                   MIN(created_at) AS oldest, MAX(created_at) AS newest
            FROM image_assets WHERE storage_kind = 'local_file'
            """
        ).fetchone()
        urls = conn.execute(
            "SELECT COUNT(*) AS total FROM image_assets WHERE storage_kind = 'upstream_url'"
        ).fetchone()
    tracked = int(row["total"] or 0)
    return {
        "root": str(root),
        "files": int(row["files"] or 0),
        "tracked_bytes": tracked,
        "tracked_megabytes": round(tracked / 1024 / 1024, 2),
        "max_bytes": settings.image_storage_max_bytes,
        "max_megabytes": round(settings.image_storage_max_bytes / 1024 / 1024, 2),
        "free_disk_bytes": _free_disk_bytes(root),
        "min_free_bytes": settings.image_storage_min_free_bytes,
        "url_assets": int(urls["total"] or 0),
        "oldest_created_at": row["oldest"],
        "newest_created_at": row["newest"],
        "asset_retention_seconds": settings.image_asset_retention_seconds,
        "log_retention_seconds": settings.image_request_log_retention_seconds,
    }


def _cost_to_micros(value: Any) -> int:
    return int((Decimal(str(value)) * Decimal(1_000_000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _cost_from_micros(value: int) -> float:
    return float(Decimal(value) / Decimal(1_000_000))


def _route_rows(conn, upstream_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM image_routes WHERE upstream_id = ? ORDER BY public_model, cost_micros, id",
        (upstream_id,),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        for legacy in ("sizes_json", "qualities_json", "operations_json"):
            item.pop(legacy, None)
        item["cost_per_request"] = _cost_from_micros(item.pop("cost_micros"))
        result.append(item)
    return result


def list_upstreams(include_keys: bool = False) -> list[dict[str, Any]]:
    with database.connection() as conn:
        rows = conn.execute("SELECT * FROM image_upstreams ORDER BY priority, id").fetchall()
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
    with database.connection() as conn:
        row = conn.execute("SELECT * FROM image_upstreams WHERE id = ?", (upstream_id,)).fetchone()
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
    api_format = payload.get("api_format") or "openai"
    with database.connection() as conn:
        if upstream_id is None:
            cursor = conn.execute(
                """
                INSERT INTO image_upstreams(
                    name, base_url, api_key_encrypted, enabled, priority, api_format,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["name"],
                    payload["base_url"],
                    secret_box.encrypt(payload["api_key"]),
                    int(payload["enabled"]),
                    payload["priority"],
                    api_format,
                    now,
                    now,
                ),
            )
            upstream_id = int(cursor.lastrowid)
        else:
            existing = conn.execute(
                "SELECT api_key_encrypted FROM image_upstreams WHERE id = ?", (upstream_id,)
            ).fetchone()
            if existing is None:
                raise KeyError("image_upstream_not_found")
            encrypted_key = existing["api_key_encrypted"]
            if payload.get("api_key"):
                encrypted_key = secret_box.encrypt(payload["api_key"])
            conn.execute(
                """
                UPDATE image_upstreams
                SET name = ?, base_url = ?, api_key_encrypted = ?, enabled = ?,
                    priority = ?, api_format = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    payload["name"],
                    payload["base_url"],
                    encrypted_key,
                    int(payload["enabled"]),
                    payload["priority"],
                    api_format,
                    now,
                    upstream_id,
                ),
            )
            conn.execute("DELETE FROM image_routes WHERE upstream_id = ?", (upstream_id,))

        # sizes_json / qualities_json / operations_json are legacy constraint
        # columns from the parameter-matching router. Routing is now a plain
        # model -> upstream forward, so they stay at the neutral "anything"
        # value and are never read.
        conn.executemany(
            """
            INSERT INTO image_routes(
                upstream_id, public_model, upstream_model, sizes_json, qualities_json,
                operations_json, cost_micros
            ) VALUES (?, ?, ?, '["*"]', '["*"]', '["*"]', ?)
            """,
            [
                (
                    upstream_id,
                    route["public_model"],
                    route["upstream_model"],
                    _cost_to_micros(route["cost_per_request"]),
                )
                for route in routes
            ],
        )
    item = get_upstream(upstream_id)
    if item is None:
        raise RuntimeError("failed_to_save_image_upstream")
    return item


def delete_upstream(upstream_id: int) -> None:
    with database.connection() as conn:
        cursor = conn.execute("DELETE FROM image_upstreams WHERE id = ?", (upstream_id,))
        if cursor.rowcount == 0:
            raise KeyError("image_upstream_not_found")


def _health_for_route(
    conn,
    route: dict[str, Any],
    now: int | None = None,
) -> dict[str, Any]:
    current_time = int(time.time()) if now is None else now
    rows = conn.execute(
        """
        SELECT rowid AS event_id, health_outcome, latency_ms, created_at
        FROM image_request_logs
        WHERE upstream_id = ?
          AND public_model = ?
          AND upstream_model = ?
          AND created_at >= ?
          AND health_outcome IN ('success', 'failure')
        ORDER BY created_at DESC, rowid DESC LIMIT ?
        """,
        (
            route["upstream_id"],
            route["public_model"],
            route["upstream_model"],
            current_time - HEALTH_LONG_WINDOW_SECONDS,
            HEALTH_LONG_SAMPLE_LIMIT,
        ),
    ).fetchall()
    if not rows:
        return {
            "state": "unobserved",
            "samples": 0,
            "success_rate": None,
            "score": HEALTH_DEFAULT_SCORE,
            "average_latency_ms": None,
            "consecutive_failures": 0,
        }

    long_successes = sum(row["health_outcome"] == "success" for row in rows)
    long_score = (long_successes + 18) / (len(rows) + 20)
    short_rows = [
        row for row in rows if row["created_at"] >= current_time - HEALTH_SHORT_WINDOW_SECONDS
    ][:HEALTH_SHORT_SAMPLE_LIMIT]
    if short_rows:
        short_successes = sum(row["health_outcome"] == "success" for row in short_rows)
        short_score = (short_successes + 4) / (len(short_rows) + 5)
    else:
        short_score = long_score
    consecutive_failures = 0
    for row in short_rows:
        if row["health_outcome"] == "success":
            break
        consecutive_failures += 1
    score = long_score * 0.65 + short_score * 0.35
    score -= min(HEALTH_MAX_STREAK_PENALTY, consecutive_failures * HEALTH_STREAK_PENALTY)
    score = max(0.05, min(0.99, score))
    latency_rows = short_rows or rows
    average_latency = round(sum(row["latency_ms"] for row in latency_rows) / len(latency_rows))
    return {
        "state": "pressure" if consecutive_failures else "stable",
        "samples": len(rows),
        "success_rate": round(long_successes / len(rows), 4),
        "score": round(score, 4),
        "average_latency_ms": average_latency,
        "consecutive_failures": consecutive_failures,
    }


def select_route(public_model: str) -> dict[str, Any] | None:
    """Resolve the upstream route for a public model.

    Routing is a plain forward: the request parameters are passed upstream
    untouched, so any enabled route registered for the model is a candidate.
    Among the candidates the healthiest one wins, with cost, priority and
    latency breaking ties.
    """
    with database.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.*, u.name AS upstream_name, u.base_url, u.api_key_encrypted,
                   u.priority, u.api_format
            FROM image_routes r
            JOIN image_upstreams u ON u.id = r.upstream_id
            WHERE u.enabled = 1 AND r.public_model = ?
            """,
            (public_model,),
        ).fetchall()
        if not rows:
            return None
        now = int(time.time())
        candidates = []
        for row in rows:
            item = dict(row)
            item["health"] = _health_for_route(conn, item, now)
            item["health_adjusted_cost"] = item["cost_micros"] / math.pow(
                item["health"]["score"], HEALTH_COST_EXPONENT
            )
            candidates.append(item)
        selected = min(
            candidates,
            key=lambda item: (
                item["health_adjusted_cost"],
                -item["health"]["score"],
                item["cost_micros"],
                item["priority"],
                item["health"]["average_latency_ms"] or 0,
                item["last_used_at"] or 0,
                item["id"],
            ),
        )
        conn.execute("UPDATE image_routes SET last_used_at = ? WHERE id = ?", (now, selected["id"]))
    selected["api_key"] = secret_box.decrypt(selected.pop("api_key_encrypted"))
    selected["api_format"] = selected.get("api_format") or "openai"
    selected["cost_per_request"] = _cost_from_micros(selected["cost_micros"])
    return selected


def record_request(
    route: dict[str, Any],
    operation: str,
    public_model: str,
    size: str,
    quality: str,
    success: bool,
    http_status: int | None,
    latency_ms: int,
    health_outcome: str,
    error: str | None = None,
) -> str:
    if health_outcome not in {"success", "failure", "neutral"}:
        raise ValueError("invalid_health_outcome")
    request_id = f"irq_{uuid.uuid4().hex}"
    with database.connection() as conn:
        conn.execute(
            """
            INSERT INTO image_request_logs(
                request_id, route_id, upstream_id, upstream_name, operation, public_model,
                upstream_model, size, quality, cost_micros, success, http_status,
                health_outcome, latency_ms, error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                route["id"],
                route["upstream_id"],
                route["upstream_name"],
                operation,
                public_model,
                route["upstream_model"],
                size or None,
                quality or None,
                route["cost_micros"],
                int(success),
                http_status,
                health_outcome,
                max(0, latency_ms),
                error[:500] if error else None,
                int(time.time()),
            ),
        )
    return request_id


def list_requests(query: str = "", outcome: str = "", limit: int = 50) -> list[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    if query:
        pattern = f"%{query}%"
        clauses.append(
            "(request_id LIKE ? OR public_model LIKE ? OR upstream_model LIKE ? OR upstream_name LIKE ?)"
        )
        params.extend([pattern, pattern, pattern, pattern])
    if outcome == "success":
        clauses.append("success = 1")
    elif outcome == "failed":
        clauses.append("success = 0")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(limit, 200)))
    with database.connection() as conn:
        rows = conn.execute(
            f"""
            SELECT request_id, upstream_name, operation, public_model, upstream_model,
                   size, quality, cost_micros, success, http_status, health_outcome,
                   latency_ms, error, created_at
            FROM image_request_logs {where}
            ORDER BY created_at DESC LIMIT ?
            """,
            params,
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["success"] = bool(item["success"])
        item["cost_per_request"] = _cost_from_micros(item.pop("cost_micros"))
        result.append(item)
    return result


def dashboard_data() -> dict[str, Any]:
    now = int(time.time())
    with database.connection() as conn:
        stats = {
            "image-upstreams": conn.execute("SELECT COUNT(*) FROM image_upstreams").fetchone()[0],
            "image-enabled": conn.execute(
                "SELECT COUNT(*) FROM image_upstreams WHERE enabled = 1"
            ).fetchone()[0],
            "image-routes": conn.execute("SELECT COUNT(*) FROM image_routes").fetchone()[0],
            "image-requests": conn.execute(
                "SELECT COUNT(*) FROM image_request_logs WHERE created_at >= ?", (now - 86400,)
            ).fetchone()[0],
        }
        upstreams = list_upstreams()
        for upstream in upstreams:
            route_health = [_health_for_route(conn, route, now) for route in upstream["routes"]]
            if not route_health:
                upstream["health"] = {
                    "state": "unobserved",
                    "samples": 0,
                    "success_rate": None,
                    "score": HEALTH_DEFAULT_SCORE,
                }
                continue
            samples = sum(item["samples"] for item in route_health)
            weighted_successes = sum(
                (item["success_rate"] or 0) * item["samples"] for item in route_health
            )
            weighted_score = sum(item["score"] * max(1, item["samples"]) for item in route_health)
            upstream["health"] = {
                "state": (
                    "pressure"
                    if any(item["state"] == "pressure" for item in route_health)
                    else "stable"
                    if samples
                    else "unobserved"
                ),
                "samples": samples,
                "success_rate": round(weighted_successes / samples, 4) if samples else None,
                "score": round(weighted_score / sum(max(1, item["samples"]) for item in route_health), 4),
            }
    return {"stats": stats, "upstreams": upstreams, "requests": list_requests()}


def list_models() -> list[str]:
    with database.connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT r.public_model
            FROM image_routes r JOIN image_upstreams u ON u.id = r.upstream_id
            WHERE u.enabled = 1 ORDER BY r.public_model
            """
        ).fetchall()
    return [row["public_model"] for row in rows]
