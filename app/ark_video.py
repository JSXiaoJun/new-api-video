from __future__ import annotations

from typing import Any
from urllib.parse import quote


PROTOCOL = "ark-v3"
CREATE_PATH = "/api/v3/contents/generations/tasks"


def task_path(task_id: str) -> str:
    return f"{CREATE_PATH}/{quote(task_id, safe='')}"


def has_reference_content(payload: dict[str, Any]) -> bool:
    return bool(_image_urls(payload) or _video_urls(payload) or _audio_urls(payload))


def transform_create_payload(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    prompt = str(payload.get("prompt") or "").strip()
    content: list[dict[str, Any]] = []
    if prompt:
        content.append({"type": "text", "text": prompt})
    content.extend(
        {
            "type": "image_url",
            "image_url": {"url": url},
            "role": "reference_image",
        }
        for url in _image_urls(payload)
    )
    content.extend(
        {
            "type": "video_url",
            "video_url": {"url": url},
            "role": "reference_video",
        }
        for url in _video_urls(payload)
    )
    content.extend(
        {
            "type": "audio_url",
            "audio_url": {"url": url},
            "role": "reference_audio",
        }
        for url in _audio_urls(payload)
    )

    known_fields = {
        "model", "prompt", "aspect_ratio", "duration", "seconds", "resolution", "generate_audio",
        "image_url", "image_urls", "images", "reference_image_urls", "reference_images",
        "reference_video", "reference_videos", "audio_url", "audio_urls", "metadata", "content",
    }
    extra = {key: value for key, value in payload.items() if key not in known_fields}
    ratio = payload.get("aspect_ratio") or metadata.get("ratio")
    duration = payload.get("duration") if payload.get("duration") is not None else payload.get("seconds")
    resolution = payload.get("resolution") or metadata.get("resolution")
    return {
        **extra,
        "model": payload.get("model"),
        "content": content,
        **({"ratio": ratio} if ratio else {}),
        **({"duration": duration} if duration is not None else {}),
        **({"resolution": resolution} if resolution else {}),
        **({"generate_audio": payload["generate_audio"]} if "generate_audio" in payload else {}),
    }


def extract_task_fields(payload: dict[str, Any]) -> dict[str, Any]:
    content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
    return {
        "status": payload.get("status"),
        "video_url": content.get("video_url"),
        "error": payload.get("error"),
        "progress": payload.get("progress"),
    }


def _string_urls(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _image_urls(payload: dict[str, Any]) -> list[str]:
    images = _string_urls(payload.get("image_urls")) or _string_urls(payload.get("images"))
    if images:
        return images
    image_url = payload.get("image_url")
    if not isinstance(image_url, str) or not image_url.strip():
        return []
    return [
        image_url.strip(),
        *_string_urls(payload.get("reference_image_urls")),
        *_string_urls(payload.get("reference_images")),
    ]


def _video_urls(payload: dict[str, Any]) -> list[str]:
    videos = _string_urls(payload.get("reference_videos"))
    if videos:
        return videos
    reference_video = payload.get("reference_video")
    return [reference_video.strip()] if isinstance(reference_video, str) and reference_video.strip() else []


def _audio_urls(payload: dict[str, Any]) -> list[str]:
    audios = _string_urls(payload.get("audio_urls"))
    if audios:
        return audios
    audio_url = payload.get("audio_url")
    return [audio_url.strip()] if isinstance(audio_url, str) and audio_url.strip() else []
