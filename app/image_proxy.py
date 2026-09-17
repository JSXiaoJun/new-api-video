from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.datastructures import UploadFile

from . import database, image_database, proxy
from .config import settings

logger = logging.getLogger("uvicorn.error")

# Opt-in request header. When set, responses carry the public asset link for
# every stored image (including base64 payloads that have no upstream URL), so a
# relay can record a viewable link in its own logs without changing what normal
# clients receive.
ASSET_LINK_HEADER = "x-image-asset-links"
# Response headers are small; a response never needs more than this many links.
MAX_ASSET_LINKS = 32
IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def _resolve_route(
    public_model: str, size: str = "", quality: str = ""
) -> tuple[dict[str, Any], str, str, str]:
    if not public_model:
        raise HTTPException(status_code=400, detail="model is required")
    if len(public_model) > 160:
        raise HTTPException(status_code=400, detail="model is too long")
    route = image_database.select_route(public_model)
    if route is None:
        raise HTTPException(status_code=404, detail=f"No image route is registered for model={public_model}")
    return route, public_model, size, quality


def _request_parameter(payload: dict[str, Any], name: str, fallback: str = "") -> str:
    value = payload.get(name)
    if value is None and fallback:
        value = payload.get(fallback)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"{name} must be a string")
    return value.strip()


def upstream_api_url(base_url: str, endpoint: str) -> str:
    normalized_base_url = base_url.rstrip("/")
    normalized_endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
    if normalized_base_url.endswith("/v1") and normalized_endpoint.startswith("/v1/"):
        normalized_endpoint = normalized_endpoint.removeprefix("/v1")
    return normalized_base_url + normalized_endpoint


def _select_route(payload: dict[str, Any]) -> tuple[dict[str, Any], str, str, str]:
    return _resolve_route(
        _request_parameter(payload, "model"),
        _request_parameter(payload, "size", "resolution"),
        _request_parameter(payload, "quality"),
    )


def _response_headers(
    upstream_response: httpx.Response, request_id: str, asset_links: list[str] | None = None
) -> dict[str, str]:
    headers = {"X-Oneapi-Request-Id": request_id}
    for name in ("content-disposition", "cache-control", "retry-after"):
        value = upstream_response.headers.get(name)
        if value:
            headers[name] = value
    if asset_links:
        headers[ASSET_LINK_HEADER] = json.dumps(asset_links[:MAX_ASSET_LINKS])
    return headers


def _relay_response(
    upstream_response: httpx.Response, request_id: str, asset_links: list[str] | None = None
) -> Response:
    headers = _response_headers(upstream_response, request_id, asset_links)
    content_type = upstream_response.headers.get("content-type")
    if content_type:
        headers["content-type"] = content_type
    return Response(content=upstream_response.content, status_code=upstream_response.status_code, headers=headers)


def _response_has_image_data(upstream_response: httpx.Response) -> bool:
    try:
        payload = upstream_response.json()
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    data = payload.get("data")
    if not isinstance(data, list):
        return False
    return any(
        isinstance(item, dict) and isinstance(item.get("url") or item.get("b64_json"), str)
        and bool(item.get("url") or item.get("b64_json"))
        for item in data
    )


def is_safe_image_source_url(source_url: str) -> bool:
    parsed_url = urlparse(source_url)
    source_host = parsed_url.hostname
    if (
        parsed_url.scheme not in {"http", "https"}
        or not source_host
        or parsed_url.username
        or parsed_url.password
        or source_host.lower() in {"localhost", "localhost.localdomain"}
        or source_host.lower().endswith(".local")
    ):
        return False
    try:
        source_ip = ip_address(source_host)
    except ValueError:
        return True
    return source_ip.is_global


def asset_links_requested(request: Request | None) -> bool:
    if request is None:
        return False
    value = request.headers.get(ASSET_LINK_HEADER, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def public_asset_url(asset_id: str) -> str:
    return f"{settings.image_public_base_url}/public/images/assets/{asset_id}"


def sniff_image_mime(data: bytes) -> str:
    for signature, mime_type in IMAGE_SIGNATURES:
        if data.startswith(signature):
            return mime_type
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:12] in {b"ftypavif", b"ftypavis"}:
        return "image/avif"
    return "application/octet-stream"


