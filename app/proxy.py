from __future__ import annotations

import time
import uuid
from typing import Any
from urllib.parse import urljoin

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import database
from .config import settings


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


def upstream_error(response: httpx.Response) -> JSONResponse:
    try:
        payload = response.json()
    except ValueError:
        payload = {
            "error": {
                "message": response.text[:2000] or f"Upstream returned HTTP {response.status_code}",
                "type": "upstream_error",
            }
        }
    return JSONResponse(payload, status_code=response.status_code)


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
            response = await client.post(upstream["base_url"] + endpoint, headers=headers, json=payload)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Upstream connection failed: {exc}") from exc
    if not 200 <= response.status_code < 300:
        return upstream_error(response)

    try:
        upstream_payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Upstream returned invalid JSON") from exc
    task_id = str(upstream_payload.get("task_id") or upstream_payload.get("id") or "").strip()
    if not task_id:
        raise HTTPException(status_code=502, detail="Upstream response did not contain a task_id")

    status = normalize_status(upstream_payload.get("status", "queued"))
    database.create_task(task_id, upstream["id"], model, protocol, status)
    result = dict(upstream_payload)
    result.update(
        {
            "id": task_id,
            "task_id": task_id,
            "object": result.get("object", "video"),
            "model": result.get("model", model),
            "status": status,
            "progress": int(result.get("progress") or 0),
            "created_at": int(result.get("created_at") or time.time()),
        }
    )
    return JSONResponse(result)


def normalize_task_payload(task: dict[str, Any], payload: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    status_value: Any = payload.get("status")
    video_url: str | None = payload.get("video_url")
    error_value: Any = payload.get("error")
    created_at = payload.get("created_at") or task["created_at"]
    updated_at = payload.get("updated_at") or int(time.time())
    progress: Any = payload.get("progress")

    if task["protocol"] == "seedance":
        outer_data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        status_value = outer_data.get("status", status_value)
        job_data = outer_data.get("data") if isinstance(outer_data.get("data"), dict) else {}
        content = job_data.get("content") if isinstance(job_data.get("content"), dict) else {}
        video_url = content.get("video_url") or video_url
        error_value = outer_data.get("error") or job_data.get("error") or error_value

    status = normalize_status(status_value)
    if progress is None:
        progress = 100 if status in {"completed", "failed"} else (30 if status == "processing" else 0)
    if isinstance(error_value, dict):
        error_message = str(error_value.get("message") or error_value.get("error") or "") or None
    elif error_value:
        error_message = str(error_value)
    else:
        error_message = None

    result = {
        "id": task["task_id"],
        "task_id": task["task_id"],
        "object": "video",
        "model": task["model"],
        "status": status,
        "progress": progress,
        "created_at": created_at,
        "updated_at": updated_at,
    }
    if video_url:
        result["video_url"] = video_url
    if error_message:
        result["error"] = {"message": error_message, "code": "upstream_task_failed"}
    else:
        result["error"] = None
    return result, video_url


async def fetch_task(task_id: str) -> JSONResponse:
    task = database.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    headers = {"Authorization": f"Bearer {task['api_key']}", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=settings.upstream_timeout_seconds) as client:
            response = await client.get(f"{task['base_url']}/v1/videos/{task_id}", headers=headers)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Upstream connection failed: {exc}") from exc
    if not 200 <= response.status_code < 300:
        return upstream_error(response)
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Upstream returned invalid JSON") from exc
    result, video_url = normalize_task_payload(task, payload)
    error = result["error"]["message"] if result.get("error") else None
    database.update_task(task_id, result["status"], video_url, error)
    return JSONResponse(result)


async def stream_content(task_id: str, request: Request) -> StreamingResponse:
    task = database.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    source_url = task.get("source_video_url")
    if not source_url:
        task_response = await fetch_task(task_id)
        task_payload = bytes(task_response.body)
        import json

        status_payload = json.loads(task_payload)
        source_url = status_payload.get("video_url")
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
        raise HTTPException(status_code=502, detail=f"Video download failed: {exc}") from exc
    if upstream_response.status_code not in {200, 206}:
        body = await upstream_response.aread()
        await upstream_response.aclose()
        await client.aclose()
        raise HTTPException(
            status_code=502,
            detail=f"Video upstream returned HTTP {upstream_response.status_code}: {body[:300].decode(errors='replace')}",
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

