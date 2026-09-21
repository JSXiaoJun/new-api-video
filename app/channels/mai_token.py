"""Adapter for the MAI Token Seedance asynchronous video API.

MAI Token is a ``content[]`` multimodal upstream: every request carries the
prompt twice (top-level ``prompt`` plus the leading ``content`` text item) and
reference media as ``image_url`` / ``audio_url`` / ``video_url`` elements.
That shape is incompatible with the flat ``videos`` request body, so this
adapter stays isolated from the other channels and never forwards undeclared
client fields.

Documented contract (https://api.mai-token.com):

* create ``POST /v1/videos``
* poll ``GET /v1/videos/{task_id}``
* download ``GET /v1/videos/{task_id}/content``
* ``seconds`` is a string from ``"4"`` to ``"15"``
* the output resolution comes from the model name, so ``resolution`` is never
  forwarded
* at most 9 images, 3 audios, and 3 videos; first/last frames share the
  image budget
"""

from __future__ import annotations

import math
from typing import Any
from urllib.parse import quote, urlsplit


PROTOCOL = "mai-token"
PROFILE = "mai-token"
CREATE_PATH = "/v1/videos"

MAX_IMAGES = 9
MAX_AUDIOS = 3
MAX_VIDEOS = 3
MIN_SECONDS = 4
MAX_SECONDS = 15

RATIOS = ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"]
DURATIONS = list(range(MIN_SECONDS, MAX_SECONDS + 1))

KNOWN_MODELS = (
    "sd-2.0-1080p",
    "sd-2.0-720p",
    "sd-2.0-480p",
    "sd-fast-720p",
    "sd-fast-480p",
    "sd-mini-720p",
    "sd-mini-480p",
)

_MODEL_RESOLUTIONS = {
    "sd-2.0-1080p": "1080p",
    "sd-2.0-720p": "720p",
    "sd-2.0-480p": "480p",
    "sd-fast-720p": "720p",
    "sd-fast-480p": "480p",
    "sd-mini-720p": "720p",
    "sd-mini-480p": "480p",
}

_PROFILE_BY_RESOLUTION = {
    "1080p": "mai-token-1080p",
    "720p": "mai-token-720p",
    "480p": "mai-token-480p",
}

_IMAGE_KEYS = ("image_urls", "images")
_SINGLE_IMAGE_KEYS = ("image_url",)
_REFERENCE_IMAGE_KEYS = ("reference_image_urls", "reference_images")
_AUDIO_KEYS = ("audio_urls", "audios")
_SINGLE_AUDIO_KEYS = ("audio_url",)
_VIDEO_KEYS = ("reference_video_urls", "reference_videos", "video_urls")
_SINGLE_VIDEO_KEYS = ("reference_video", "video_url")


def _capabilities(resolution: str) -> dict[str, Any]:
    return {
        "ratios": list(RATIOS),
        "durations": list(DURATIONS),
        "resolutions": [resolution],
        "maxImages": MAX_IMAGES,
        "referenceVideo": True,
        "maxVideos": MAX_VIDEOS,
        "maxAudios": MAX_AUDIOS,
        "maxReferences": MAX_IMAGES + MAX_AUDIOS + MAX_VIDEOS,
    }


PROFILE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "mai-token-1080p": {
        "label": "MAI Token · Seedance 1080p",
        "request_format": PROFILE,
        "capabilities": _capabilities("1080p"),
    },
    "mai-token-720p": {
        "label": "MAI Token · Seedance 720p",
        "request_format": PROFILE,
        "capabilities": _capabilities("720p"),
    },
    "mai-token-480p": {
        "label": "MAI Token · Seedance 480p",
        "request_format": PROFILE,
        "capabilities": _capabilities("480p"),
    },
}


