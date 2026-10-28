"""Adapter for the XingAPI video API (``https://xingapi.top/v1``).

Tasks are created at ``/v1/videos/generations``, polled at
``/v1/videos/{task_id}`` and downloaded from ``/v1/videos/{task_id}/content``
with the same Bearer key. The request takes the public fields only
(``duration``, ``ratio``, ``resolution`` and the ``images`` / ``videos`` /
``audios`` URL arrays); upstream-specific names such as ``seconds`` or
``image_urls`` must never be forwarded.

Which backend serves a model depends on its exact name: ``seedance-2.0-933x``
and ``seedance-2.0-mini`` (hyphenated) go to the 喵呜 backend, while
``seedance2.0-mini`` goes to the 纯900 backend. Neither accepts reference
videos.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urlsplit


PROTOCOL = "xingapi"
PROFILE = "xingapi-video"
CREATE_PATH = "/v1/videos/generations"
MODELS_PATH = "/v1/models"
RATIOS = ("16:9", "9:16")
RESOLUTIONS = ("480p", "720p")
DURATIONS = tuple(range(4, 16))
DEFAULT_MAX_IMAGES = 9

# The key that created a task can be rotated or lose a model grant while the
# task keeps running upstream; such an answer says nothing about the task.
CREDENTIAL_REJECTION_STATUS_CODES = frozenset({401, 403})

PROFILE_DEFINITIONS: dict[str, dict[str, Any]] = {
    PROFILE: {
        "label": "XingAPI · 视频",
        "request_format": PROFILE,
        "capabilities": {
            "ratios": list(RATIOS),
            "durations": list(DURATIONS),
            "resolutions": list(RESOLUTIONS),
            "maxImages": 10,
            "referenceVideo": False,
            "maxAudios": 3,
            "experimental": True,
        },
    }
}

# Media the documented backends accept: (images, videos, audios).
_MEDIA_BY_MODEL: dict[str, tuple[int, int, int]] = {
    "seedance2.0-mini": (10, 0, 3),
    "seedance-2.0-933x": (DEFAULT_MAX_IMAGES, 0, 3),
    "seedance-2.0-mini": (DEFAULT_MAX_IMAGES, 0, 3),
}
_VIDEO_KEYWORDS = ("seedance", "happyhorse", "快乐马", "video", "veo", "kling", "sora", "vidu", "hailuo", "wan")
_DURATION_IN_NAME = re.compile(r"-(\d{1,2})s(?:$|-)")
_SIZE = re.compile(r"^\s*(\d{2,5})\s*[x*×]\s*(\d{2,5})\s*$", re.IGNORECASE)

_STATUS = {
    "queued": "queued",
    "pending": "queued",
    "submitted": "queued",
    "not_start": "queued",
    "running": "processing",
    "processing": "processing",
    "in_progress": "processing",
    "success": "completed",
    "completed": "completed",
    "succeeded": "completed",
    "done": "completed",
    "finished": "completed",
    "failed": "failed",
    "failure": "failed",
    "error": "failed",
    "cancelled": "failed",
    "canceled": "failed",
}


class XingApiRequestError(ValueError):
    """The request cannot be sent to XingAPI as asked."""


def is_xingapi_base_url(base_url: str) -> bool:
    try:
        hostname = (urlsplit(base_url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return hostname == "xingapi.top" or hostname.endswith(".xingapi.top")


def api_url(base_url: str, path: str) -> str:
    """Join a ``/v1/...`` path onto a base URL that may already end in ``/v1``."""
    normalized = base_url.rstrip("/")
    if normalized.lower().endswith("/v1") and path.startswith("/v1/"):
        normalized = normalized[:-3]
    return normalized + path


def is_video_model(model: str, endpoint_types: Any = None) -> bool:
    """Keep video models; the same key can also list chat and image models."""
    if isinstance(endpoint_types, list) and endpoint_types:
        return any(isinstance(item, str) and "video" in item.lower() for item in endpoint_types)
    normalized = model.strip().lower()
    return any(keyword in normalized for keyword in _VIDEO_KEYWORDS)


def name_duration(model: str) -> int | None:
    """Return the fixed length a name such as ``快乐马1.1-5S`` promises."""
    match = _DURATION_IN_NAME.search(model.strip().lower())
    if not match:
        return None
    duration = int(match.group(1))
    return duration if 1 <= duration <= 60 else None


def suggest_route(model: str) -> dict[str, Any]:
    normalized = model.strip().lower()
    images, videos, audios = _MEDIA_BY_MODEL.get(normalized, (DEFAULT_MAX_IMAGES, 0, 0))
    duration = name_duration(model)
    return {
        "profile": PROFILE,
        "durations": [duration] if duration else list(DURATIONS),
        "resolutions": list(RESOLUTIONS),
        "image_count": images,
        "video_count": videos,
        "audio_count": audios,
        "supports_image": images > 0,
        "supports_video": videos > 0,
        "supports_audio": audios > 0,
    }


def transform_create_payload(
    payload: dict[str, Any], allowed_durations: list[int] | None = None
) -> dict[str, Any]:
    """Convert canonical video fields to XingAPI's documented request body."""
    model = str(payload.get("model") or "").strip()
    if not model:
        raise XingApiRequestError("model is required")
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    size = payload.get("size") or metadata.get("size")

    result: dict[str, Any] = {
        "model": model,
        "prompt": payload.get("prompt"),
        "duration": _duration(payload, metadata, allowed_durations or []),
    }
    ratio = (
        payload.get("ratio")
        or payload.get("aspect_ratio")
        or payload.get("aspectRatio")
        or metadata.get("ratio")
        or metadata.get("aspect_ratio")
        or _size_ratio(size)
    )
    if ratio not in (None, ""):
        result["ratio"] = str(ratio).strip()
    resolution = payload.get("resolution") or metadata.get("resolution") or _size_resolution(size)
    if resolution not in (None, ""):
        result["resolution"] = str(resolution).strip().lower()

    media = {
        "images": _media_urls(
            payload,
            ("images", "image_urls", "reference_image_urls", "reference_images"),
            ("image_url", "image", "input_reference"),
        ),
        "videos": _media_urls(
            payload,
            ("videos", "reference_videos", "video_urls", "videoUrls"),
            ("reference_video", "video_url"),
        ),
        "audios": _media_urls(
            payload,
            ("audios", "audio_urls", "reference_audios"),
            ("audio_url", "reference_audio"),
        ),
    }
    for field, urls in media.items():
        for url in urls:
            if urlsplit(url).scheme.lower() not in {"http", "https"}:
                raise XingApiRequestError(
                    f"{field} 必须是公网可访问的 http(s) 直链，不支持 data:、Base64 或本地文件"
                )
        if urls:
            result[field] = urls
    return result