def store_encoded_image(encoded: str) -> str | None:
    """Persist a base64 image payload and return its public link.

    The ceiling is applied before decoding so an oversized payload is rejected
    without materialising it in memory first.
    """
    raw = encoded.strip()
    if raw.startswith("data:"):
        _, _, raw = raw.partition(",")
    if not raw:
        return None
    limit = settings.image_storage_max_bytes
    if limit and len(raw) > (limit * 4) // 3 + 16:
        return None
    try:
        data = base64.b64decode(raw, validate=False)
    except (binascii.Error, ValueError):
        return None
    if not data:
        return None
    asset_id = image_database.create_image_blob_asset(data, sniff_image_mime(data))
    return public_asset_url(asset_id) if asset_id else None


def _sanitize_image_payload(
    upstream_response: httpx.Response, include_asset_links: bool
) -> tuple[dict[str, Any] | None, list[str]]:
    """Rewrite upstream image URLs and store inline payloads.

    URL payloads are rewritten in the body so every client receives the
    desensitized middleware link. Base64 payloads are returned to the client
    byte-for-byte, so their stored link is reported out of band through the
    x-image-asset-links response header instead of mutating the body.
    """
    try:
        payload = upstream_response.json()
    except ValueError:
        return None, []
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return None, []

    changed = False
    asset_links: list[str] = []
    for item in payload["data"]:
        if not isinstance(item, dict):
            continue
        source_url = item.get("url")
        if isinstance(source_url, str) and source_url.strip():
            source_url = source_url.strip()
            if not is_safe_image_source_url(source_url):
                raise ValueError("invalid_image_url")
            item["url"] = public_asset_url(image_database.create_image_url_asset(source_url))
            changed = True
            continue
        encoded = item.get("b64_json")
        if include_asset_links and isinstance(encoded, str) and encoded.strip():
            asset_url = store_encoded_image(encoded)
            if asset_url:
                asset_links.append(asset_url)
    return (payload if changed else None), asset_links


def _upstream_error_response(
    request_id: str, status_code: int, message: str, code: str, retry_after: str | None = None
) -> JSONResponse:
    headers = {"X-Oneapi-Request-Id": request_id}
    if retry_after:
        headers["Retry-After"] = retry_after
    return JSONResponse(
        {"error": {"message": message, "type": "upstream_error", "code": code}},
        status_code=status_code,
        headers=headers,
    )


def classify_health_outcome(http_status: int, response_body: str = "") -> str:
    if 200 <= http_status < 300:
        return "success"
    if http_status >= 500 or http_status in {401, 402, 408, 425, 429}:
        return "failure"
    normalized_body = response_body[:4000].lower()
    availability_markers = (
        "temporarily unavailable",
        "service unavailable",
        "upstream service",
        "upstream request failed",
        "upstream accounts",
        "connection failed",
        "timed out",
        "timeout",
        "rate limit",
        "too many requests",
        "overloaded",
        "capacity",
        "暂时不可用",
        "服务不可用",
        "上游",
        "超时",
        "限流",
        "繁忙",
        "过载",
    )
    if any(marker in normalized_body for marker in availability_markers):
        return "failure"
    return "neutral"


def _connection_failure_response(
    route: dict[str, Any],
    operation: str,
    public_model: str,
    size: str,
    quality: str,
    latency_ms: int,
    exc: httpx.RequestError,
) -> JSONResponse:
    request_id = image_database.record_request(
        route,
        operation,
        public_model,
        size,
        quality,
        False,
        None,
        latency_ms,
        "failure",
        f"{type(exc).__name__}: connection failed",
    )
    return _upstream_error_response(request_id, 502, "Image upstream connection failed", "upstream_connection_failed")