def is_mai_token_base_url(base_url: str) -> bool:
    """Return whether the known MAI Token endpoint is configured."""
    try:
        hostname = (urlsplit(base_url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return hostname == "api.mai-token.com"


def suggest_route(model: str) -> dict[str, Any] | None:
    resolution = _MODEL_RESOLUTIONS.get(model.strip().lower())
    if resolution is None:
        return None
    return {
        "profile": _PROFILE_BY_RESOLUTION[resolution],
        "durations": list(DURATIONS),
        "resolutions": [resolution],
        "image_count": MAX_IMAGES,
        "supports_image": True,
        "supports_video": True,
        "supports_audio": True,
    }


def transform_create_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Build the documented ``content[]`` request body.

    Only declared MAI Token fields are emitted. Undeclared client fields are
    dropped on purpose: the upstream rejects a ``resolution`` that conflicts
    with the model suffix, while ``seed`` and ``generate_audio`` are forwarded
    explicitly because they are part of the documented contract.
    """
    prompt = str(payload.get("prompt") or "").strip()
    content: list[dict[str, Any]] = []
    if prompt:
        content.append({"type": "text", "text": prompt})

    frames = [
        (role, url)
        for role, url in (
            (
                "first_frame",
                _first_url(
                    payload, "first_image", "first_frame_url", "first_frame_image", "first_frame"
                ),
            ),
            (
                "last_frame",
                _first_url(
                    payload, "last_image", "last_frame_url", "last_frame_image", "last_frame"
                ),
            ),
        )
        if url
    ]
    images = _media_urls(payload, _IMAGE_KEYS, _SINGLE_IMAGE_KEYS)
    references = _media_urls(payload, _REFERENCE_IMAGE_KEYS, ())
    # The documented 9-image budget covers first_frame and last_frame too.
    role_by_url = {url: role for role, url in frames}
    for url in _dedupe([url for _, url in frames] + images + references)[:MAX_IMAGES]:
        content.append(_image_item(role_by_url.get(url, "reference_image"), url))

    for url in _media_urls(payload, _AUDIO_KEYS, _SINGLE_AUDIO_KEYS)[:MAX_AUDIOS]:
        content.append({"type": "audio_url", "role": "reference_audio", "audio_url": {"url": url}})

    for url in _media_urls(payload, _VIDEO_KEYS, _SINGLE_VIDEO_KEYS)[:MAX_VIDEOS]:
        content.append({"type": "video_url", "role": "reference_video", "video_url": {"url": url}})

    result: dict[str, Any] = {
        "model": payload.get("model"),
        "prompt": payload.get("prompt"),
        "content": content,
    }
    seconds = _seconds(payload)
    if seconds:
        result["seconds"] = seconds
    ratio = _ratio(payload)
    if ratio:
        result["ratio"] = ratio
    if payload.get("generate_audio") is not None:
        result["generate_audio"] = payload["generate_audio"]
    elif payload.get("generateAudio") is not None:
        result["generate_audio"] = payload["generateAudio"]
    if payload.get("seed") is not None:
        result["seed"] = payload["seed"]
    return result


def extract_create_task_id(payload: dict[str, Any]) -> str:
    return str(payload.get("id") or payload.get("task_id") or "").strip()


def extract_task_fields(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": payload.get("status"),
        "video_url": payload.get("video_url") or payload.get("url"),
        "error": payload.get("error"),
        "progress": payload.get("progress"),
    }


def task_path(task_id: str) -> str:
    return f"{CREATE_PATH}/{quote(task_id, safe='')}"


def content_path(task_id: str) -> str:
    return f"{task_path(task_id)}/content"


def _image_item(role: str, url: str) -> dict[str, Any]:
    return {"type": "image_url", "role": role, "image_url": {"url": url}}


def _seconds(payload: dict[str, Any]) -> str | None:
    value = payload.get("duration")
    if value is None:
        value = payload.get("seconds")
    if value is None or value == "":
        return None
    try:
        return str(int(float(value)))
    except (TypeError, ValueError):
        return str(value).strip() or None


def _ratio(payload: dict[str, Any]) -> str | None:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    for value in (
        payload.get("aspect_ratio"),
        payload.get("aspectRatio"),
        payload.get("ratio"),
        metadata.get("aspect_ratio"),
        metadata.get("ratio"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _ratio_from_size(payload.get("size") or metadata.get("size"))


def _ratio_from_size(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized.count(":") == 1:
        left, right = normalized.split(":", 1)
    elif "x" in normalized:
        left, right = normalized.split("x", 1)
    else:
        return None
    left, right = left.strip(), right.strip()
    if not left.isdigit() or not right.isdigit() or int(left) <= 0 or int(right) <= 0:
        return None
    divisor = math.gcd(int(left), int(right))
    return f"{int(left) // divisor}:{int(right) // divisor}"


def _media_urls(
    payload: dict[str, Any],
    list_keys: tuple[str, ...],
    single_keys: tuple[str, ...],
) -> list[str]:
    result: list[str] = []
    for key in single_keys:
        url = _as_url(payload.get(key))
        if url:
            result.append(url)
    for key in list_keys:
        value = payload.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            url = _as_url(item)
            if url:
                result.append(url)
        if result:
            break
    return _dedupe(result)


def _first_url(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        url = _as_url(payload.get(key))
        if url:
            return url
    return None


def _as_url(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, dict):
        return None
    for key in ("url", "uri", "href"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    nested = value.get("image_url")
    if isinstance(nested, str) and nested.strip():
        return nested.strip()
    if isinstance(nested, dict) and isinstance(nested.get("url"), str) and nested["url"].strip():
        return nested["url"].strip()
    return None


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result
