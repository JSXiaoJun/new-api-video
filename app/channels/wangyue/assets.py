"""Upload reference images as private Wangyue pool assets.

The pool does not fetch image URLs itself. Each image is downloaded here,
leased (``POST /v1/pool/api/assets/input/lease`` with its size, SHA-256 and
MD5), PUT to the signed storage URL with exactly the headers the lease
returned, and confirmed. Assets are bound to the task ID they were leased
for, so the task must be created with the same ``taskId``.

Nothing here is billed, so every step may be retried. A failure means no
task was created.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from ... import media_probe
from . import signing, tasks


logger = logging.getLogger("uvicorn.error")

MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_REDIRECTS = 5
DOWNLOAD_TIMEOUT_SECONDS = 60.0
API_TIMEOUT_SECONDS = 30.0
UPLOAD_TIMEOUT_SECONDS = 120.0
ATTEMPTS = 3
RETRY_DELAY_SECONDS = 1.0

_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


class WangyueAssetError(Exception):
    """An image could not become a pool asset; no task was created."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def sniff_image_type(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def upload_images(base_url: str, api_key: str, task_id: str, urls: list[str]) -> list[str]:
    """Return one confirmed asset ID per image URL, in order."""
    asset_ids: list[str] = []
    for index, url in enumerate(urls, start=1):
        data = await _download(url, index)
        asset_ids.append(await _upload(base_url, api_key, task_id, data, index))
    return asset_ids


async def _download(url: str, index: int) -> bytes:
    label = f"第 {index} 张参考图片"
    try:
        async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=False) as client:
            for _ in range(MAX_REDIRECTS + 1):
                await media_probe.check_public_url(url)
                async with client.stream("GET", url) as response:
                    if response.is_redirect and response.headers.get("location"):
                        url = urljoin(url, response.headers["location"])
                        continue
                    if response.status_code != 200:
                        raise WangyueAssetError(f"{label}下载失败：HTTP {response.status_code}", 400)
                    chunks: list[bytes] = []
                    received = 0
                    async for chunk in response.aiter_bytes():
                        received += len(chunk)
                        if received > MAX_IMAGE_BYTES:
                            raise WangyueAssetError(f"{label}超过 50MB", 400)
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    break
            else:
                raise WangyueAssetError(f"{label}重定向次数过多", 400)
    except media_probe.MediaProbeError as exc:
        raise WangyueAssetError(f"{label}不是可公开访问的链接", 400) from exc
    except httpx.RequestError as exc:
        logger.info("Wangyue reference image download failed (%s): %r", urlsplit(url).hostname, exc)
        raise WangyueAssetError(f"{label}下载失败", 400) from exc
    if not data:
        raise WangyueAssetError(f"{label}是空文件", 400)
    if sniff_image_type(data) is None:
        raise WangyueAssetError(f"{label}只支持 JPEG、PNG 或 WebP", 400)
    return data


async def _upload(base_url: str, api_key: str, task_id: str, data: bytes, index: int) -> str:
    content_type = sniff_image_type(data) or "application/octet-stream"
    lease = await signed_post(base_url + tasks.ASSET_LEASE_PATH, api_key, {
        "taskId": task_id,
        "fileName": f"reference-{index}.{_EXTENSIONS.get(content_type, 'bin')}",
        "contentType": content_type,
        "sizeBytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "md5Base64": base64.b64encode(hashlib.md5(data).digest()).decode("ascii"),
    })
    asset = lease.get("asset") if isinstance(lease.get("asset"), dict) else {}
    upload = lease.get("upload") if isinstance(lease.get("upload"), dict) else {}
    asset_id = str(asset.get("id") or "").strip()
    upload_url = upload.get("url")
    if not asset_id or not isinstance(upload_url, str) or not upload_url.startswith("https://"):
        raise WangyueAssetError("号池素材租约缺少上传地址")
    headers = upload.get("headers") if isinstance(upload.get("headers"), dict) else {}
    await _put(
        str(upload.get("method") or "PUT").upper(),
        upload_url,
        {str(key): str(value) for key, value in headers.items() if value is not None},
        data,
    )
    confirmed = await signed_post(base_url + tasks.asset_confirm_path(asset_id), api_key, None)
    state = confirmed.get("asset", {}).get("state") if isinstance(confirmed.get("asset"), dict) else None
    if state not in (None, "ready"):
        raise WangyueAssetError(f"号池素材确认后状态为 {state}")
    return asset_id


async def _put(method: str, url: str, headers: dict[str, str], data: bytes) -> None:
    if method != "PUT":
        raise WangyueAssetError(f"号池素材上传方式 {method} 不受支持")
    error: Exception | None = None
    for attempt in range(ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT_SECONDS, follow_redirects=False) as client:
                response = await client.put(url, headers=headers, content=data)
            if 200 <= response.status_code < 300:
                return
            error = WangyueAssetError(f"号池素材上传失败：HTTP {response.status_code}")
            if response.status_code < 500:
                break
        except httpx.RequestError as exc:
            error = exc
        if attempt + 1 < ATTEMPTS:
            await asyncio.sleep(RETRY_DELAY_SECONDS)
    if isinstance(error, WangyueAssetError):
        raise error
    raise WangyueAssetError("号池素材上传失败") from error


async def signed_post(url: str, api_key: str, body: dict[str, Any] | None) -> dict[str, Any]:
    """A signed POST, retried on transport errors, 5xx and 429 (fresh nonce each time)."""
    content = signing.encode_body(body)
    response: httpx.Response | None = None
    error: httpx.RequestError | None = None
    for attempt in range(ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=API_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    url, headers=signing.signed_headers(api_key, "POST", url, content), content=content
                )
            error = None
            if response.status_code < 500 and response.status_code != 429:
                break
        except httpx.RequestError as exc:
            error = exc
        if attempt + 1 < ATTEMPTS:
            await asyncio.sleep(RETRY_DELAY_SECONDS)
    if response is None or error is not None:
        raise WangyueAssetError("号池素材接口无响应") from error
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not 200 <= response.status_code < 300 or not isinstance(payload, dict) or payload.get("ok") is False:
        message = payload.get("message") if isinstance(payload, dict) else None
        code = payload.get("error") if isinstance(payload, dict) else None
        detail = " ".join(str(part) for part in (code, message) if part) or f"HTTP {response.status_code}"
        logger.warning("Wangyue asset call %s failed: HTTP %s %s", urlsplit(url).path, response.status_code,
                       json.dumps(payload, ensure_ascii=False)[:500] if payload else response.text[:500])
        raise WangyueAssetError(f"号池素材接口返回错误：{detail}")
    return payload
