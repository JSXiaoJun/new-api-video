from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import ark_video, database, media_probe
from .channels import autodl_comfyui, funai, fuyao, mai_token, o10_grok, pro666, rolldek, sub2api_video
from .config import settings
from .model_profiles import (
    INLINE_VALUE_KEYS,
    MEDIA_FIELD_NAMES,
    MEDIA_FIELD_PREFIXES,
    URL_VALUE_KEYS,
    MediaLimitError,
    ResolutionMismatchError,
    apply_pinned_resolution,
    enforce_reference_media_limits,
    iter_media_references,
    route_media_counts,
    transform_create_payload,
)


logger = logging.getLogger("uvicorn.error")
REQUEST_ID_HEADER = "X-Oneapi-Request-Id"
MAX_UPSTREAM_ERROR_MESSAGE_LENGTH = 1000
# HTTP errors from the polling transport must not be confused with a terminal
# task result.  In particular, Cloudflare 52x responses describe a failure
# between Cloudflare and the origin, not a failed video-generation job.
PENDING_POLL_STATUS_CODES = {409, 429}

_IMAGE_FIELD_NAMES = MEDIA_FIELD_NAMES["image"]
_IMAGE_URL_KEYS = URL_VALUE_KEYS
_INLINE_IMAGE_KEYS = INLINE_VALUE_KEYS


def validate_online_image_inputs(payload: dict[str, Any]) -> None:
    """Reject inline/base64 image data before it reaches a video upstream.

    Video providers need to download reference media themselves, so image
    fields must contain publicly reachable HTTP(S) URLs. The walker accepts
    URL arrays and common OpenAI-style ``image_url: {url: ...}`` objects.
    """
    for value in _iter_image_candidates(payload):
        if not isinstance(value, str) or not value.strip():
            continue
        candidate = value.strip()
        parsed = urlsplit(candidate)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise HTTPException(
                status_code=400,
                detail=(
                    "图片必须传入在线链接（http:// 或 https:// URL），不支持 Base64；"
                    "请先将图片上传到可公开访问的地址后再提交。"
                ),
            )


