"""Wangyue task paths and create/poll response parsing.

Statuses: ``queued`` (waiting for a pool node), ``leased`` (a node is
running it), ``review_required`` (paused for a safe-recovery check, then
continues), ``completed``, ``failed`` and ``cancelled``.

``review_required`` is not a failure: reporting it as one would make New API
refund a task the pool still finishes. It is held as ``processing``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote


TASKS_PATH = "/v1/pool/api/tasks"
CREATE_PATH = TASKS_PATH
ASSET_LEASE_PATH = "/v1/pool/api/assets/input/lease"
# Poll answers keep this marker as the task's video link; the real signed
# download URL is fetched from ``/result`` at download time, because it
# expires within minutes.
RESULT_MARKER_SUFFIX = "/result"

_STATUS = {
    "queued": "queued",
    "leased": "processing",
    "review_required": "processing",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "failed",
}
_PROGRESS = {"queued": 0, "leased": 50, "review_required": 50}


def task_path(task_id: str) -> str:
    return f"{TASKS_PATH}/{quote(task_id, safe='')}"


def result_path(task_id: str) -> str:
    return f"{task_path(task_id)}{RESULT_MARKER_SUFFIX}"


def asset_confirm_path(asset_id: str) -> str:
    return f"/v1/pool/api/assets/{quote(asset_id, safe='')}/confirm"


def _task(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and isinstance(payload.get("task"), dict):
        return payload["task"]
    return {}


def extract_create_task_id(payload: dict[str, Any]) -> str:
    return str(_task(payload).get("id") or "").strip()


def normalize_status(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().lower()
    return _STATUS.get(normalized, normalized)


def extract_create_status(payload: dict[str, Any]) -> str | None:
    return normalize_status(_task(payload).get("status"))


def extract_task_fields(payload: dict[str, Any], task_id: str) -> dict[str, Any]:
    task = _task(payload)
    raw_status = str(task.get("status") or "").strip().lower()
    status = normalize_status(raw_status)
    error = task.get("error")
    if status == "failed" and not error:
        error = "任务已取消" if raw_status == "cancelled" else payload.get("message")
    return {
        "status": status,
        "video_url": result_path(task_id) if status == "completed" else None,
        "error": error,
        "progress": _PROGRESS.get(raw_status),
    }


# Codes the gateway uses for a request it would not authenticate. They say
# nothing about the task. ``api_key_id_invalid`` (HTTP 400) is what the live
# gateway answers for an unknown key ID; the guide does not list it.
_CREDENTIAL_ERROR_PREFIXES = ("api_key", "pool_api_")


def is_credential_rejection(status_code: int, payload: Any) -> bool:
    """Whether a failed read was refused for our credentials, not the task."""
    if status_code in (401, 403):
        return True
    code = payload.get("error") if isinstance(payload, dict) else None
    return (
        status_code in (400, 409, 503)
        and isinstance(code, str)
        and code.startswith(_CREDENTIAL_ERROR_PREFIXES)
    )


def extract_download(payload: Any) -> tuple[str, dict[str, str]] | None:
    """``(url, headers)`` from a ``/result`` answer, if it carries a link."""
    if not isinstance(payload, dict) or not isinstance(payload.get("download"), dict):
        return None
    download = payload["download"]
    url = download.get("url")
    if not isinstance(url, str) or not url.strip().lower().startswith(("https://", "http://")):
        return None
    headers = download.get("headers") if isinstance(download.get("headers"), dict) else {}
    return url.strip(), {str(key): str(value) for key, value in headers.items() if value is not None}