def _duration(payload: dict[str, Any], metadata: dict[str, Any], allowed: list[int]) -> int:
    for value in (
        payload.get("duration"),
        payload.get("seconds"),
        metadata.get("duration"),
        metadata.get("seconds"),
    ):
        if value is None or value == "" or isinstance(value, bool):
            continue
        try:
            return int(float(str(value).strip().rstrip("sS")))
        except ValueError as error:
            raise XingApiRequestError(f"duration 必须是整数秒，收到 {value!r}") from error
    # The upstream requires the length; only a route that offers a single
    # length may supply it on the caller's behalf.
    if len(allowed) == 1:
        return int(allowed[0])
    raise XingApiRequestError("duration 为必填参数（视频秒数）")


def _size_dimensions(size: Any) -> tuple[int, int] | None:
    match = _SIZE.match(size) if isinstance(size, str) else None
    return (int(match.group(1)), int(match.group(2))) if match else None


def _size_resolution(size: Any) -> str | None:
    dimensions = _size_dimensions(size)
    return f"{min(dimensions)}p" if dimensions else None


def _size_ratio(size: Any) -> str | None:
    dimensions = _size_dimensions(size)
    if not dimensions:
        return None
    width, height = dimensions
    if width * 9 == height * 16:
        return "16:9"
    if width * 16 == height * 9:
        return "9:16"
    return None


def task_path(task_id: str) -> str:
    return f"/v1/videos/{quote(task_id, safe='')}"


def content_path(task_id: str) -> str:
    return f"{task_path(task_id)}/content"


def _task_document(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, dict) and any(key in data for key in ("status", "task_id", "id")):
        return data
    return payload


def extract_create_task_id(payload: dict[str, Any]) -> str:
    document = _task_document(payload)
    return str(document.get("task_id") or document.get("id") or "").strip()


def normalize_status(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    return _STATUS.get(normalized, normalized)


def extract_create_status(payload: dict[str, Any]) -> str | None:
    return normalize_status(_task_document(payload).get("status"))


def extract_task_fields(payload: dict[str, Any]) -> dict[str, Any]:
    document = _task_document(payload)
    return {
        "status": normalize_status(document.get("status")),
        # Never stored: the documented download is ``/content`` on the same
        # host with the channel key, so a link in the task body is not used.
        "video_url": None,
        "error": document.get("error") or document.get("fail_reason") or document.get("message"),
        "progress": _progress(document.get("progress")),
    }


def _progress(value: Any) -> Any:
    if isinstance(value, str) and value.strip().endswith("%"):
        return value.strip()[:-1]
    return value


def _media_urls(
    payload: dict[str, Any],
    list_keys: tuple[str, ...],
    single_keys: tuple[str, ...],
) -> list[str]:
    result: list[str] = []
    for key in single_keys:
        if (url := _as_url(payload.get(key))) and url not in result:
            result.append(url)
    for key in list_keys:
        value = payload.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if (url := _as_url(item)) and url not in result:
                result.append(url)
    return result


def _as_url(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, dict):
        return None
    for key in ("url", "image_url", "video_url", "audio_url"):
        nested = value.get(key)
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
        if isinstance(nested, dict) and isinstance(nested.get("url"), str) and nested["url"].strip():
            return nested["url"].strip()
    return None