def _iter_image_candidates(payload: Any):
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in _IMAGE_FIELD_NAMES or normalized.startswith(MEDIA_FIELD_PREFIXES["image"]):
                yield from _iter_image_field_values(value)
            elif isinstance(value, (dict, list)):
                yield from _iter_image_candidates(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_image_candidates(item)


def _iter_image_field_values(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _iter_image_field_values(item)
    elif isinstance(value, dict):
        # OpenAI-style image objects put the actual URL below ``url`` or
        # ``image_url``; ignore metadata such as ``type``.
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in _IMAGE_URL_KEYS:
                yield from _iter_image_field_values(child)
            elif normalized in _INLINE_IMAGE_KEYS:
                yield from _iter_image_field_values(child)
            elif normalized in _IMAGE_FIELD_NAMES or normalized.startswith(MEDIA_FIELD_PREFIXES["image"]):
                yield from _iter_image_field_values(child)


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


# Background measurements in flight, by task id. Keeping a reference stops the
# event loop from collecting a running task.
_reference_video_measurements: dict[str, asyncio.Task] = {}


def reference_video_usage(task: dict[str, Any]) -> dict[str, Any] | None:
    """Usage New API bills reference videos from, once every video is measured.

    ``reference_video_seconds`` holds one entry per video; ``null`` marks a
    video whose length could not be read, which New API bills at its reserved
    cap. Absent on routes that do not measure.
    """
    videos = task.get("reference_videos")
    if not isinstance(videos, dict):
        return None
    seconds = videos.get("seconds")
    if not isinstance(seconds, list):
        return None
    measured = [value for value in seconds if isinstance(value, (int, float))]
    return {
        "input_video_seconds": round(sum(measured), 3),
        "reference_video_seconds": seconds,
    }


def reference_videos_pending(task: dict[str, Any]) -> bool:
    videos = task.get("reference_videos")
    return isinstance(videos, dict) and not isinstance(videos.get("seconds"), list)


async def _measure_task_reference_videos(task_id: str, urls: list[str]) -> None:
    try:
        seconds = await media_probe.measure_reference_videos(urls)
    except Exception:
        logger.exception("Reference video measurement failed for %s", task_id)
        seconds = [None] * len(urls)
    database.set_reference_video_seconds(task_id, seconds)


def start_reference_video_measurement(task_id: str, urls: list[str]) -> asyncio.Task | None:
    """Measure in the background; a running measurement is not started twice."""
    running = _reference_video_measurements.get(task_id)
    if running is not None and not running.done():
        return running
    job = asyncio.get_running_loop().create_task(_measure_task_reference_videos(task_id, urls))
    _reference_video_measurements[task_id] = job
    job.add_done_callback(lambda _job: _reference_video_measurements.pop(task_id, None))
    return job


# A status read is a GET and changes nothing upstream, so it can be retried
# freely. Fuyao's task endpoint answers most reads within a few seconds but
# leaves some hanging indefinitely; a short limit per attempt plus a retry
# reaches a good answer sooner than one long wait.
POLL_ATTEMPTS = 3
POLL_ATTEMPT_TIMEOUT_SECONDS = 25.0
POLL_RETRY_DELAY_SECONDS = 1.0
POLL_UNREACHABLE_DETAIL = "上游查询无响应，任务保持原状态，稍后自动重试"
CONTENT_CONNECT_TIMEOUT_SECONDS = 15.0
CONTENT_FIRST_BYTE_TIMEOUT_SECONDS = 30.0


async def _get_with_retries(
    url: str, headers: dict[str, str], attempt_timeout: float, attempts: int
) -> tuple[httpx.Response | None, httpx.RequestError | None]:
    error: httpx.RequestError | None = None
    for attempt in range(attempts):
        try:
            async with httpx.AsyncClient(timeout=attempt_timeout) as client:
                return await client.get(url, headers=headers), None
        except httpx.RequestError as exc:
            error = exc
        if attempt + 1 < attempts:
            await asyncio.sleep(POLL_RETRY_DELAY_SECONDS)
    return None, error


def pending_task_response(task: dict[str, Any], response: httpx.Response | None = None) -> JSONResponse:
    """Report a task as unchanged when its upstream status could not be read."""
    if task.get("status") in {"completed", "failed"} and response is None:
        # A settled task keeps its settled answer.
        result: dict[str, Any] = {
            "object": "video",
            "model": task["model"],
            "status": task["status"],
            "progress": 100,
            "created_at": int(task["created_at"]),
            "updated_at": int(time.time()),
            "error": (
                {"message": task.get("error") or "Video generation failed", "code": "video_generation_failed"}
                if task["status"] == "failed"
                else None
            ),
        }
        if task["status"] == "completed" and task.get("public_task_id"):
            result["video_url"] = database.public_video_url(task["public_task_id"])
        usage = reference_video_usage(task)
        if usage:
            result["usage"] = usage
        relay_request_id = task.get("relay_request_id")
        return JSONResponse(result, headers={REQUEST_ID_HEADER: relay_request_id} if relay_request_id else None)
    return pending_poll_response(task, response)


def pending_poll_response(task: dict[str, Any], response: httpx.Response | None) -> JSONResponse:
    """Return a successful, still-pending response for transport failures.

    Polling is part of one long-running generation request from New API's
    perspective.  Propagating a Cloudflare 52x status here makes New API treat
    the generation as rejected and issue a refund, even when the provider job
    later completes.  Keep the task pending and let the next poll retry.
    """
    status = task.get("status") if task.get("status") in {"queued", "processing"} else "processing"
    progress = 30 if status == "processing" else 0
    payload: dict[str, Any] = {
        "object": "video",
        "model": task["model"],
        "status": status,
        "progress": progress,
        "created_at": int(task["created_at"]),
        "updated_at": int(time.time()),
        "error": None,
    }
    usage = reference_video_usage(task)
    if usage:
        payload["usage"] = usage
    retry_after = response.headers.get("Retry-After") if response is not None else None
    if not retry_after and response is not None:
        try:
            upstream_payload = response.json()
        except ValueError:
            upstream_payload = None
        if isinstance(upstream_payload, dict):
            value = upstream_payload.get("retry_after")
            if isinstance(value, (int, float)) and value > 0:
                retry_after = str(int(value))
            elif isinstance(value, str) and value.strip().isdigit():
                retry_after = value.strip()
    if retry_after:
        payload["retry_after"] = int(retry_after) if retry_after.isdigit() else retry_after
    relay_request_id = task.get("relay_request_id")
    headers: dict[str, str] = {}
    if relay_request_id:
        headers[REQUEST_ID_HEADER] = relay_request_id
    if retry_after:
        headers["Retry-After"] = retry_after
    return JSONResponse(payload, status_code=200, headers=headers or None)


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


IDEMPOTENT_CREATE_PROTOCOLS = frozenset({"videos", "seedance", fuyao.PROTOCOL})
# A gateway in front of the upstream lost the origin's reply after the request
# reached it, so the job may exist.
UNCONFIRMED_CREATE_STATUS_CODES = frozenset({502, 504, 520, 524})
# Raised before a single byte of the request reached the upstream, so nothing
# can have been created or billed and retrying is free.
CREATE_NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol)
CREATE_CONNECT_ATTEMPTS = 3
CREATE_CONNECT_RETRY_DELAY_SECONDS = 2.0
CREATE_CONNECT_TIMEOUT_SECONDS = 10.0
CREATE_LEASE_GRACE_SECONDS = 60
CREATE_CONNECT_FAILED_DETAIL = "Video upstream connection failed"
CREATE_UNCONFIRMED_DETAIL = "上游未确认是否已创建任务，待人工核对，请勿重复提交"
CREATE_EXPIRED_DETAIL = "上游始终未确认是否创建了任务，已按失败处理"
CREATE_OPERATOR_FAILED_DETAIL = "管理员已确认上游未创建任务"

# In-process create submissions, keyed by relay request ID. Holding the task
# here keeps it alive after the caller stopped waiting for it.
_create_jobs: dict[str, asyncio.Task] = {}


def _create_headers(protocol: str, api_key: str) -> dict[str, str]:
    if protocol == autodl_comfyui.PROTOCOL:
        return autodl_comfyui.auth_headers(api_key)
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _create_timeout() -> httpx.Timeout:
    # A slow create is normal (some upstreams validate every reference file
    # before answering), so only connecting is held to a short limit.
    return httpx.Timeout(
        settings.video_create_timeout_seconds,
        connect=min(CREATE_CONNECT_TIMEOUT_SECONDS, settings.video_create_timeout_seconds),
    )


def _extract_create_task_id(protocol: str, payload: dict[str, Any]) -> str:
    if protocol == funai.PROTOCOL:
        return funai.extract_create_task_id(payload)
    if protocol == autodl_comfyui.PROTOCOL:
        return autodl_comfyui.extract_create_task_id(payload)
    if protocol == o10_grok.PROTOCOL:
        return o10_grok.extract_create_task_id(payload)
    if protocol == sub2api_video.PROTOCOL:
        return sub2api_video.extract_create_task_id(payload)
    if protocol == mai_token.PROTOCOL:
        return mai_token.extract_create_task_id(payload)
    if protocol == rolldek.PROTOCOL:
        return rolldek.extract_create_task_id(payload)
    if protocol == fuyao.PROTOCOL:
        return fuyao.extract_create_task_id(payload)
    return str(payload.get("task_id") or payload.get("id") or "").strip()


def provisional_create_response(relay_request_id: str, model: str, created_at: int) -> JSONResponse:
    """Answer a create the upstream has not confirmed yet.

    The relay request ID stands in for the upstream task ID; polling it
    resolves to the real task once it is known. Reporting a failure here
    instead would refund the caller for a job the upstream may have accepted
    and billed.
    """
    return JSONResponse(
        {
            "id": relay_request_id,
            "object": "video",
            "model": model,
            "status": "queued",
            "progress": 0,
            "created_at": created_at,
        },
        headers={REQUEST_ID_HEADER: relay_request_id},
    )


def _start_create_job(submission: dict[str, Any], api_key: str) -> asyncio.Task:
    relay_request_id = submission["relay_request_id"]
    job = asyncio.create_task(_submit_create(submission, api_key))
    _create_jobs[relay_request_id] = job
    job.add_done_callback(lambda done: _forget_create_job(relay_request_id, done))
    return job


def _forget_create_job(relay_request_id: str, job: asyncio.Task) -> None:
    if _create_jobs.get(relay_request_id) is job:
        del _create_jobs[relay_request_id]
    if job.cancelled():
        return
    error = job.exception()
    if error is not None and not isinstance(error, HTTPException):
        logger.error("Video create submission %s crashed", relay_request_id, exc_info=error)


async def _post_create(
    url: str, headers: dict[str, str], payload: Any
) -> tuple[str, httpx.Response | None, httpx.RequestError | None]:
    """POST once and say what is known: ``not_sent``, ``unconfirmed`` or ``answered``."""
    try:
        async with httpx.AsyncClient(timeout=_create_timeout()) as client:
            response = await client.post(url, headers=headers, json=payload)
    except CREATE_NOT_SENT_ERRORS as exc:
        return "not_sent", None, exc
    except httpx.RequestError as exc:
        # Timeouts and dropped connections after the body went out: the
        # upstream may well have created (and billed) the job.
        return "unconfirmed", None, exc
    if response.status_code in UNCONFIRMED_CREATE_STATUS_CODES:
        return "unconfirmed", response, None
    if 200 <= response.status_code < 300:
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            # Accepted, but the task ID cannot be read from the answer.
            return "unconfirmed", response, None
    return "answered", response, None


def _error_text(error: httpx.RequestError) -> str:
    message = str(error).strip()
    return f"{type(error).__name__}: {message}" if message else type(error).__name__


def _record_unconfirmed_attempt(
    relay_request_id: str, response: httpx.Response | None, error: httpx.RequestError | None
) -> None:
    if error is not None:
        logger.warning("Video upstream create %s unconfirmed: %r", relay_request_id, error)
        upstream_body = _error_text(error)
    else:
        logger.warning(
            "Video upstream create %s unconfirmed: HTTP %s", relay_request_id, response.status_code
        )
        upstream_body = response.text
    database.record_audit_event(
        relay_request_id,
        "create",
        response.status_code if response is not None else None,
        upstream_body,
        {"detail": CREATE_UNCONFIRMED_DETAIL, "unconfirmed": True},
    )


async def _submit_create(submission: dict[str, Any], api_key: str) -> JSONResponse:
    """Send a create, retrying only while nothing has reached the upstream.

    Once the request body may have arrived, it is never sent again: resending
    can start a second billed job. An unknown outcome is parked as
    ``unknown`` for an operator to bind the upstream task or fail it.
    """
    relay_request_id = submission["relay_request_id"]
    response_headers = {REQUEST_ID_HEADER: relay_request_id}
    headers = _create_headers(submission["protocol"], api_key)
    if submission["idempotency_key"]:
        headers["Idempotency-Key"] = submission["idempotency_key"]
    for attempt in range(1, CREATE_CONNECT_ATTEMPTS + 1):
        database.record_create_attempt(relay_request_id)
        outcome, response, error = await _post_create(
            submission["create_url"], headers, submission["upstream_request_payload"]
        )
        if outcome == "answered":
            return _finish_create(submission, response, response_headers)
        if outcome == "unconfirmed":
            _record_unconfirmed_attempt(relay_request_id, response, error)
            database.mark_create_unknown(relay_request_id, CREATE_UNCONFIRMED_DETAIL)
            return provisional_create_response(relay_request_id, submission["model"], int(time.time()))
        if attempt < CREATE_CONNECT_ATTEMPTS:
            await asyncio.sleep(CREATE_CONNECT_RETRY_DELAY_SECONDS)
    logger.warning("Video upstream create request failed: %s", error)
    sanitized = {"detail": CREATE_CONNECT_FAILED_DETAIL}
    database.record_audit_event(relay_request_id, "create", None, _error_text(error), sanitized)
    database.fail_audit_request(relay_request_id, sanitized["detail"])
    raise HTTPException(status_code=502, detail=sanitized["detail"], headers=response_headers) from error


def recover_create(relay_request_id: str) -> None:
    """Park or expire an unconfirmed create. Never sends anything upstream."""
    if relay_request_id in _create_jobs:
        return
    submission = database.get_create_submission(relay_request_id)
    if (
        submission is None
        or not submission["create_state"]
        or submission["upstream_task_id"]
        or submission["status"] != "queued"
    ):
        return
    now = int(time.time())
    state = submission["create_state"]
    started_at = int(submission["create_started_at"] or 0)
    if state == "submitting":
        if now - started_at < settings.video_create_timeout_seconds + CREATE_LEASE_GRACE_SECONDS:
            # The POST may still be in flight in this or another worker.
            return
        # Its worker vanished (restart) after the request may have gone out.
        database.mark_create_unknown(relay_request_id, CREATE_UNCONFIRMED_DETAIL)
        database.record_audit_event(
            relay_request_id, "create", None, "submission interrupted",
            {"detail": CREATE_UNCONFIRMED_DETAIL, "unconfirmed": True},
        )
    if now - int(submission["created_at"]) >= settings.video_create_unconfirmed_ttl_seconds:
        if database.fail_unconfirmed_create(relay_request_id, CREATE_EXPIRED_DETAIL):
            database.record_audit_event(relay_request_id, "create", None, None, {"detail": CREATE_EXPIRED_DETAIL})


def recover_pending_creates(limit: int = 20) -> None:
    now = int(time.time())
    lease_expired_before = now - int(settings.video_create_timeout_seconds) - CREATE_LEASE_GRACE_SECONDS
    created_before = now - settings.video_create_unconfirmed_ttl_seconds
    for relay_request_id in database.list_pending_create_ids(lease_expired_before, created_before, limit):
        try:
            recover_create(relay_request_id)
        except Exception:
            logger.exception("Video create recovery failed for %s", relay_request_id)


def bind_upstream_task(relay_request_id: str, upstream_task_id: str) -> None:
    if relay_request_id in _create_jobs:
        raise ValueError("create_in_flight")
    database.bind_upstream_task(relay_request_id, upstream_task_id)


def fail_unconfirmed_create(relay_request_id: str) -> None:
    if relay_request_id in _create_jobs:
        raise ValueError("create_in_flight")
    if not database.fail_unconfirmed_create(relay_request_id, CREATE_OPERATOR_FAILED_DETAIL):
        raise ValueError("create_not_unconfirmed")
    database.record_audit_event(
        relay_request_id, "create", None, None, {"detail": CREATE_OPERATOR_FAILED_DETAIL}
    )


def resolve_task_id(task_id: str) -> str:
    """Map a provisional (relay request) ID onto the upstream task it became."""
    if task_id.startswith("vrq_") and database.get_task(task_id) is None:
        submission = database.get_create_submission(task_id)
        if submission is not None and submission["upstream_task_id"]:
            return str(submission["upstream_task_id"])
    return task_id


def _pending_create_status(submission: dict[str, Any]) -> JSONResponse:
    relay_request_id = submission["relay_request_id"]
    result: dict[str, Any] = {
        "object": "video",
        "model": submission["model"],
        "status": "queued",
        "progress": 0,
        "created_at": int(submission["created_at"]),
        "updated_at": int(time.time()),
        "error": None,
    }
    if submission["status"] == "failed":
        result.update(
            status="failed",
            progress=100,
            error={
                "message": submission["error"] or "Video generation failed",
                "code": "video_generation_failed",
            },
        )
    return JSONResponse(result, headers={REQUEST_ID_HEADER: relay_request_id})


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

    validate_online_image_inputs(payload)

    protocol = upstream["protocol"]
    # 参考媒体的数量上限在这里统一执行：超过后台配置的数量直接拒绝，并说明是哪
    # 一种媒体超了多少，绝不悄悄丢掉参考媒体让用户拿到另一个任务的结果。数量与
    # ``/v1/model-capabilities`` 公布的数字同源，调用方可以提前知道预算。
    media_counts = route_media_counts(upstream)
    try:
        enforce_reference_media_limits(payload, media_counts)
    except MediaLimitError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    video_limit = media_counts["video"]
    routed_payload = {**payload, "model": upstream["upstream_model"]}
    # A route whose public name promises a resolution (``wan3.0-video-720p``
    # split off ``wan3.0-video``) must produce it. Routes that do not forward
    # resolution leave it to the model, so nothing is pinned there.
    if upstream.get("forward_resolution", True):
        try:
            routed_payload = apply_pinned_resolution(
                routed_payload, upstream.get("resolutions"), model, upstream["upstream_model"]
            )
        except ResolutionMismatchError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    allows_promptless = (
        protocol == ark_video.PROTOCOL and ark_video.has_reference_content(routed_payload)
    ) or (
        protocol == autodl_comfyui.PROTOCOL
        and not autodl_comfyui.requires_prompt(upstream["upstream_model"])
    )
    if not prompt and not allows_promptless:
        raise HTTPException(status_code=400, detail="prompt is required")
    reference_video_urls: list[str] | None = None
    if upstream.get("measure_reference_video"):
        # Measured after the upstream accepts the task, so submission never
        # waits on the user's file host. New API reserves the cap meanwhile.
        reference_video_urls = list(dict.fromkeys(
            value.strip() for value in iter_media_references(payload, "video") if value.strip()
        ))
    relay_request_id = database.start_audit_request(upstream["id"], model, protocol, payload)
    if not upstream.get("forward_resolution", True):
        routed_payload.pop("resolution", None)
        if isinstance(routed_payload.get("metadata"), dict):
            metadata = {
                key: value for key, value in routed_payload["metadata"].items() if key != "resolution"
            }
            if isinstance(metadata.get("parameters"), dict):
                metadata["parameters"] = {
                    key: value for key, value in metadata["parameters"].items() if key != "resolution"
                }
            routed_payload["metadata"] = metadata
    if protocol == ark_video.PROTOCOL:
        upstream_payload = ark_video.transform_create_payload(routed_payload)
    elif protocol == funai.PROTOCOL:
        upstream_payload = funai.transform_create_payload(routed_payload, upstream["profile"])
    elif protocol == autodl_comfyui.PROTOCOL:
        upstream_payload = autodl_comfyui.transform_create_payload(routed_payload)
    elif protocol == o10_grok.PROTOCOL:
        upstream_payload = o10_grok.transform_create_payload(routed_payload)
    elif protocol == sub2api_video.PROTOCOL:
        upstream_payload = sub2api_video.transform_create_payload(routed_payload)
    elif protocol == mai_token.PROTOCOL:
        upstream_payload = mai_token.transform_create_payload(routed_payload)
    elif protocol == rolldek.PROTOCOL:
        upstream_payload = rolldek.transform_create_payload(routed_payload)
    elif protocol == fuyao.PROTOCOL:
        try:
            upstream_payload = fuyao.transform_create_payload(routed_payload, upstream["profile"])
        except fuyao.FuyaoRequestError as error:
            # The model family cannot serve this request; say why instead of
            # sending a trimmed task the caller did not ask for.
            database.fail_audit_request(relay_request_id, str(error))
            raise HTTPException(
                status_code=400,
                detail=str(error),
                headers={REQUEST_ID_HEADER: relay_request_id},
            ) from error
    else:
        upstream_payload = transform_create_payload(
            routed_payload, upstream["profile"], video_limit
        )
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
        else sub2api_video.CREATE_PATH
        if protocol == sub2api_video.PROTOCOL
        else mai_token.CREATE_PATH
        if protocol == mai_token.PROTOCOL
        else rolldek.CREATE_PATH
        if protocol == rolldek.PROTOCOL
        else fuyao.CREATE_PATH
        if protocol == fuyao.PROTOCOL
        else "/v1/video/generations" if protocol == "seedance" else "/v1/videos"
    )
    headers = _create_headers(protocol, upstream["api_key"])
    # One key per generation, so a retry by the caller with the same key can
    # be matched to the original task by the upstream. Fuyao asks for a fresh
    # key per generation.
    if protocol in IDEMPOTENT_CREATE_PROTOCOLS:
        explicit_key = str(payload.get("idempotency_key", "")).strip()
        headers["Idempotency-Key"] = incoming_idempotency_key or explicit_key or str(
            uuid.uuid5(uuid.NAMESPACE_URL, relay_request_id)
        )

    url = (
        funai.api_url(upstream["base_url"], endpoint)
        if protocol == funai.PROTOCOL
        else upstream["base_url"] + endpoint
    )
    if public_task_id:
        try:
            database.set_public_task_id(relay_request_id, public_task_id)
        except ValueError as exc:
            database.fail_audit_request(relay_request_id, "Public task ID is already in use")
            raise HTTPException(
                status_code=409, detail="Public task ID is already in use", headers=response_headers
            ) from exc
    database.begin_create_submission(relay_request_id, url, headers.get("Idempotency-Key"))
    submission = {
        "relay_request_id": relay_request_id,
        "upstream_id": upstream["id"],
        "model": model,
        "protocol": protocol,
        "create_url": url,
        "idempotency_key": headers.get("Idempotency-Key"),
        "upstream_request_payload": upstream_payload,
        "reference_video_urls": reference_video_urls,
    }
    job = _start_create_job(submission, upstream["api_key"])
    try:
        # The submission is a separate task, so a caller that disconnects or a
        # slow upstream never cancels it; past the wait budget the caller gets
        # a provisional task ID and the submission finishes in the background.
        return await asyncio.wait_for(asyncio.shield(job), timeout=settings.video_create_wait_seconds)
    except asyncio.TimeoutError:
        return provisional_create_response(relay_request_id, model, int(time.time()))