def _finalize_upstream_response(
    route: dict[str, Any],
    operation: str,
    public_model: str,
    size: str,
    quality: str,
    latency_ms: int,
    upstream_response: httpx.Response,
    include_asset_links: bool = False,
) -> Response:
    upstream_status = upstream_response.status_code
    success = 200 <= upstream_status < 300
    health_outcome = classify_health_outcome(upstream_status, upstream_response.text)
    error = None if success else f"HTTP {upstream_status}"
    sanitized_payload = None
    asset_links: list[str] = []
    if success and not _response_has_image_data(upstream_response):
        success = False
        health_outcome = "failure"
        error = "No usable image data returned"
    if success:
        try:
            sanitized_payload, asset_links = _sanitize_image_payload(upstream_response, include_asset_links)
        except Exception:
            success = False
            health_outcome = "failure"
            error = "Unable to anonymize image URL"

    request_id = image_database.record_request(
        route,
        operation,
        public_model,
        size,
        quality,
        success,
        upstream_status,
        latency_ms,
        health_outcome,
        error,
    )
    if error == "No usable image data returned":
        return _upstream_error_response(request_id, 502, "Image upstream returned no usable image", "no_image_returned")
    if error == "Unable to anonymize image URL":
        return _upstream_error_response(
            request_id,
            502,
            "Image upstream returned an unusable image URL",
            "image_url_anonymization_failed",
        )
    if not 200 <= upstream_status < 300:
        client_status = upstream_status if 400 <= upstream_status < 600 else 502
        return _upstream_error_response(
            request_id,
            client_status,
            "Image upstream rejected the request" if client_status < 500 else "Image upstream request failed",
            f"upstream_http_{upstream_status}",
            upstream_response.headers.get("retry-after"),
        )
    if sanitized_payload is not None:
        return JSONResponse(
            sanitized_payload,
            status_code=upstream_status,
            headers=_response_headers(upstream_response, request_id, asset_links),
        )
    return _relay_response(upstream_response, request_id, asset_links)


