from __future__ import annotations

import time
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.datastructures import UploadFile

from . import database, image_database, proxy
from .config import settings


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


def _select_route(payload: dict[str, Any], operation: str) -> tuple[dict[str, Any], str, str, str]:
    public_model = _request_parameter(payload, "model")
    if not public_model:
        raise HTTPException(status_code=400, detail="model is required")
    if len(public_model) > 160:
        raise HTTPException(status_code=400, detail="model is too long")
    size = _request_parameter(payload, "size", "resolution")
    quality = _request_parameter(payload, "quality")
    route = image_database.select_route(public_model, size, quality, operation)
    if route is None:
        requested_size = size or "default"
        requested_quality = quality or "default"
        raise HTTPException(
            status_code=404,
            detail=(
                f"No image route matches model={public_model}, size={requested_size}, "
                f"quality={requested_quality}, operation={operation}"
            ),
        )
    return route, public_model, size, quality


def _response_headers(upstream_response: httpx.Response, request_id: str) -> dict[str, str]:
    headers = {"X-Oneapi-Request-Id": request_id}
    for name in ("content-disposition", "cache-control", "retry-after"):
        value = upstream_response.headers.get(name)
        if value:
            headers[name] = value
    return headers


def _relay_response(upstream_response: httpx.Response, request_id: str) -> Response:
    headers = _response_headers(upstream_response, request_id)
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


def _anonymize_image_urls(upstream_response: httpx.Response) -> dict[str, Any] | None:
    try:
        payload = upstream_response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return None

    changed = False
    for item in payload["data"]:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            continue
        source_url = item["url"].strip()
        if not is_safe_image_source_url(source_url):
            raise ValueError("invalid_image_url")
        asset_id = image_database.create_image_asset(source_url)
        item["url"] = f"{database.get_public_link_base_url()}/public/images/assets/{asset_id}"
        changed = True
    return payload if changed else None


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
) -> Response:
    upstream_status = upstream_response.status_code
    success = 200 <= upstream_status < 300
    health_outcome = classify_health_outcome(upstream_status, upstream_response.text)
    error = None if success else f"HTTP {upstream_status}"
    sanitized_payload = None
    if success and not _response_has_image_data(upstream_response):
        success = False
        health_outcome = "failure"
        error = "No usable image data returned"
    if success:
        try:
            sanitized_payload = _anonymize_image_urls(upstream_response)
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
            headers=_response_headers(upstream_response, request_id),
        )
    return _relay_response(upstream_response, request_id)


async def stream_image_asset(asset_id: str, request: Request) -> StreamingResponse:
    source_url = image_database.get_image_asset(asset_id)
    if source_url is None:
        raise HTTPException(status_code=404, detail="Image link has expired or does not exist")
    return await proxy.stream_upstream_content(
        source_url,
        request,
        timeout=settings.image_upstream_timeout_seconds,
        error_message="Image download failed",
        source_url_validator=is_safe_image_source_url,
    )


async def forward_json(payload: dict[str, Any], operation: str, idempotency_key: str | None = None) -> Response:
    route, public_model, size, quality = _select_route(payload, operation)
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
            upstream_response = await client.post(
                upstream_api_url(route["base_url"], endpoint), headers=headers, json=upstream_payload
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

    return _finalize_upstream_response(
        route,
        operation,
        public_model,
        size,
        quality,
        round((time.perf_counter() - started) * 1000),
        upstream_response,
    )


async def forward_edit(request: Request, idempotency_key: str | None = None) -> Response:
    form = await request.form()
    try:
        fields: dict[str, Any] = {}
        for name, value in form.multi_items():
            if not isinstance(value, UploadFile) and name not in fields:
                fields[name] = value
        route, public_model, size, quality = _select_route(fields, "edit")

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
                upstream_response = await client.post(
                    upstream_api_url(route["base_url"], "/v1/images/edits"), headers=headers, files=parts
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

        return _finalize_upstream_response(
            route,
            "edit",
            public_model,
            size,
            quality,
            round((time.perf_counter() - started) * 1000),
            upstream_response,
        )
    finally:
        await form.close()