def _finish_create(
    submission: dict[str, Any], response: httpx.Response, response_headers: dict[str, str]
) -> JSONResponse:
    """Store a definite create answer: an upstream error or the new task."""
    relay_request_id = submission["relay_request_id"]
    protocol = submission["protocol"]
    model = submission["model"]
    if not 200 <= response.status_code < 300:
        return upstream_error(response, relay_request_id, "create", mark_failed=True)

    upstream_payload = response.json()
    task_id = _extract_create_task_id(protocol, upstream_payload)
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
        else fuyao.normalize_status(upstream_payload.get("status"))
        if protocol == fuyao.PROTOCOL
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
    reference_video_urls = submission.get("reference_video_urls")
    reference_videos = None
    if reference_video_urls is not None:
        reference_videos = {"urls": reference_video_urls}
        if not reference_video_urls:
            reference_videos["seconds"] = []
    database.create_task(
        task_id, submission["upstream_id"], relay_request_id, model, protocol, status, reference_videos
    )
    if reference_video_urls:
        start_reference_video_measurement(task_id, reference_video_urls)
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
    elif task["protocol"] == sub2api_video.PROTOCOL:
        fields = sub2api_video.extract_task_fields(payload)
        status_value = fields["status"]
        video_url = fields["video_url"]
        error_value = fields["error"]
        progress = fields["progress"]
    elif task["protocol"] == mai_token.PROTOCOL:
        fields = mai_token.extract_task_fields(payload)
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
    elif task["protocol"] == fuyao.PROTOCOL:
        fields = fuyao.extract_task_fields(payload, task["task_id"])
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
    task_id = resolve_task_id(task_id)
    task = database.get_task(task_id)
    if task is None:
        submission = database.get_create_submission(task_id) if task_id.startswith("vrq_") else None
        if submission is not None and not submission["upstream_task_id"]:
            recover_create(task_id)
            return _pending_create_status(database.get_create_submission(task_id) or submission)
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
        else sub2api_video.task_path(task_id)
        if task["protocol"] == sub2api_video.PROTOCOL
        else mai_token.task_path(task_id)
        if task["protocol"] == mai_token.PROTOCOL
        else rolldek.task_path(task_id)
        if task["protocol"] == rolldek.PROTOCOL
        else fuyao.task_path(task_id)
        if task["protocol"] == fuyao.PROTOCOL
        else f"/v1/videos/{task_id}"
    )
    url = (
        funai.api_url(task["base_url"], endpoint)
        if task["protocol"] == funai.PROTOCOL
        else f"{task['base_url']}{endpoint}"
    )
    attempt_timeout = min(POLL_ATTEMPT_TIMEOUT_SECONDS, settings.upstream_timeout_seconds)
    if timeout_seconds is not None:
        attempt_timeout = min(attempt_timeout, timeout_seconds)
    response, error = await _get_with_retries(url, headers, attempt_timeout, POLL_ATTEMPTS)
    if response is None:
        # The task is unaffected by our failure to reach the upstream. A 502
        # here makes New API treat the generation as failed and refund it.
        logger.warning("Video upstream task request failed for %s: %r", task_id, error)
        if relay_request_id:
            database.record_audit_event(
                relay_request_id, "poll", None, _error_text(error), {"detail": POLL_UNREACHABLE_DETAIL}
            )
        return pending_task_response(task)
    if not 200 <= response.status_code < 300:
        error_response = upstream_error(response, relay_request_id, "poll")
        if should_preserve_task_on_poll_error(response):
            return pending_task_response(task, response)
        else:
            try:
                error_payload = response.json()
            except ValueError:
                error_payload = None
            error = upstream_error_message(error_payload, "Video generation failed")
            database.update_task(task_id, "failed", None, error)
        return error_response
    try:
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("upstream task payload is not a JSON object")
    except ValueError:
        # Usually a gateway error page with a 2xx status; it says nothing
        # about the task, so the task stays pending.
        sanitized = {"detail": "Upstream returned invalid JSON"}
        if relay_request_id:
            database.record_audit_event(relay_request_id, "poll", response.status_code, response.text, sanitized)
        return pending_task_response(task)
    result, video_url = normalize_task_payload(task, payload)
    if result["status"] == "completed" and reference_videos_pending(task):
        # New API settles once, on the first completed poll; report the task
        # as still processing until the reference videos are measured. A
        # restart lost the background job, so it is started again here.
        start_reference_video_measurement(task_id, task["reference_videos"].get("urls") or [])
        result["status"] = "processing"
        result["progress"] = 99
        video_url = None
    usage = reference_video_usage(task)
    if usage:
        result["usage"] = usage
    error = result["error"]["message"] if result.get("error") else None
    database.update_task(task_id, result["status"], video_url, error)
    if result["status"] == "completed" and task.get("public_task_id"):
        result["video_url"] = database.public_video_url(task["public_task_id"])
    if relay_request_id:
        database.record_audit_event(relay_request_id, "poll", response.status_code, response.text, result)
    return JSONResponse(result, headers=response_headers)


