from __future__ import annotations

import logging
import time
import uuid
from typing import Any
from urllib.parse import urljoin

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import database
from .config import settings
from .model_profiles import transform_create_payload


logger = logging.getLogger("uvicorn.error")
REQUEST_ID_HEADER = "X-Oneapi-Request-Id"


STATUS_MAP = {
    "NOT_START": "queued",
    "PENDING": "queued",
    "QUEUED": "queued",
    "IN_PROGRESS": "processing",
    "PROCESSING": "processing",
    "RUNNING": "processing",
    "SUCCESS": "completed",
    "SUCCEEDED": "completed",
    "COMPLETED": "completed",
    "FAILURE": "failed",
    "FAILED": "failed",
    "CANCELLED": "failed",
}


def normalize_status(value: Any) -> str:
    if not isinstance(value, str):
        return "queued"
    return STATUS_MAP.get(value.strip().upper(), value.strip().lower())


def upstream_error(
    response: httpx.Response,
    relay_request_id: str | None = None,
    phase: str | None = None,
    mark_failed: bool = False,
) -> JSONResponse:
    logger.warning("Video upstream returned HTTP %s: %s", response.status_code, response.text[:2000])
    payload = {
        "error": {
            "message": "Video upstream request failed",
            "type": "upstream_error",
            "code": f"upstream_http_{response.status_code}",
        }
    }
    if relay_request_id and phase:
        database.record_audit_event(relay_request_id, phase, response.status_code, response.text, payload)
        if mark_failed:
            database.fail_audit_request(relay_request_id, payload["error"]["message"])
    headers = {REQUEST_ID_HEADER: relay_request_id} if relay_request_id else None
    return JSONResponse(payload, status_code=response.status_code, headers=headers)


