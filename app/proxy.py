from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import ark_video, database
from .channels import autodl_comfyui, funai, o10_grok, pro666, rolldek
from .config import settings
from .model_profiles import transform_create_payload


logger = logging.getLogger("uvicorn.error")
REQUEST_ID_HEADER = "X-Oneapi-Request-Id"
MAX_UPSTREAM_ERROR_MESSAGE_LENGTH = 1000
# HTTP errors from the polling transport must not be confused with a terminal
# task result.  In particular, Cloudflare 52x responses describe a failure
# between Cloudflare and the origin, not a failed video-generation job.
PENDING_POLL_STATUS_CODES = {409, 429}


def should_preserve_task_on_poll_error(response: httpx.Response) -> bool:
    """Return whether a failed poll must leave the task pending.

    Every 5xx response is an infrastructure/transport failure and therefore
    cannot authoritatively describe the task's state.  This remains true when
    a Cloudflare envelope says ``retryable: false``: that flag means retrying
    will not repair the TLS/origin configuration, not that the video task
    itself failed.  Explicitly retryable non-5xx responses are also preserved.
    """
    if response.status_code >= 500 or response.status_code in PENDING_POLL_STATUS_CODES:
        return True
    try:
        payload = response.json()
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("retryable") is True


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
    "EXPIRED": "failed",
    "SUBMITTED": "queued",
}


def normalize_status(value: Any) -> str:
    if not isinstance(value, str):
        return "queued"
    return STATUS_MAP.get(value.strip().upper(), value.strip().lower())


def upstream_error_message(value: Any, fallback: str) -> str:
    candidate = _find_error_message(value)
    if candidate is None:
        return fallback
    message = " ".join(candidate.split())
    message = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [redacted]", message)
    message = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "sk-[redacted]", message)
    message = re.sub(r"https?://\S+", "[redacted URL]", message, flags=re.IGNORECASE)
    return message[:MAX_UPSTREAM_ERROR_MESSAGE_LENGTH] or fallback