_reconcile_job: asyncio.Task | None = None


def schedule_reconcile() -> None:
    """Refresh pending tasks in the background; at most one sweep at a time.

    A slow upstream status endpoint must not hold up the console page that
    asked for the refresh.
    """
    global _reconcile_job
    if _reconcile_job is not None and not _reconcile_job.done():
        return
    _reconcile_job = asyncio.create_task(reconcile_pending_tasks())


async def reconcile_pending_tasks(limit: int = 8, stale_seconds: int = 10) -> None:
    recover_pending_creates()
    task_ids = database.list_pending_task_ids(int(time.time()) - max(0, stale_seconds), limit)
    if not task_ids:
        return

    semaphore = asyncio.Semaphore(8)

    async def refresh(task_id: str) -> None:
        async with semaphore:
            try:
                await fetch_task(task_id)
            except HTTPException:
                # Connection and malformed-response errors are recorded by fetch_task and retried later.
                pass
            except Exception:
                # This refresh is a side effect of a read-only console request.
                # Any other failure is logged here instead of being returned to
                # a caller that only asked for the dashboard.
                logger.exception("Pending task refresh failed for %s", task_id)

    await asyncio.gather(*(refresh(task_id) for task_id in task_ids))


async def stream_upstream_content(
    source_url: str,
    request: Request,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
    default_media_type: str = "application/octet-stream",
    error_message: str = "Upstream download failed",
    source_url_validator: Callable[[str], bool] | None = None,
    reject_json: bool = False,
    filename_stem: str | None = None,
) -> StreamingResponse:
    request_headers = dict(headers or {})
    if request.headers.get("range"):
        request_headers["Range"] = request.headers["range"]
    if source_url_validator is not None and not source_url_validator(source_url):
        raise HTTPException(status_code=502, detail=error_message)
    # No read limit by default: a large file legitimately streams for a long
    # time. Connecting and the first answer must still come within bounds.
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=CONTENT_CONNECT_TIMEOUT_SECONDS),
        follow_redirects=source_url_validator is None,
    )
    current_url = source_url
    for _ in range(6):
        upstream_response = None
        error: httpx.RequestError | None = None
        for attempt in range(POLL_ATTEMPTS):
            try:
                upstream_response = await asyncio.wait_for(
                    client.send(client.build_request("GET", current_url, headers=request_headers), stream=True),
                    timeout=CONTENT_FIRST_BYTE_TIMEOUT_SECONDS,
                )
                break
            except asyncio.TimeoutError:
                error = httpx.ReadTimeout("no response headers")
            except httpx.RequestError as exc:
                error = exc
            if attempt + 1 < POLL_ATTEMPTS:
                await asyncio.sleep(POLL_RETRY_DELAY_SECONDS)
        if upstream_response is None:
            await client.aclose()
            logger.warning("%s: %r", error_message, error)
            raise HTTPException(status_code=502, detail=error_message) from error
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
    # A JSON body where a video was expected is an upstream task document, and
    # it usually names the upstream host and a signed storage link. Never
    # forward it: public links are meant to hide exactly those.
    if reject_json and _is_json_response(upstream_response.headers):
        await upstream_response.aclose()
        await client.aclose()
        logger.warning("%s: upstream returned JSON instead of a video", error_message)
        raise HTTPException(status_code=502, detail=error_message)

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

    # Some providers advertise Accept-Ranges but answer every Range request
    # with the full 200 body. Browsers reject that for <video>: an MP4 whose
    # moov box sits at the end can only be opened with a 206 tail request.
    status_code = upstream_response.status_code
    skip, limit = 0, None
    if status_code == 200 and "Range" in request_headers:
        size = _identity_content_length(upstream_response.headers)
        byte_range = parse_byte_range(request_headers["Range"], size) if size is not None else None
        if byte_range is None or request.headers.get("if-range"):
            response_headers.pop("accept-ranges", None)
        elif byte_range == UNSATISFIABLE_RANGE:
            await upstream_response.aclose()
            await client.aclose()
            return Response(
                status_code=416,
                headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
            )
        else:
            skip, limit = byte_range[0], byte_range[1] - byte_range[0] + 1
            status_code = 206
            response_headers["accept-ranges"] = "bytes"
            response_headers["content-length"] = str(limit)
            response_headers["content-range"] = f"bytes {byte_range[0]}-{byte_range[1]}/{size}"

    async def iterator():
        to_skip, remaining = skip, limit
        try:
            async for chunk in upstream_response.aiter_bytes(1024 * 256):
                if to_skip:
                    if len(chunk) <= to_skip:
                        to_skip -= len(chunk)
                        continue
                    chunk = chunk[to_skip:]
                    to_skip = 0
                if remaining is not None:
                    chunk = chunk[:remaining]
                    remaining -= len(chunk)
                if chunk:
                    yield chunk
                if remaining == 0:
                    break
        finally:
            await upstream_response.aclose()
            await client.aclose()

    media_type = upstream_response.headers.get("content-type", "")
    # Object stores often label every file ``binary/octet-stream``. Players
    # sniff the bytes and cope, but downloads and apps receiving a forwarded
    # file go by the type and name alone, so a generic label gets the
    # caller's known type instead.
    if media_type.split(";", 1)[0].strip().lower() in _GENERIC_MEDIA_TYPES:
        media_type = default_media_type
    if filename_stem and "content-disposition" not in response_headers:
        stem = re.sub(r"[^A-Za-z0-9._-]", "_", filename_stem)
        extension = _MEDIA_EXTENSIONS.get(media_type.split(";", 1)[0].strip().lower(), "")
        response_headers["content-disposition"] = f'inline; filename="{stem}{extension}"'
    return StreamingResponse(
        iterator(),
        status_code=status_code,
        media_type=media_type,
        headers=response_headers,
    )