async def create_video(payload: dict[str, Any], incoming_idempotency_key: str | None) -> JSONResponse:
    model = str(payload.get("model", "")).strip()
    prompt = str(payload.get("prompt", "")).strip()
    if not model:
        raise HTTPException(status_code=400, detail="model is required")
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    upstream = database.select_upstream(model)
    if upstream is None:
        raise HTTPException(status_code=404, detail=f"No enabled upstream for model {model}")

    protocol = upstream["protocol"]
    relay_request_id = database.start_audit_request(upstream["id"], model, protocol, payload)
    upstream_payload = transform_create_payload(payload, upstream["profile"])
    response_headers = {REQUEST_ID_HEADER: relay_request_id}
    endpoint = "/v1/video/generations" if protocol == "seedance" else "/v1/videos"
    headers = {
        "Authorization": f"Bearer {upstream['api_key']}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if protocol == "seedance":
        explicit_key = str(payload.pop("idempotency_key", "")).strip()
        headers["Idempotency-Key"] = incoming_idempotency_key or explicit_key or str(uuid.uuid4())

    try:
        async with httpx.AsyncClient(timeout=settings.upstream_timeout_seconds) as client:
            response = await client.post(upstream["base_url"] + endpoint, headers=headers, json=upstream_payload)
    except httpx.RequestError as exc:
        logger.warning("Video upstream create request failed: %s", exc)
        sanitized = {"detail": "Video upstream connection failed"}
        database.record_audit_event(relay_request_id, "create", None, None, sanitized)
        database.fail_audit_request(relay_request_id, sanitized["detail"])
        raise HTTPException(status_code=502, detail=sanitized["detail"], headers=response_headers) from exc
    if not 200 <= response.status_code < 300:
        return upstream_error(response, relay_request_id, "create", mark_failed=True)

    try:
        upstream_payload = response.json()
    except ValueError as exc:
        sanitized = {"detail": "Upstream returned invalid JSON"}
        database.record_audit_event(relay_request_id, "create", response.status_code, response.text, sanitized)
        database.fail_audit_request(relay_request_id, sanitized["detail"])
        raise HTTPException(status_code=502, detail=sanitized["detail"], headers=response_headers) from exc
    task_id = str(upstream_payload.get("task_id") or upstream_payload.get("id") or "").strip()
    if not task_id:
        sanitized = {"detail": "Upstream response did not contain a task_id"}
        database.record_audit_event(relay_request_id, "create", response.status_code, response.text, sanitized)
        database.fail_audit_request(relay_request_id, sanitized["detail"])
        raise HTTPException(status_code=502, detail=sanitized["detail"], headers=response_headers)

    status = normalize_status(upstream_payload.get("status", "queued"))
    if status not in {"queued", "processing", "completed", "failed"}:
        status = "queued"
    try:
        progress = max(0, min(100, int(float(upstream_payload.get("progress") or 0))))
    except (TypeError, ValueError, OverflowError):
        progress = 0
    result = {
        "id": task_id,
        "object": "video",
        "model": model,
        "status": status,
        "progress": progress,
        "created_at": int(time.time()),
    }
    database.create_task(task_id, upstream["id"], relay_request_id, model, protocol, status)
    database.record_audit_event(relay_request_id, "create", response.status_code, response.text, result)
    return JSONResponse(result, headers=response_headers)


def normalize_task_payload(task: dict[str, Any], payload: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    status_value: Any = payload.get("status")
    video_url: str | None = payload.get("video_url")
    error_value: Any = payload.get("error")
    progress: Any = payload.get("progress")

    if task["protocol"] == "seedance":
        outer_data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        status_value = outer_data.get("status", status_value)
        job_data = outer_data.get("data") if isinstance(outer_data.get("data"), dict) else {}
        content = job_data.get("content") if isinstance(job_data.get("content"), dict) else {}
        video_url = content.get("video_url") or video_url
        error_value = outer_data.get("error") or job_data.get("error") or error_value

    status = normalize_status(status_value)
    if status not in {"queued", "processing", "completed", "failed"}:
        status = "queued"
    if progress is None:
        progress = 100 if status in {"completed", "failed"} else (30 if status == "processing" else 0)
    try:
        progress = max(0, min(100, int(float(progress))))
    except (TypeError, ValueError, OverflowError):
        progress = 0

    result = {
        "object": "video",
        "model": task["model"],
        "status": status,
        "progress": progress,
        "created_at": int(task["created_at"]),
        "updated_at": int(time.time()),
    }
    if error_value:
        result["error"] = {"message": "Video generation failed", "code": "video_generation_failed"}
    else:
        result["error"] = None
    return result, video_url


async def fetch_task(task_id: str) -> JSONResponse:
    task = database.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    headers = {"Authorization": f"Bearer {task['api_key']}", "Accept": "application/json"}
    relay_request_id = task.get("relay_request_id")
    response_headers = {REQUEST_ID_HEADER: relay_request_id} if relay_request_id else None
    try:
        async with httpx.AsyncClient(timeout=settings.upstream_timeout_seconds) as client:
            response = await client.get(f"{task['base_url']}/v1/videos/{task_id}", headers=headers)
    except httpx.RequestError as exc:
        logger.warning("Video upstream task request failed: %s", exc)
        sanitized = {"detail": "Video upstream connection failed"}
        if relay_request_id:
            database.record_audit_event(relay_request_id, "poll", None, None, sanitized)
        raise HTTPException(status_code=502, detail=sanitized["detail"], headers=response_headers) from exc
    if not 200 <= response.status_code < 300:
        return upstream_error(response, relay_request_id, "poll")
    try:
        payload = response.json()
    except ValueError as exc:
        sanitized = {"detail": "Upstream returned invalid JSON"}
        if relay_request_id:
            database.record_audit_event(relay_request_id, "poll", response.status_code, response.text, sanitized)
        raise HTTPException(status_code=502, detail=sanitized["detail"], headers=response_headers) from exc
    result, video_url = normalize_task_payload(task, payload)
    error = result["error"]["message"] if result.get("error") else None
    database.update_task(task_id, result["status"], video_url, error)
    if relay_request_id:
        database.record_audit_event(relay_request_id, "poll", response.status_code, response.text, result)
    return JSONResponse(result, headers=response_headers)


async def stream_content(task_id: str, request: Request) -> StreamingResponse:
    task = database.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    source_url = task.get("source_video_url")
    if not source_url:
        task_response = await fetch_task(task_id)
        import json

        status_payload = json.loads(bytes(task_response.body))
        refreshed_task = database.get_task(task_id)
        if refreshed_task is not None:
            task = refreshed_task
            source_url = task.get("source_video_url")
        if not source_url and status_payload.get("status") != "completed":
            raise HTTPException(status_code=409, detail="Video is not completed")

    if source_url:
        source_url = urljoin(task["base_url"] + "/", source_url)
    else:
        source_url = f"{task['base_url']}/v1/videos/{task_id}/content"

    headers = {"Authorization": f"Bearer {task['api_key']}"}
    if request.headers.get("range"):
        headers["Range"] = request.headers["range"]

    client = httpx.AsyncClient(timeout=None, follow_redirects=True)
    try:
        upstream_response = await client.send(client.build_request("GET", source_url, headers=headers), stream=True)
    except httpx.RequestError as exc:
        await client.aclose()
        logger.warning("Video upstream content request failed: %s", exc)
        raise HTTPException(status_code=502, detail="Video upstream download failed") from exc
    if upstream_response.status_code not in {200, 206}:
        body = await upstream_response.aread()
        await upstream_response.aclose()
        await client.aclose()
        logger.warning(
            "Video upstream content returned HTTP %s: %s",
            upstream_response.status_code,
            body[:2000].decode(errors="replace"),
        )
        raise HTTPException(
            status_code=502,
            detail="Video upstream download failed",
        )

    async def iterator():
        try:
            async for chunk in upstream_response.aiter_bytes(1024 * 256):
                yield chunk
        finally:
            await upstream_response.aclose()
            await client.aclose()

    response_headers = {}
    for key in ("content-length", "content-range", "accept-ranges", "content-disposition", "etag", "last-modified"):
        if key in upstream_response.headers:
            response_headers[key] = upstream_response.headers[key]
    return StreamingResponse(
        iterator(),
        status_code=upstream_response.status_code,
        media_type=upstream_response.headers.get("content-type", "video/mp4"),
        headers=response_headers,
    )
