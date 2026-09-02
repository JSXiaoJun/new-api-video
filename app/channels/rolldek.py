"""RollDek video API adapter.

RollDek exposes an OpenAI-compatible asynchronous video API, but its CH3
models use ``image_refs`` while CH4 accepts the richer multimodal field set.
Keeping these mappings here prevents RollDek-specific aliases from leaking
into the existing upstream adapters.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


PROTOCOL = "rolldek"
CREATE_PATH = "/v1/videos"

KNOWN_MODELS = (
    "sd-2-ch3",
    "sd-2.5-ch3",
    "sd-2-ch4",
    "sd-2.0-ch4",
)

PROFILE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "rolldek-sd2-ch3": {
        "label": "RollDek · Seedance 2.0 CH3",
        "request_format": "rolldek-ch3",
        "capabilities": {
            "ratios": ["16:9", "9:16", "1:1"],
            "durations": [10],
            "resolutions": ["720p"],
            "maxImages": 9,
            "referenceVideo": False,
            "maxAudios": 0,
        },
    },
    "rolldek-sd25-ch3": {
        "label": "RollDek · Seedance 2.5 CH3",
        "request_format": "rolldek-ch3",
        "capabilities": {
            "ratios": ["16:9", "9:16", "1:1"],
            "durations": [30],
            "resolutions": ["720p"],
            "maxImages": 9,
            "referenceVideo": False,
            "maxAudios": 0,
        },
    },
    "rolldek-sd2-ch4": {
        "label": "RollDek · Seedance 2.0 CH4",
        "request_format": "rolldek-ch4",
        "capabilities": {
            "ratios": ["16:9", "9:16", "1:1"],
            "durations": list(range(1, 61)),
            "resolutions": ["720p"],
            "maxImages": 9,
            "referenceVideo": True,
            "maxReferenceVideoDuration": 30,
            "maxAudios": 3,
            "maxReferences": 15,
        },
    },
}


def is_rolldek_base_url(base_url: str) -> bool:
    try:
        hostname = (urlsplit(base_url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return hostname == "rolldek.com" or hostname.endswith(".rolldek.com")


def suggest_route(model: str) -> dict[str, Any] | None:
    normalized = model.strip().lower()
    if normalized == "sd-2-ch3":
        return _route("rolldek-sd2-ch3", [10], 9, False, False)
    if normalized == "sd-2.5-ch3":
        return _route("rolldek-sd25-ch3", [30], 9, False, False)
    if normalized in {"sd-2-ch4", "sd-2.0-ch4"}:
        return _route("rolldek-sd2-ch4", list(range(1, 61)), 9, True, True)
    return None


def _route(
    profile: str,
    durations: list[int],
    image_count: int,
    supports_video: bool,
    supports_audio: bool,
) -> dict[str, Any]:
    return {
        "profile": profile,
        "durations": durations,
        "image_count": image_count,
        "supports_image": image_count > 0,
        "supports_video": supports_video,
        "supports_audio": supports_audio,
    }


def transform_create_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Map the gateway's canonical media fields to RollDek's public fields."""
    model = str(payload.get("model") or "").strip().lower()
    duration = payload.get("duration") if payload.get("duration") is not None else payload.get("seconds")
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    ratio = (
        payload.get("aspect_ratio")
        or payload.get("aspectRatio")
        or payload.get("ratio")
        or metadata.get("aspect_ratio")
    )
    resolution = payload.get("resolution") or metadata.get("resolution")
    images = _urls(payload, ("image_refs", "images", "image_urls", "reference_image_urls"), ("image_url",))
    videos = _urls(payload, ("videos", "video_refs", "video_urls", "reference_videos"), ("reference_video",))
    audios = _urls(payload, ("audios", "audio_refs", "audio_urls", "reference_audios"), ("audio_url",))
    first_image = _first(payload, "first_image", "start_image_url", "first_frame_url", "first_frame")
    last_image = _first(payload, "last_image", "end_image_url", "last_frame_url", "last_frame")

    consumed = {
        "model", "prompt", "duration", "seconds", "aspect_ratio", "aspectRatio", "ratio", "resolution",
        "size", "generate_audio", "generateAudio", "metadata", "image_refs", "images", "image_urls",
        "reference_image_urls", "image_url", "image", "videos", "video_refs", "video_urls",
        "reference_videos", "reference_video", "audios", "audio_refs", "audio_urls", "reference_audios",
        "audio_url", "first_image", "start_image_url", "first_frame_url", "first_frame", "last_image",
        "end_image_url", "last_frame_url", "last_frame", "imageUrls", "videoUrls", "audioUrls",
        "idempotency_key",
    }
    extra = {key: value for key, value in payload.items() if key not in consumed}
    result: dict[str, Any] = {**extra, "model": payload.get("model"), "prompt": payload.get("prompt")}

    # CH3 has a deliberately small contract and rejects video/audio inputs.
    if model in {"sd-2-ch3", "sd-2.5-ch3"}:
        # RollDek ignores client supplied duration for these fixed-length models;
        # sending the canonical value also makes the request self-documenting.
        result["duration"] = 10 if model == "sd-2-ch3" else 30
        if ratio:
            result["aspect_ratio"] = ratio
        elif payload.get("size"):
            result["size"] = payload["size"]
        if images:
            result["image_refs"] = images[:9]
        return result

    if duration is not None:
        result["duration"] = duration
    if resolution:
        result["resolution"] = resolution
    if ratio:
        result["aspect_ratio"] = ratio
    elif payload.get("size"):
        result["size"] = payload["size"]
    if payload.get("generate_audio") is not None:
        result["generateAudio"] = payload["generate_audio"]
    elif payload.get("generateAudio") is not None:
        result["generateAudio"] = payload["generateAudio"]
    if images:
        result["images"] = images[:9]
    if videos:
        result["videos"] = videos[:3]
    if audios:
        result["audios"] = audios[:3]
    if first_image:
        result["first_image"] = first_image
    if last_image:
        result["last_image"] = last_image
    return result


def extract_create_task_id(payload: dict[str, Any]) -> str:
    return str(payload.get("task_id") or payload.get("id") or "").strip()


def extract_task_fields(payload: dict[str, Any]) -> dict[str, Any]:
    error = payload.get("error")
    return {
        "status": payload.get("status"),
        "video_url": payload.get("video_url") or payload.get("url"),
        "error": error,
        "progress": payload.get("progress"),
    }


def task_path(task_id: str) -> str:
    from urllib.parse import quote

    return f"{CREATE_PATH}/{quote(task_id, safe='')}"


def content_path(task_id: str) -> str:
    return f"{task_path(task_id)}/content"


def _first(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _urls(payload: dict[str, Any], list_keys: tuple[str, ...], single_keys: tuple[str, ...]) -> list[str]:
    for key in list_keys:
        value = payload.get(key)
        if isinstance(value, list):
            values = [item.strip() for item in value if isinstance(item, str) and item.strip()]
            if values:
                return values
    value = _first(payload, *single_keys)
    return [value] if value else []
