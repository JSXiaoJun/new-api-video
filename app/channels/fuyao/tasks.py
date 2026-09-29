"""Fuyao task paths and create/poll response parsing.

Documented statuses: ``queued``/``pending``, ``in_progress``/``processing``,
``completed``/``done`` and ``failed``/``error``. ``done`` and ``error`` are not
in the relay's shared status table, so they are normalised here instead of
widening the table for every channel.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote


CREATE_PATH = "/v1/videos"

_STATUS = {
    "queued": "queued",
    "pending": "queued",
    "submitted": "queued",
    "in_progress": "processing",
    "processing": "processing",
    "running": "processing",
    "completed": "completed",
    "done": "completed",
    "success": "completed",
    "succeeded": "completed",
    "failed": "failed",
    "error": "failed",
    "failure": "failed",
    "cancelled": "failed",
    "expired": "failed",
}


def task_path(task_id: str) -> str:
    return f"{CREATE_PATH}/{quote(task_id, safe='')}"


def content_path(task_id: str) -> str:
    return f"{task_path(task_id)}/content"


def extract_create_task_id(payload: dict[str, Any]) -> str:
    return str(payload.get("id") or payload.get("task_id") or "").strip()


def normalize_status(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().lower()
    return _STATUS.get(normalized, normalized)


def extract_task_fields(payload: dict[str, Any], task_id: str) -> dict[str, Any]:
    """Map a ``GET /v1/videos/{id}`` body onto the relay's task fields.

    A completed task is always downloaded through ``/content``: the tutorial
    says the result URL in the body is temporary, and the relay may serve the
    download long after it expires. The relative path is joined to the
    upstream base URL at download time, which also keeps the request
    same-origin so the channel key is attached.
    """
    status = normalize_status(payload.get("status"))
    error = payload.get("error")
    if status == "failed" and not error:
        error = payload.get("message")
    return {
        "status": status,
        "video_url": content_path(task_id) if status == "completed" else None,
        "error": error,
        "progress": _progress(payload.get("progress")),
    }


def _progress(value: Any) -> int | None:
    if isinstance(value, str):
        value = value.strip().rstrip("%").strip()
    if value is None or value == "":
        return None
    try:
        return max(0, min(100, int(float(value))))
    except (TypeError, ValueError, OverflowError):
        return None
