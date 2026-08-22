"""Adapter for the o10.top Grok Imagine video API."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit


PROTOCOL = "o10-grok"
CREATE_PATH = "/v1/videos/generations"
KNOWN_MODELS = ("grok-imagine-video", "grok-imagine-video-1.5")


def is_o10_base_url(base_url: str) -> bool:
    try:
        hostname = (urlsplit(base_url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return hostname == "o10.top" or hostname.endswith(".o10.top")


def suggest_route(model: str) -> dict[str, Any] | None:
    if not model.strip().lower().startswith("grok-imagine-video"):
        return None
    return {
        "profile": "grok-auto",
        "durations": list(range(1, 16)),
        "resolutions": ["480p", "720p"],
        "image_count": 1,
        "supports_image": True,
        "supports_video": False,
        "supports_audio": False,
    }


def transform_create_payload(payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"model": payload.get("model"), "prompt": payload.get("prompt")}
    for field in ("duration", "aspect_ratio", "resolution"):
        value = payload.get(field)
        if value is None and field == "duration":
            value = payload.get("seconds")
        if value is not None and value != "":
            result[field] = value

    image = payload.get("image")
    if isinstance(image, dict) and isinstance(image.get("url"), str) and image["url"].strip():
        result["image"] = {"url": image["url"].strip()}
    else:
        image_url = payload.get("image_url")
        if not image_url and isinstance(payload.get("image_urls"), list):
            image_url = next((item for item in payload["image_urls"] if isinstance(item, str) and item.strip()), None)
        if isinstance(image_url, str) and image_url.strip():
            result["image"] = {"url": image_url.strip()}
    return result


def task_path(task_id: str) -> str:
    return f"/v1/videos/{quote(task_id, safe='')}"


def content_path(task_id: str) -> str:
    return f"{task_path(task_id)}/content"


def extract_create_task_id(payload: dict[str, Any]) -> str:
    return str(payload.get("request_id") or payload.get("task_id") or payload.get("id") or "").strip()


def extract_task_fields(payload: dict[str, Any]) -> dict[str, Any]:
    status = str(payload.get("status") or "pending").strip().lower()
    status = {
        "pending": "processing",
        "processing": "processing",
        "done": "completed",
        "completed": "completed",
        "failed": "failed",
        "failure": "failed",
    }.get(status, status)
    video = payload.get("video") if isinstance(payload.get("video"), dict) else {}
    video_url = video.get("url") if isinstance(video.get("url"), str) else None
    return {
        "status": status,
        "video_url": video_url,
        "error": payload.get("error") or payload.get("message"),
        "progress": payload.get("progress"),
    }
