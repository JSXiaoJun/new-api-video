"""Adapter for Sub2API's asynchronous Grok video API.

This API must remain separate from :mod:`o10_grok`: Sub2API creates tasks
at ``/v1/videos`` and accepts reference images as an ``images`` URL array,
whereas the o10-compatible API uses ``/v1/videos/generations`` and ``image``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit


PROTOCOL = "sub2api-video"
PROFILE = "sub2api-video"
CREATE_PATH = "/v1/videos"
KNOWN_MODELS = ("grok-imagine-video", "grok-imagine-video-1.5")

PROFILE_DEFINITIONS: dict[str, dict[str, Any]] = {
    PROFILE: {
        "label": "Sub2API · Grok Video",
        "request_format": PROFILE,
        "capabilities": {
            "ratios": ["16:9", "9:16", "1:1", "4:3", "3:4", "2:3", "3:2"],
            "durations": list(range(1, 16)),
            "resolutions": ["480p", "720p", "1080p"],
            "maxImages": 7,
            "referenceVideo": False,
            "maxAudios": 0,
            "experimental": True,
        },
    }
}


def is_sub2api_base_url(base_url: str) -> bool:
    """Return whether the known PandatK Sub2API endpoint is configured."""
    try:
        hostname = (urlsplit(base_url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return hostname == "api.pandatk.com"


def suggest_route(model: str) -> dict[str, Any] | None:
    normalized = model.strip().lower()
    if normalized == "grok-imagine-video":
        # The provider rejects 1080p for this model in live validation.
        return _route(["480p", "720p"], 0)
    if normalized == "grok-imagine-video-1.5":
        return _route(["480p", "720p", "1080p"], 7)
    return None


def _route(resolutions: list[str], image_count: int) -> dict[str, Any]:
    return {
        "profile": PROFILE,
        "durations": list(range(1, 16)),
        "resolutions": resolutions,
        "image_count": image_count,
        "supports_image": image_count > 0,
        "supports_video": False,
        "supports_audio": False,
    }


def transform_create_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert canonical video fields to Sub2API's documented request body."""
    model = str(payload.get("model") or "").strip()
    if model not in KNOWN_MODELS:
        raise ValueError(f"Unsupported Sub2API video model: {model}")

    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    result: dict[str, Any] = {"model": model, "prompt": payload.get("prompt")}
    duration = payload.get("duration") if payload.get("duration") is not None else payload.get("seconds")
    if duration not in (None, ""):
        result["duration"] = duration
    ratio = (
        payload.get("aspect_ratio")
        or payload.get("aspectRatio")
        or payload.get("ratio")
        or metadata.get("aspect_ratio")
        or metadata.get("ratio")
    )
    if ratio not in (None, ""):
        result["ratio"] = ratio
    size = payload.get("resolution") or payload.get("size") or metadata.get("resolution")
    if size not in (None, ""):
        result["size"] = size

    # Per the documented contract, only 1.5 supports image-to-video and it
    # consumes public image URLs in a top-level array. Never leak the o10
    # ``image`` object into this request.
    if model == "grok-imagine-video-1.5":
        images = _media_urls(
            payload,
            ("images", "image_urls", "reference_image_urls", "reference_images"),
            ("image_url", "image", "input_reference"),
        )
        if images:
            result["images"] = images[:7]
    return result


def task_path(task_id: str) -> str:
    return f"/v1/videos/{quote(task_id, safe='')}"


def content_path(task_id: str) -> str:
    return f"{task_path(task_id)}/content"


def extract_create_task_id(payload: dict[str, Any]) -> str:
    return str(payload.get("task_id") or payload.get("id") or "").strip()


def extract_task_fields(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": payload.get("status"),
        "video_url": payload.get("video_url") or payload.get("url"),
        "error": payload.get("error") or payload.get("message"),
        "progress": payload.get("progress"),
    }


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
        if result:
            break
    return result


def _as_url(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, dict):
        return None
    direct = value.get("url")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    nested = value.get("image_url")
    if isinstance(nested, str) and nested.strip():
        return nested.strip()
    if isinstance(nested, dict) and isinstance(nested.get("url"), str) and nested["url"].strip():
        return nested["url"].strip()
    return None
