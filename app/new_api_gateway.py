from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from .config import settings


FORWARDED_REQUEST_HEADERS = (
    "authorization",
    "accept",
    "content-type",
    "idempotency-key",
    "range",
)
FORWARDED_RESPONSE_HEADERS = (
    "accept-ranges",
    "cache-control",
    "content-disposition",
    "content-length",
    "content-range",
    "content-type",
    "etag",
    "last-modified",
    "x-new-api-version",
    "x-oneapi-request-id",
)


def _request_headers(request: Request) -> dict[str, str]:
    headers = {
        name: value
        for name in FORWARDED_REQUEST_HEADERS
        if (value := request.headers.get(name)) is not None
    }
    headers["accept-encoding"] = "identity"
    return headers


def _response_headers(response: httpx.Response) -> dict[str, str]:
    return {
        name: value
        for name in FORWARDED_RESPONSE_HEADERS
        if (value := response.headers.get(name)) is not None
    }


async def _close_stream(client: httpx.AsyncClient, response: httpx.Response) -> None:
    await response.aclose()
    await client.aclose()


async def _stream_body(response: httpx.Response) -> AsyncIterator[bytes]:
    async for chunk in response.aiter_raw():
        yield chunk


async def forward(request: Request, path: str, *, stream: bool = False) -> Response:
    target_url = f"{settings.new_api_gateway_base_url}{path}"
    if request.url.query:
        target_url = f"{target_url}?{request.url.query}"

    body = await request.body() if request.method not in {"GET", "HEAD"} else None
    client = httpx.AsyncClient(
        timeout=settings.new_api_gateway_timeout_seconds,
        follow_redirects=False,
    )
    upstream_request = client.build_request(
        request.method,
        target_url,
        headers=_request_headers(request),
        content=body,
    )
    try:
        response = await client.send(upstream_request, stream=stream)
    except httpx.RequestError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail="New API gateway connection failed") from exc

    headers = _response_headers(response)
    if stream:
        return StreamingResponse(
            _stream_body(response),
            status_code=response.status_code,
            headers=headers,
            background=BackgroundTask(_close_stream, client, response),
        )

    try:
        content = await response.aread()
    finally:
        await response.aclose()
        await client.aclose()
    return Response(content=content, status_code=response.status_code, headers=headers)
