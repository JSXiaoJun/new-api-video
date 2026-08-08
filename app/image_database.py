from __future__ import annotations

import json
import math
import re
import time
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from . import database
from .security import secret_box


HEALTH_SHORT_WINDOW_SECONDS = 90 * 60
HEALTH_LONG_WINDOW_SECONDS = 48 * 60 * 60
HEALTH_SHORT_SAMPLE_LIMIT = 20
HEALTH_LONG_SAMPLE_LIMIT = 200
HEALTH_DEFAULT_SCORE = 0.90
HEALTH_STREAK_PENALTY = 0.08
HEALTH_MAX_STREAK_PENALTY = 0.45
HEALTH_COST_EXPONENT = 4
IMAGE_ASSET_RETENTION_SECONDS = 7 * 24 * 60 * 60


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
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_image_assets_created
                ON image_assets(created_at);
            """
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


def create_image_asset(source_url: str) -> str:
    now = int(time.time())
    asset_id = f"img_{uuid.uuid4().hex}"
    with database.connection() as conn:
        conn.execute(
            "DELETE FROM image_assets WHERE created_at < ?",
            (now - IMAGE_ASSET_RETENTION_SECONDS,),
        )
        conn.execute(
            "INSERT INTO image_assets(asset_id, source_url_encrypted, created_at) VALUES (?, ?, ?)",
            (asset_id, secret_box.encrypt(source_url), now),
        )
    return asset_id


def get_image_asset(asset_id: str) -> str | None:
    with database.connection() as conn:
        row = conn.execute(
            "SELECT source_url_encrypted FROM image_assets WHERE asset_id = ? AND created_at >= ?",
            (asset_id, int(time.time()) - IMAGE_ASSET_RETENTION_SECONDS),
        ).fetchone()
    return secret_box.decrypt(row["source_url_encrypted"]) if row else None


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
        item["sizes"] = json.loads(item.pop("sizes_json"))
        item["qualities"] = json.loads(item.pop("qualities_json"))
        item["operations"] = json.loads(item.pop("operations_json"))
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
    with database.connection() as conn:
        if upstream_id is None:
            cursor = conn.execute(
                """
                INSERT INTO image_upstreams(
                    name, base_url, api_key_encrypted, enabled, priority, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
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
            conn.execute("DELETE FROM image_routes WHERE upstream_id = ?", (upstream_id,))

        conn.executemany(
            """
            INSERT INTO image_routes(
                upstream_id, public_model, upstream_model, sizes_json, qualities_json,
                operations_json, cost_micros
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    upstream_id,
                    route["public_model"],
                    route["upstream_model"],
                    json.dumps(route["sizes"], separators=(",", ":")),
                    json.dumps(route["qualities"], separators=(",", ":")),
                    json.dumps(route["operations"], separators=(",", ":")),
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


def _matches(value: str, constraints: list[str]) -> bool:
    normalized = value.strip().lower()
    return not normalized or "*" in constraints or normalized in constraints


def _matches_size(value: str, constraints: list[str]) -> bool:
    if _matches(value, constraints):
        return True
    dimensions = re.fullmatch(r"\s*(\d{1,5})\s*x\s*(\d{1,5})\s*", value, flags=re.IGNORECASE)
    if dimensions is None:
        return False
    longest_edge = max(int(dimensions.group(1)), int(dimensions.group(2)))
    if longest_edge <= 1024:
        tier = "1k"
    elif longest_edge <= 2048:
        tier = "2k"
    elif longest_edge <= 4096:
        tier = "4k"
    else:
        return False
    return tier in constraints


def _health_for_route(
    conn,
    route: dict[str, Any],
    now: int | None = None,
    operation: str | None = None,
) -> dict[str, Any]:
    current_time = int(time.time()) if now is None else now
    rows = conn.execute(
        """
        SELECT rowid AS event_id, health_outcome, latency_ms, created_at
        FROM image_request_logs
        WHERE upstream_id = ?
          AND public_model = ?
          AND upstream_model = ?
          AND (? IS NULL OR operation = ?)
          AND created_at >= ?
          AND health_outcome IN ('success', 'failure')
        ORDER BY created_at DESC, rowid DESC LIMIT ?
        """,
        (
            route["upstream_id"],
            route["public_model"],
            route["upstream_model"],
            operation,
            operation,
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


def select_route(public_model: str, size: str, quality: str, operation: str) -> dict[str, Any] | None:
    with database.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.*, u.name AS upstream_name, u.base_url, u.api_key_encrypted, u.priority
            FROM image_routes r
            JOIN image_upstreams u ON u.id = r.upstream_id
            WHERE u.enabled = 1 AND r.public_model = ?
            """,
            (public_model,),
        ).fetchall()
        candidates = []
        now = int(time.time())
        for row in rows:
            item = dict(row)
            sizes = json.loads(item["sizes_json"])
            qualities = json.loads(item["qualities_json"])
            operations = json.loads(item["operations_json"])
            if operation not in operations or not _matches_size(size, sizes) or not _matches(quality, qualities):
                continue
            health = _health_for_route(conn, item, now, operation)
            item["health"] = health
            item["health_adjusted_cost"] = item["cost_micros"] / math.pow(
                health["score"], HEALTH_COST_EXPONENT
            )
            item["sizes"] = sizes
            item["qualities"] = qualities
            item["operations"] = operations
            candidates.append(item)
        if not candidates:
            return None
        candidates.sort(
            key=lambda item: (
                item["health_adjusted_cost"],
                -item["health"]["score"],
                item["cost_micros"],
                item["priority"],
                item["health"]["average_latency_ms"] or 0,
                item["last_used_at"] or 0,
                item["id"],
            )
        )
        selected = candidates[0]
        conn.execute("UPDATE image_routes SET last_used_at = ? WHERE id = ?", (now, selected["id"]))
    selected["api_key"] = secret_box.decrypt(selected.pop("api_key_encrypted"))
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