_GENERIC_MEDIA_TYPES = {"", "application/octet-stream", "binary/octet-stream", "application/binary"}
_MEDIA_EXTENSIONS = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
}
UNSATISFIABLE_RANGE = (-1, -1)


def _identity_content_length(headers: httpx.Headers) -> int | None:
    # httpx decodes compressed bodies, so Content-Length only describes the
    # streamed bytes when no Content-Encoding was applied.
    if headers.get("content-encoding", "identity").strip().lower() != "identity":
        return None
    try:
        size = int(headers.get("content-length", ""))
    except ValueError:
        return None
    return size if size >= 0 else None


def parse_byte_range(header: str, size: int) -> tuple[int, int] | None:
    """Resolve a single ``bytes=`` range to inclusive offsets within ``size``.

    Returns ``None`` when the header cannot be served as one range (multiple
    ranges, other units, malformed values) and ``UNSATISFIABLE_RANGE`` when a
    well-formed range selects no bytes.
    """
    unit, _, spec = header.strip().partition("=")
    if unit.strip().lower() != "bytes" or "," in spec:
        return None
    first, separator, last = spec.strip().partition("-")
    first, last = first.strip(), last.strip()
    if not separator or not all(part.isascii() and part.isdigit() for part in (first, last) if part):
        return None
    if not first:
        if not last:
            return None
        suffix = int(last)
        if suffix == 0 or size == 0:
            return UNSATISFIABLE_RANGE
        return max(size - suffix, 0), size - 1
    start = int(first)
    if last and int(last) < start:
        return None
    if start >= size:
        return UNSATISFIABLE_RANGE
    end = min(int(last), size - 1) if last else size - 1
    return start, end