def _find_error_message(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, dict):
        return None
    nested_error = value.get("error")
    if isinstance(nested_error, dict):
        message = _find_error_message(nested_error)
        if message:
            return message
    for key in ("message", "msg", "detail"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        if isinstance(candidate, dict):
            message = _find_error_message(candidate)
            if message:
                return message
    if isinstance(nested_error, str) and nested_error.strip():
        return nested_error.strip()
    return None


def upstream_error(
    response: httpx.Response,
    relay_request_id: str | None = None,
    phase: str | None = None,
    mark_failed: bool = False,
) -> JSONResponse:
    logger.warning("Video upstream returned HTTP %s: %s", response.status_code, response.text[:2000])
    try:
        upstream_payload = response.json()
    except ValueError:
        upstream_payload = None
    message = upstream_error_message(upstream_payload, "Video upstream request failed")
    payload = {
        "error": {
            "message": message,
            "type": "upstream_error",
            "code": f"upstream_http_{response.status_code}",
        }
    }
    if relay_request_id and phase:
        database.record_audit_event(relay_request_id, phase, response.status_code, response.text, payload)
        if mark_failed:
            database.fail_audit_request(relay_request_id, payload["error"]["message"])
    headers: dict[str, str] = {}
    if relay_request_id:
        headers[REQUEST_ID_HEADER] = relay_request_id
    # Preserve a provider/Cloudflare backoff hint for callers.  Some upstreams
    # send it as a standard header, while Cloudflare's 520 envelope uses a JSON
    # ``retry_after`` field instead.
    retry_after = response.headers.get("Retry-After")
    if not retry_after and isinstance(upstream_payload, dict):
        retry_after_value = upstream_payload.get("retry_after")
        if isinstance(retry_after_value, (int, float)) and retry_after_value > 0:
            retry_after = str(int(retry_after_value))
        elif isinstance(retry_after_value, str) and retry_after_value.strip().isdigit():
            retry_after = retry_after_value.strip()
    if retry_after:
        headers["Retry-After"] = retry_after
    return JSONResponse(payload, status_code=response.status_code, headers=headers or None)


async def create_video(
    payload: dict[str, Any],
    incoming_idempotency_key: str | None,
    public_task_id: str | None = None,
) -> JSONResponse:
    model = str(payload.get("model", "")).strip()
    prompt = str(payload.get("prompt", "")).strip()
    if not model:
        raise HTTPException(status_code=400, detail="model is required")

    upstream = database.select_upstream(model)
    if upstream is None:
        raise HTTPException(status_code=404, detail=f"No enabled upstream for model {model}")

    protocol = upstream["protocol"]
    allows_promptless = (
        protocol == ark_video.PROTOCOL and ark_video.has_reference_content(payload)
    ) or (
        protocol == autodl_comfyui.PROTOCOL
        and not autodl_comfyui.requires_prompt(upstream["upstream_model"])
    )
    if not prompt and not allows_promptless:
        raise HTTPException(status_code=400, detail="prompt is required")
    relay_request_id = database.start_audit_request(upstream["id"], model, protocol, payload)
    routed_payload = {**payload, "model": upstream["upstream_model"]}
    if not upstream.get("forward_resolution", True):
        routed_payload.pop("resolution", None)
        if isinstance(routed_payload.get("metadata"), dict):
            routed_payload["metadata"] = {
                key: value for key, value in routed_payload["metadata"].items() if key != "resolution"
            }
    if protocol == ark_video.PROTOCOL:
        upstream_payload = ark_video.transform_create_payload(routed_payload)
    elif protocol == funai.PROTOCOL:
        upstream_payload = funai.transform_create_payload(routed_payload, upstream["profile"])
    elif protocol == autodl_comfyui.PROTOCOL:
        upstream_payload = autodl_comfyui.transform_create_payload(routed_payload)
    elif protocol == o10_grok.PROTOCOL:
        upstream_payload = o10_grok.transform_create_payload(routed_payload)
    elif protocol == rolldek.PROTOCOL:
        upstream_payload = rolldek.transform_create_payload(routed_payload)
    else:
        upstream_payload = transform_create_payload(routed_payload, upstream["profile"])
    database.record_upstream_request_payload(relay_request_id, upstream_payload)
    response_headers = {REQUEST_ID_HEADER: relay_request_id}
    endpoint = (
        ark_video.CREATE_PATH
        if protocol == ark_video.PROTOCOL
        else funai.CREATE_PATH
        if protocol == funai.PROTOCOL
        else autodl_comfyui.create_path(upstream["upstream_model"])
        if protocol == autodl_comfyui.PROTOCOL
        else o10_grok.CREATE_PATH
        if protocol == o10_grok.PROTOCOL
        else rolldek.CREATE_PATH
        if protocol == rolldek.PROTOCOL
        else "/v1/video/generations" if protocol == "seedance" else "/v1/videos"
    )
    headers = (
        autodl_comfyui.auth_headers(upstream["api_key"])
        if protocol == autodl_comfyui.PROTOCOL
        else {
            "Authorization": f"Bearer {upstream['api_key']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    )
    if protocol == "seedance":
        explicit_key = str(payload.pop("idempotency_key", "")).strip()
        headers["Idempotency-Key"] = incoming_idempotency_key or explicit_key or str(uuid.uuid4())

    try:
        async with httpx.AsyncClient(timeout=settings.upstream_timeout_seconds) as client:
            url = (
                funai.api_url(upstream["base_url"], endpoint)
                if protocol == funai.PROTOCOL
                else upstream["base_url"] + endpoint
            )
            response = await client.post(url, headers=headers, json=upstream_payload)
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
    task_id = (
        funai.extract_create_task_id(upstream_payload)
        if protocol == funai.PROTOCOL
        else autodl_comfyui.extract_create_task_id(upstream_payload)
        if protocol == autodl_comfyui.PROTOCOL
        else o10_grok.extract_create_task_id(upstream_payload)
        if protocol == o10_grok.PROTOCOL
        else rolldek.extract_create_task_id(upstream_payload)
        if protocol == rolldek.PROTOCOL
        else str(upstream_payload.get("task_id") or upstream_payload.get("id") or "").strip()
    )
    if not task_id:
        sanitized = {
            "detail": upstream_error_message(
                upstream_payload,
                "Upstream response did not contain a task_id",
            )
        }
        database.record_audit_event(relay_request_id, "create", response.status_code, response.text, sanitized)
        database.fail_audit_request(relay_request_id, sanitized["detail"])
        raise HTTPException(status_code=502, detail=sanitized["detail"], headers=response_headers)

    create_status = (
        autodl_comfyui.extract_create_status(upstream_payload)
        if protocol == autodl_comfyui.PROTOCOL
        else upstream_payload.get("status")
    )
    if create_status is None and upstream_payload.get("error"):
        create_status = "failed"
    status = normalize_status(create_status or "queued")
    if status not in {"queued", "processing", "completed", "failed"}:
        status = "queued"
    progress_value = upstream_payload.get("progress")
    if progress_value is None:
        progress = 100 if status in {"completed", "failed"} else (30 if status == "processing" else 0)
    else:
        try:
            progress = max(0, min(100, int(float(progress_value))))
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
    if status == "failed":
        result["error"] = {
            "message": upstream_error_message(upstream_payload, "Video generation failed"),
            "code": "video_generation_failed",
        }
    database.create_task(task_id, upstream["id"], relay_request_id, model, protocol, status)
    if public_task_id:
        try:
            database.set_public_task_id(relay_request_id, public_task_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="Public task ID is already in use") from exc
    database.record_audit_event(relay_request_id, "create", response.status_code, response.text, result)
    return JSONResponse(result, headers=response_headers)


def normalize_task_payload(task: dict[str, Any], payload: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    status_value: Any = payload.get("status")
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    video_url: str | None = (
        payload.get("video_url")
        or payload.get("result_url")
        or metadata.get("url")
        or payload.get("download_url")
    )
    error_value: Any = payload.get("error")
    progress: Any = payload.get("progress")

    if task["protocol"] == "seedance":
        outer_data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        status_value = outer_data.get("status", status_value)
        job_data = outer_data.get("data") if isinstance(outer_data.get("data"), dict) else {}
        content = job_data.get("content") if isinstance(job_data.get("content"), dict) else {}
        video_url = content.get("video_url") or video_url
        error_value = outer_data.get("error") or job_data.get("error") or error_value
    elif task["protocol"] == ark_video.PROTOCOL:
        fields = ark_video.extract_task_fields(payload)
        status_value = fields["status"]
        video_url = fields["video_url"]
        error_value = fields["error"]
        progress = fields["progress"]
    elif task["protocol"] == funai.PROTOCOL:
        fields = funai.extract_task_fields(payload)
        status_value = fields["status"]
        video_url = fields["video_url"]
        error_value = fields["error"]
        progress = fields["progress"]
    elif task["protocol"] == autodl_comfyui.PROTOCOL:
        fields = autodl_comfyui.extract_task_fields(payload)
        status_value = fields["status"]
        video_url = fields["video_url"]
        error_value = fields["error"]
        progress = fields["progress"]
    elif task["protocol"] == o10_grok.PROTOCOL:
        fields = o10_grok.extract_task_fields(payload)
        status_value = fields["status"]
        video_url = fields["video_url"]
        error_value = fields["error"]
        progress = fields["progress"]
    elif task["protocol"] == rolldek.PROTOCOL:
        fields = rolldek.extract_task_fields(payload)
        status_value = fields["status"]
        video_url = fields["video_url"]
        error_value = fields["error"]
        progress = fields["progress"]

    if status_value is None and error_value:
        status_value = "failed"
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
    if status == "failed":
        result["error"] = {
            "message": upstream_error_message(error_value or payload, "Video generation failed"),
            "code": "video_generation_failed",
        }
    else:
        result["error"] = None
    return result, video_url


async def fetch_task(task_id: str, timeout_seconds: float | None = None) -> JSONResponse:
    task = database.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    database.touch_task(task_id)
    headers = (
        autodl_comfyui.auth_headers(task["api_key"])
        if task["protocol"] == autodl_comfyui.PROTOCOL
        else {"Authorization": f"Bearer {task['api_key']}", "Accept": "application/json"}
    )
    relay_request_id = task.get("relay_request_id")
    response_headers = {REQUEST_ID_HEADER: relay_request_id} if relay_request_id else None
    endpoint = (
        ark_video.task_path(task_id)
        if task["protocol"] == ark_video.PROTOCOL
        else funai.task_path(task_id)
        if task["protocol"] == funai.PROTOCOL
        else autodl_comfyui.task_path(task_id)
        if task["protocol"] == autodl_comfyui.PROTOCOL
        else o10_grok.task_path(task_id)
        if task["protocol"] == o10_grok.PROTOCOL
        else rolldek.task_path(task_id)
        if task["protocol"] == rolldek.PROTOCOL
        else f"/v1/videos/{task_id}"
    )
    try:
        request_timeout = settings.upstream_timeout_seconds if timeout_seconds is None else timeout_seconds
        async with httpx.AsyncClient(timeout=request_timeout) as client:
            url = (
                funai.api_url(task["base_url"], endpoint)
                if task["protocol"] == funai.PROTOCOL
                else f"{task['base_url']}{endpoint}"
            )
            response = await client.get(url, headers=headers)
    except httpx.RequestError as exc:
        logger.warning("Video upstream task request failed: %s", exc)
        sanitized = {"detail": "Video upstream connection failed"}
        if relay_request_id:
            database.record_audit_event(relay_request_id, "poll", None, None, sanitized)
        raise HTTPException(status_code=502, detail=sanitized["detail"], headers=response_headers) from exc
    if not 200 <= response.status_code < 300:
        error_response = upstream_error(response, relay_request_id, "poll")
        if not should_preserve_task_on_poll_error(response):
            try:
                error_payload = response.json()
            except ValueError:
                error_payload = None
            error = upstream_error_message(error_payload, "Video generation failed")
            database.update_task(task_id, "failed", None, error)
        return error_response
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
    if result["status"] == "completed" and task.get("public_task_id"):
        result["video_url"] = database.public_video_url(task["public_task_id"])
    if relay_request_id:
        database.record_audit_event(relay_request_id, "poll", response.status_code, response.text, result)
    return JSONResponse(result, headers=response_headers)


async def reconcile_pending_tasks(limit: int = 8, stale_seconds: int = 3) -> None:
    task_ids = database.list_pending_task_ids(int(time.time()) - max(0, stale_seconds), limit)
    if not task_ids:
        return

    semaphore = asyncio.Semaphore(8)

    async def refresh(task_id: str) -> None:
        async with semaphore:
            try:
                await fetch_task(task_id, timeout_seconds=min(5.0, settings.upstream_timeout_seconds))
            except HTTPException:
                # Connection and malformed-response errors are recorded by fetch_task and retried later.
                pass

    await asyncio.gather(*(refresh(task_id) for task_id in task_ids))


async def stream_upstream_content(
    source_url: str,
    request: Request,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
    default_media_type: str = "application/octet-stream",
    error_message: str = "Upstream download failed",
    source_url_validator: Callable[[str], bool] | None = None,
) -> StreamingResponse:
    request_headers = dict(headers or {})
    if request.headers.get("range"):
        request_headers["Range"] = request.headers["range"]
    if source_url_validator is not None and not source_url_validator(source_url):
        raise HTTPException(status_code=502, detail=error_message)
    client = httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=source_url_validator is None,
    )
    current_url = source_url
    for _ in range(6):
        try:
            upstream_response = await client.send(
                client.build_request("GET", current_url, headers=request_headers), stream=True
            )
        except httpx.RequestError as exc:
            await client.aclose()
            logger.warning("%s: %s", error_message, exc)
            raise HTTPException(status_code=502, detail=error_message) from exc
        if source_url_validator is None or not upstream_response.is_redirect:
            break
        redirect_url = urljoin(current_url, upstream_response.headers.get("location", ""))
        await upstream_response.aclose()
        if not upstream_response.headers.get("location") or not source_url_validator(redirect_url):
            await client.aclose()
            raise HTTPException(status_code=502, detail=error_message)
        current_url = redirect_url
    else:
        await client.aclose()
        raise HTTPException(status_code=502, detail=error_message)
    if upstream_response.status_code not in {200, 206}:
        await upstream_response.aclose()
        await client.aclose()
        logger.warning("%s: upstream returned HTTP %s", error_message, upstream_response.status_code)
        raise HTTPException(status_code=502, detail=error_message)

    async def iterator():
        try:
            async for chunk in upstream_response.aiter_bytes(1024 * 256):
                yield chunk
        finally:
            await upstream_response.aclose()
            await client.aclose()

    response_headers = {}
    for key in (
        "content-length",
        "content-range",
        "accept-ranges",
        "content-disposition",
        "etag",
        "last-modified",
        "cache-control",
    ):
        if key in upstream_response.headers:
            response_headers[key] = upstream_response.headers[key]
    return StreamingResponse(
        iterator(),
        status_code=upstream_response.status_code,
        media_type=upstream_response.headers.get("content-type", default_media_type),
        headers=response_headers,
    )


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
    elif task["protocol"] in {ark_video.PROTOCOL, autodl_comfyui.PROTOCOL}:
        provider = "Ark" if task["protocol"] == ark_video.PROTOCOL else "AutoDL"
        raise HTTPException(status_code=502, detail=f"{provider} task completed without a video URL")
    elif task["protocol"] == o10_grok.PROTOCOL:
        source_url = f"{task['base_url']}{o10_grok.content_path(task_id)}"
    elif task["protocol"] == funai.PROTOCOL:
        source_url = funai.api_url(task["base_url"], funai.content_path(task_id))
    elif task["protocol"] == rolldek.PROTOCOL:
        source_url = f"{task['base_url']}{rolldek.content_path(task_id)}"
    else:
        source_url = f"{task['base_url']}/v1/videos/{task_id}/content"

    request_headers = {}
    if same_origin(source_url, task["base_url"]) or pro666.permits_api_key_forwarding(
        source_url, task["base_url"]
    ):
        request_headers["Authorization"] = (
            task["api_key"]
            if task["protocol"] == autodl_comfyui.PROTOCOL
            else f"Bearer {task['api_key']}"
        )

    return await stream_upstream_content(
        source_url,
        request,
        headers=request_headers,
        default_media_type="video/mp4",
        error_message="Video upstream download failed",
    )


def same_origin(left_url: str, right_url: str) -> bool:
    try:
        left = urlsplit(left_url)
        right = urlsplit(right_url)
        left_port = left.port or (443 if left.scheme.lower() == "https" else 80)
        right_port = right.port or (443 if right.scheme.lower() == "https" else 80)
    except ValueError:
        return False
    return (
        left.scheme.lower(),
        (left.hostname or "").lower(),
        left_port,
    ) == (
        right.scheme.lower(),
        (right.hostname or "").lower(),
        right_port,
    )
