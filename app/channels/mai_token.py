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
* the upstream expects an explicit ``resolution`` (``480p`` / ``720p`` /
  ``1080p``); the model name alone is not enough, so it is always sent
* at most 9 images, 3 audios, and 3 videos; first/last frames share the
  image budget
"""

from __future__ import annotations

import math
import re
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

# The catalog documented by MAI Token. Only used to recognise the channel and
# as the emergency fallback when the upstream model list cannot be read; new
# models are discovered from ``GET /v1/models`` instead of this table.
_MODEL_RESOLUTIONS = {
    "sd-2.0-1080p": "1080p",
    "sd-2.0-720p": "720p",
    "sd-2.0-480p": "480p",
    "sd-fast-720p": "720p",
    "sd-fast-480p": "480p",
    "sd-mini-720p": "720p",
    "sd-mini-480p": "480p",
}

# The upstream names every tier with a trailing resolution suffix and derives
# the output size from the model name alone, so an unrecognised name can still
# be routed by its suffix.
_RESOLUTION_SUFFIXES = (
    ("-1080p", "1080p"),
    ("-720p", "720p"),
    ("-480p", "480p"),
)
_RESOLUTION_SUFFIXES_VALUES = tuple(resolution for _, resolution in _RESOLUTION_SUFFIXES)
_RESOLUTION_LABEL = '、'.join(_RESOLUTION_SUFFIXES_VALUES)

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


class MaiTokenRequestError(Exception):
    """请求超出了 MAI Token 文档写明的范围，不能靠改写请求来迁就。"""


def _check_limits(images: int, audios: int, videos: int) -> None:
    """超出文档上限就拒绝，而不是截断。

    截断等于把调用方的素材悄悄扔掉：用户传了 12 张参考图、付了整单的钱，却只
    有 9 张进了上游。路由配置的上限（``/v1/model-capabilities`` 公布的
    ``maxImages``）在代理层已经拦过一次，这里是渠道自身的硬上限兜底——运营把
    数量填大时也不会让素材无声消失。
    """
    for count, limit, label, unit in (
        (images, MAX_IMAGES, '图片', '张'),
        (audios, MAX_AUDIOS, '音频', '段'),
        (videos, MAX_VIDEOS, '视频', '段'),
    ):
        if count > limit:
            raise MaiTokenRequestError(
                f'MAI Token 每个任务最多 {limit} {unit}{label}，本次请求 {count} {unit}'
            )


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
    resolution = resolve_resolution(model)
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


def resolve_resolution(model: str) -> str | None:
    """Map a model name to its output tier, or ``None`` for a foreign name.

    Known names resolve through the documented table. Anything else must still
    resolve by suffix: the upstream derives the output size from the model name,
    so a newly published tier such as ``sd-3.0-720p`` has to keep routing to
    ``mai-token-720p`` without a code change. ``None`` means the name is not an
    MAI Token video model at all.
    """
    normalized = model.strip().lower()
    if not normalized:
        return None
    known = _MODEL_RESOLUTIONS.get(normalized)
    if known is not None:
        return known
    for suffix, resolution in _RESOLUTION_SUFFIXES:
        if normalized.endswith(suffix):
            return resolution
    return None


def is_known_model(model: str) -> bool:
    """Return whether the name is one this channel documents.

    Protocol detection must stay strict and use this rather than
    ``suggest_route``: the suffix rule is deliberately loose, so feeding it to
    detection would let another channel's model -- ``v1-seedance-2.0-720p``
    also ends in ``-720p`` -- be mistaken for a MAI Token one.
    """
    return model.strip().lower() in _MODEL_RESOLUTIONS


def transform_create_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Build the documented ``content[]`` request body.

    Only declared MAI Token fields are emitted. ``resolution`` is always sent:
    the upstream requires an explicit tier and does not reliably infer it from
    the model name, so dropping it made every request fall back to the upstream
    default (a 1080p route silently produced 720p). The route-level check in
    ``proxy.create_video`` has already rejected a tier the model does not
    publish; here the value is normalized to the documented spelling and, when
    the caller omitted it, derived from the model suffix.
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
    audios = _media_urls(payload, _AUDIO_KEYS, _SINGLE_AUDIO_KEYS)
    videos = _media_urls(payload, _VIDEO_KEYS, _SINGLE_VIDEO_KEYS)
    # The documented budget covers first_frame and last_frame too, so the frame
    # URLs are counted before the media is trimmed or rejected.
    ordered_images = _dedupe([url for _, url in frames] + images + references)
    _check_limits(len(ordered_images), len(audios), len(videos))

    role_by_url = {url: role for role, url in frames}
    for url in ordered_images:
        content.append(_image_item(role_by_url.get(url, "reference_image"), url))

    for url in audios:
        content.append({"type": "audio_url", "role": "reference_audio", "audio_url": {"url": url}})

    for url in videos:
        content.append({"type": "video_url", "role": "reference_video", "video_url": {"url": url}})

    result: dict[str, Any] = {
        "model": payload.get("model"),
        "prompt": payload.get("prompt"),
        "content": content,
    }
    seconds = _seconds(payload)
    if seconds:
        result["seconds"] = seconds
    resolution = _resolution(payload)
    if resolution:
        if resolution not in _RESOLUTION_SUFFIXES_VALUES:
            raise MaiTokenRequestError(
                f'MAI Token 只支持分辨率 {_RESOLUTION_LABEL}，收到 {resolution}'
            )
        result["resolution"] = resolution
    ratio = _ratio(payload)
    if ratio:
        if ratio not in RATIOS:
            raise MaiTokenRequestError(f'MAI Token 只支持画面比例 {"、".join(RATIOS)}，收到 {ratio}')
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
    """Return the documented ``seconds`` string, refusing a length out of range.

    The upstream only accepts 4–15 seconds. Sending anything else either fails
    the create or gets replaced by the default length, and a replaced length is
    billed as a different task than the caller asked for.
    """
    value = payload.get("duration")
    if value is None:
        value = payload.get("seconds")
    if value is None or value == "":
        return None
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        raise MaiTokenRequestError(f'时长必须是整数秒，收到 {value!r}') from None
    if not MIN_SECONDS <= seconds <= MAX_SECONDS:
        raise MaiTokenRequestError(
            f'MAI Token 只支持 {MIN_SECONDS}–{MAX_SECONDS} 秒，收到 {seconds} 秒'
        )
    return str(seconds)


def _resolution(payload: dict[str, Any]) -> str | None:
    """Return the tier to send upstream, falling back to the model suffix.

    The upstream wants an explicit ``resolution``. When the caller omitted it
    the model name still encodes the tier (``sdg-2.5-1080p``), so the request
    stays correct for that route instead of letting the upstream choose.
    ``quality`` only counts when it is written like a tier: ``high`` is a
    quality word for other channels and must not turn into a resolution here.
    """
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    parameters = metadata.get("parameters") if isinstance(metadata.get("parameters"), dict) else {}
    fields = (
        ("resolution", payload.get("resolution")),
        ("metadata.resolution", metadata.get("resolution")),
        ("metadata.parameters.resolution", parameters.get("resolution")),
        ("quality", payload.get("quality")),
    )
    for field, value in fields:
        if not isinstance(value, str) or not value.strip():
            continue
        normalized = _normalize_resolution(value)
        if normalized in _RESOLUTION_SUFFIXES_VALUES:
            return normalized
        if field != "quality":
            # The field is meant to hold a tier; report the value as given and
            # let the caller's error name what the upstream accepts.
            return normalized
    return resolve_resolution(str(payload.get("model") or ""))


def _normalize_resolution(value: str) -> str:
    """``720P`` / ``1280x720`` → ``720p`` so the upstream sees one spelling."""
    normalized = value.strip().lower()
    match = re.fullmatch(r"(\d{3,4})p", normalized)
    if match is not None:
        return f"{match.group(1)}p"
    size = re.fullmatch(r"(\d{2,5})\s*[x*×]\s*(\d{2,5})", normalized)
    if size is not None:
        return f"{min(int(size.group(1)), int(size.group(2)))}p"
    return normalized


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