async def stream_content(
    task_id: str, request: Request, filename_stem: str | None = None
) -> StreamingResponse:
    task_id = resolve_task_id(task_id)
    task = database.get_task(task_id)
    if task is None:
        if task_id.startswith("vrq_") and database.get_create_submission(task_id) is not None:
            raise HTTPException(status_code=409, detail="Video is not completed")
        raise HTTPException(status_code=404, detail="Task not found")

    source_url = task.get("source_video_url")
    if not source_url:
        task_response = await fetch_task(task_id)
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
    elif task["protocol"] == sub2api_video.PROTOCOL:
        source_url = f"{task['base_url']}{sub2api_video.content_path(task_id)}"
    elif task["protocol"] == mai_token.PROTOCOL:
        source_url = f"{task['base_url']}{mai_token.content_path(task_id)}"
    elif task["protocol"] == funai.PROTOCOL:
        source_url = funai.api_url(task["base_url"], funai.content_path(task_id))
    elif task["protocol"] == rolldek.PROTOCOL:
        source_url = f"{task['base_url']}{rolldek.content_path(task_id)}"
    elif task["protocol"] == fuyao.PROTOCOL:
        source_url = f"{task['base_url']}{fuyao.content_path(task_id)}"
    else:
        source_url = f"{task['base_url']}/v1/videos/{task_id}/content"

    if task["protocol"] == fuyao.PROTOCOL and same_origin(source_url, task["base_url"]):
        source_url = await _resolve_fuyao_video_link(source_url, task)

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
        reject_json=True,
        filename_stem=filename_stem or task_id,
    )