async def stream_image_asset(asset_id: str, request: Request) -> Response:
    asset = image_database.get_image_asset(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Image link has expired or does not exist")
    remaining = max(0, int(asset["expires_at"]) - int(time.time()))
    if asset["storage_kind"] == "local_file":
        path = asset["local_path"]
        if path is None or not path.is_file():
            raise HTTPException(status_code=404, detail="Image link has expired or does not exist")
        return FileResponse(
            path,
            media_type=asset["mime_type"],
            headers={"Cache-Control": f"private, max-age={remaining}"},
        )
    response = await proxy.stream_upstream_content(
        asset["source_url"],
        request,
        timeout=settings.image_upstream_timeout_seconds,
        error_message="Image download failed",
        source_url_validator=is_safe_image_source_url,
    )
    # The upstream copy is cached under its own rules, which may outlive this
    # link. Serve the remaining lifetime instead so a shared cache can never
    # hand out an image after the link expired.
    response.headers["Cache-Control"] = f"private, max-age={remaining}"
    return response


async def forward_json(
    payload: dict[str, Any],
    operation: str,
    idempotency_key: str | None = None,
    include_asset_links: bool = False,
) -> Response:
    route, public_model, size, quality = _select_route(payload)
    upstream_payload = dict(payload)
    upstream_payload["model"] = route["upstream_model"]
    headers = {
        "Authorization": f"Bearer {route['api_key']}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    endpoint = "/v1/images/generations" if operation == "generation" else "/v1/images/edits"
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=settings.image_upstream_timeout_seconds) as client:
            upstream_response = await _bounded_upstream_post(
                client, upstream_api_url(route["base_url"], endpoint), headers=headers, json=upstream_payload
            )
    except httpx.RequestError as exc:
        return _connection_failure_response(
            route,
            operation,
            public_model,
            size,
            quality,
            round((time.perf_counter() - started) * 1000),
            exc,
        )
    if upstream_response is None:
        return _oversized_upstream_response(
            route, operation, public_model, size, quality, round((time.perf_counter() - started) * 1000)
        )

    return _finalize_upstream_response(
        route,
        operation,
        public_model,
        size,
        quality,
        round((time.perf_counter() - started) * 1000),
        upstream_response,
        include_asset_links,
    )


async def forward_edit(request: Request, idempotency_key: str | None = None) -> Response:
    form = await request.form()
    try:
        fields: dict[str, Any] = {}
        for name, value in form.multi_items():
            if not isinstance(value, UploadFile) and name not in fields:
                fields[name] = value
        route, public_model, size, quality = _select_route(fields)

        parts = []
        for name, value in form.multi_items():
            if isinstance(value, UploadFile):
                parts.append((name, (value.filename or "upload", value.file, value.content_type)))
            else:
                parts.append((name, (None, route["upstream_model"] if name == "model" else str(value))))
        headers = {"Authorization": f"Bearer {route['api_key']}", "Accept": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=settings.image_upstream_timeout_seconds) as client:
                upstream_response = await _bounded_upstream_post(
                    client, upstream_api_url(route["base_url"], "/v1/images/edits"), headers=headers, files=parts
                )
        except httpx.RequestError as exc:
            return _connection_failure_response(
                route,
                "edit",
                public_model,
                size,
                quality,
                round((time.perf_counter() - started) * 1000),
                exc,
            )
        if upstream_response is None:
            return _oversized_upstream_response(
                route, "edit", public_model, size, quality, round((time.perf_counter() - started) * 1000)
            )

        return _finalize_upstream_response(
            route,
            "edit",
            public_model,
            size,
            quality,
            round((time.perf_counter() - started) * 1000),
            upstream_response,
            asset_links_requested(request),
        )
    finally:
        await form.close()


UPSTREAM_READ_CHUNK_BYTES = 64 * 1024
# The buffered reply holds decoded bytes, so the framing headers that described
# the wire format are dropped instead of being replayed onto the copy.
UPSTREAM_FRAMING_HEADERS = ("content-encoding", "content-length", "transfer-encoding")


async def _bounded_upstream_post(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response | None:
    """POST to an image upstream, buffering the reply under a hard ceiling.

    Returns None when the reply exceeds the ceiling, so the caller can refuse it
    before it ever reaches memory in full.
    """
    request = client.build_request("POST", url, **kwargs)
    upstream_response = await client.send(request, stream=True)
    limit = settings.image_max_upstream_response_bytes
    body = bytearray()
    try:
        async for chunk in upstream_response.aiter_bytes(UPSTREAM_READ_CHUNK_BYTES):
            body.extend(chunk)
            if limit and len(body) > limit:
                logger.warning(
                    "image upstream reply refused: exceeded the %s byte ceiling", limit
                )
                return None
    finally:
        await upstream_response.aclose()
    headers = {
        name: value
        for name, value in upstream_response.headers.items()
        if name.lower() not in UPSTREAM_FRAMING_HEADERS
    }
    return httpx.Response(
        upstream_response.status_code,
        headers=headers,
        content=bytes(body),
        request=request,
    )


def _oversized_upstream_response(
    route: dict[str, Any],
    operation: str,
    public_model: str,
    size: str,
    quality: str,
    latency_ms: int,
) -> JSONResponse:
    request_id = image_database.record_request(
        route,
        operation,
        public_model,
        size,
        quality,
        False,
        None,
        latency_ms,
        "neutral",
        "Upstream response exceeded the size ceiling",
    )
    return _upstream_error_response(
        request_id, 502, "Image upstream response is too large to relay", "upstream_response_too_large"
    )


GEMINI_IMAGE_ACTIONS = ("predict", "generateContent", "streamGenerateContent")
GEMINI_API_VERSION_SUFFIXES = ("/v1beta", "/v1alpha", "/v1")


def gemini_upstream_url(base_url: str, upstream_model: str, action: str, query: str = "") -> str:
    """Build the native Gemini endpoint for an upstream.

    A base URL that already carries its API version (for example
    `https://host/v1beta`) is used as is, so the field accepts both forms.
    """
    normalized = base_url.rstrip("/")
    if not normalized.endswith(GEMINI_API_VERSION_SUFFIXES):
        normalized = f"{normalized}/v1beta"
    url = f"{normalized}/models/{upstream_model}:{action}"
    return f"{url}?{query}" if query else url


def _gemini_request_operation(payload: dict[str, Any]) -> str:
    """Label a Gemini call as generation or edit from the images it carries.

    Input images are never stored or logged; they only decide the log label.
    """
    instances = payload.get("instances")
    if isinstance(instances, list):
        for instance in instances:
            if isinstance(instance, dict) and (instance.get("image") or instance.get("imageBytes")):
                return "edit"
    contents = payload.get("contents")
    if isinstance(contents, list):
        for content in contents:
            parts = content.get("parts") if isinstance(content, dict) else None
            if not isinstance(parts, list):
                continue
            for part in parts:
                if isinstance(part, dict) and (part.get("inlineData") or part.get("inline_data")):
                    return "edit"
    return "generation"


def _gemini_inline_images(payload: dict[str, Any]) -> list[str]:
    """Collect the base64 images a native Gemini payload carries."""
    encoded_images: list[str] = []
    predictions = payload.get("predictions")
    if isinstance(predictions, list):
        for prediction in predictions:
            if not isinstance(prediction, dict) or prediction.get("raiFilteredReason"):
                continue
            encoded = prediction.get("bytesBase64Encoded")
            if isinstance(encoded, str) and encoded.strip():
                encoded_images.append(encoded)
    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            content = candidate.get("content") if isinstance(candidate, dict) else None
            parts = content.get("parts") if isinstance(content, dict) else None
            if not isinstance(parts, list):
                continue
            for part in parts:
                inline = None
                if isinstance(part, dict):
                    inline = part.get("inlineData") or part.get("inline_data")
                if not isinstance(inline, dict):
                    continue
                encoded = inline.get("data")
                if isinstance(encoded, str) and encoded.strip():
                    encoded_images.append(encoded)
    return encoded_images


def _gemini_response_images(upstream_response: httpx.Response) -> list[str]:
    """Pull the base64 images out of a native Gemini response.

    Covers both buffered shapes (imagen predictions and generateContent
    candidates) and the SSE shape streamGenerateContent emits.
    """
    if "text/event-stream" in upstream_response.headers.get("content-type", "").lower():
        encoded_images: list[str] = []
        for line in upstream_response.content.splitlines():
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            try:
                chunk = json.loads(line[len(b"data:") :].strip())
            except ValueError:
                continue
            if isinstance(chunk, dict):
                encoded_images.extend(_gemini_inline_images(chunk))
        return encoded_images
    try:
        payload = upstream_response.json()
    except ValueError:
        return []
    return _gemini_inline_images(payload) if isinstance(payload, dict) else []


def _gemini_error_response(request_id: str, status_code: int, message: str, code: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": status_code, "message": message, "status": code}},
        status_code=status_code,
        headers={"X-Oneapi-Request-Id": request_id},
    )


async def forward_gemini(
    payload: dict[str, Any],
    public_model: str,
    action: str,
    include_asset_links: bool,
    query: str = "",
    idempotency_key: str | None = None,
) -> Response:
    """Proxy a native Gemini image call to its upstream.

    Serves both the imagen (`:predict`) and nano-banana (`:generateContent`)
    shapes. Request and response bodies travel untouched: the generated images
    are stored so the caller gets a viewable link for its own logs, reported
    through the x-image-asset-links response header.
    """
    route, public_model, _, _ = _resolve_route(public_model)
    operation = _gemini_request_operation(payload)
    headers = {
        "x-goog-api-key": route["api_key"],
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=settings.image_upstream_timeout_seconds) as client:
            upstream_response = await _bounded_upstream_post(
                client,
                gemini_upstream_url(route["base_url"], route["upstream_model"], action, query),
                headers=headers,
                json=payload,
            )
    except httpx.RequestError as exc:
        return _connection_failure_response(
            route,
            operation,
            public_model,
            "",
            "",
            round((time.perf_counter() - started) * 1000),
            exc,
        )
    if upstream_response is None:
        return _oversized_upstream_response(
            route, operation, public_model, "", "", round((time.perf_counter() - started) * 1000)
        )
    return _finalize_gemini_response(
        route,
        operation,
        public_model,
        round((time.perf_counter() - started) * 1000),
        upstream_response,
        include_asset_links,
    )


def _finalize_gemini_response(
    route: dict[str, Any],
    operation: str,
    public_model: str,
    latency_ms: int,
    upstream_response: httpx.Response,
    include_asset_links: bool,
) -> Response:
    upstream_status = upstream_response.status_code
    success = 200 <= upstream_status < 300
    health_outcome = classify_health_outcome(upstream_status, upstream_response.text)
    error = None if success else f"HTTP {upstream_status}"
    asset_links: list[str] = []
    if success:
        encoded_images = _gemini_response_images(upstream_response)
        if not encoded_images:
            success = False
            health_outcome = "failure"
            error = "No usable image data returned"
        elif include_asset_links:
            for encoded in encoded_images:
                asset_url = store_encoded_image(encoded)
                if asset_url:
                    asset_links.append(asset_url)

    request_id = image_database.record_request(
        route,
        operation,
        public_model,
        "",
        "",
        success,
        upstream_status,
        latency_ms,
        health_outcome,
        error,
    )
    if error == "No usable image data returned":
        return _gemini_error_response(
            request_id, 502, "Image upstream returned no usable image", "no_image_returned"
        )
    return _relay_response(upstream_response, request_id, asset_links)