async def _resolve_fuyao_video_link(content_url: str, task: dict[str, Any]) -> str:
    """Turn Fuyao's ``/content`` endpoint into the actual video file URL.

    Despite the documented download endpoint, ``/content`` can answer with the
    task JSON instead of the MP4. Streaming that body would hand the viewer the
    upstream host and its signed storage link, so the link is read from the
    JSON and the file is streamed from it. A real video response is kept.
    """
    payload: Any = None
    error: httpx.RequestError | None = None
    for attempt in range(POLL_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=POLL_ATTEMPT_TIMEOUT_SECONDS, follow_redirects=False) as client:
                # Streamed so a real MP4 answer is not read into memory just to
                # learn its content type; only a JSON body is read.
                async with client.stream(
                    "GET", content_url, headers={"Authorization": f"Bearer {task['api_key']}"}
                ) as response:
                    if response.is_redirect and response.headers.get("location"):
                        return urljoin(content_url, response.headers["location"])
                    if response.status_code != 200 or not _is_json_response(response.headers):
                        return content_url
                    body = await response.aread()
            break
        except httpx.RequestError as exc:
            error = exc
            if attempt + 1 < POLL_ATTEMPTS:
                await asyncio.sleep(POLL_RETRY_DELAY_SECONDS)
    else:
        logger.warning("Fuyao content lookup failed: %r", error)
        raise HTTPException(status_code=502, detail="Video upstream download failed") from error
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    link = fuyao.extract_video_link(payload)
    if link is None:
        logger.warning("Fuyao content endpoint returned JSON without a video link")
        raise HTTPException(status_code=502, detail="Video upstream download failed")
    # Not cached: the link is signed and expires (``Expires=...``), so every
    # download asks ``/content`` for a fresh one.
    return link


def _is_json_response(headers: httpx.Headers) -> bool:
    media_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    return media_type == "application/json" or media_type.endswith("+json")


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
