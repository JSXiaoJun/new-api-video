"""Adapter for AutoDL.Art ComfyUI workflow video APIs."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit


PROTOCOL = "autodl-comfyui"
PROFILE = "autodl-comfyui"
CREATE_PREFIX = "/api/v1/comfyui/comfyui_workflow"
RESULT_PREFIX = f"{CREATE_PREFIX}/result"


MODEL_SPECS: dict[str, dict[str, Any]] = {
    "wan2.2animate-v4-motion_retargeting": {
        "durations": [],
        "resolutions": [],
        "image_count": 1,
        "supports_video": True,
        "supports_audio": False,
        "requires_prompt": False,
    },
    "minimax_h3_image_audio_to_video_v2_15s": {
        "durations": list(range(1, 16)),
        "resolutions": ["480p", "768p"],
        "image_count": 9,
        "supports_video": False,
        "supports_audio": True,
        "requires_prompt": True,
    },
    "minimax_h3_lightx2v_v5_15s": {
        "durations": list(range(1, 16)),
        "resolutions": ["480p", "768p"],
        "image_count": 9,
        "supports_video": False,
        "supports_audio": False,
        "requires_prompt": True,
    },
    "minimax_h3_image_audio_to_video_v2": {
        "durations": list(range(1, 11)),
        "resolutions": ["480p", "768p", "1080p"],
        "image_count": 9,
        "supports_video": False,
        "supports_audio": True,
        "requires_prompt": True,
    },
    "minimax_h3_image_audio_to_video": {
        "durations": list(range(1, 16)),
        "resolutions": ["480p", "768p", "1080p"],
        "image_count": 1,
        "supports_video": False,
        "supports_audio": True,
        "requires_prompt": False,
    },
    "minimax_h3_lightx2v_v5": {
        "durations": list(range(1, 11)),
        "resolutions": ["480p", "768p", "1080p"],
        "image_count": 9,
        "supports_video": False,
        "supports_audio": False,
        "requires_prompt": True,
    },
    "minimax_h3_lightx2v_no_pic": {
        "durations": list(range(1, 16)),
        "resolutions": ["480p", "768p"],
        "image_count": 0,
        "supports_video": False,
        "supports_audio": False,
        "requires_prompt": True,
    },
    "minimax_h3_lightx2v": {
        "durations": list(range(1, 16)),
        "resolutions": ["480p", "768p"],
        "image_count": 2,
        "supports_video": False,
        "supports_audio": False,
        "requires_prompt": True,
    },
}

KNOWN_MODELS = tuple(MODEL_SPECS)

PROFILE_DEFINITIONS: dict[str, dict[str, Any]] = {
    PROFILE: {
        "label": "AutoDL · ComfyUI 工作流",
        "request_format": PROFILE,
        "capabilities": {
            "ratios": ["16:9", "9:16", "1:1"],
            "durations": list(range(1, 16)),
            "resolutions": ["480p", "768p", "1080p"],
            "maxImages": 9,
            "referenceVideo": True,
            "maxAudios": 3,
            "experimental": True,
        },
    }
}


def is_autodl_base_url(base_url: str) -> bool:
    try:
        hostname = (urlsplit(base_url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return hostname in {"autodl.art", "www.autodl.art"}


def suggest_route(model: str) -> dict[str, Any] | None:
    spec = MODEL_SPECS.get(model.strip())
    if spec is None:
        return None
    return {
        "profile": PROFILE,
        "durations": list(spec["durations"]),
        "resolutions": list(spec["resolutions"]),
        "image_count": spec["image_count"],
        "supports_image": spec["image_count"] > 0,
        "supports_video": spec["supports_video"],
        "supports_audio": spec["supports_audio"],
    }


def requires_prompt(workflow_id: str) -> bool:
    spec = MODEL_SPECS.get(workflow_id)
    return True if spec is None else bool(spec["requires_prompt"])


def auth_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def create_path(workflow_id: str) -> str:
    return f"{CREATE_PREFIX}/{quote(workflow_id, safe='')}"


def task_path(task_id: str) -> str:
    return f"{RESULT_PREFIX}/{quote(task_id, safe='')}"


def transform_create_payload(payload: dict[str, Any]) -> dict[str, Any]:
    workflow_id = str(payload.get("model") or "").strip()
    if workflow_id not in MODEL_SPECS:
        raise ValueError(f"Unsupported AutoDL ComfyUI workflow: {workflow_id}")

    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    images = _media_urls(
        payload,
        ("image_urls", "images", "reference_image_urls", "reference_images"),
        ("image_url", "image", "input_reference"),
    )
    audios = _media_urls(
        payload,
        ("audio_urls", "audios", "reference_audios"),
        ("audio_url", "reference_audio"),
    )
    videos = _media_urls(
        payload,
        ("video_urls", "videos", "reference_videos"),
        ("video_url", "reference_video"),
    )
    duration = payload.get("duration") if payload.get("duration") is not None else payload.get("seconds")
    resolution = _provider_resolution(payload, metadata)
    result = _native_fields(payload, workflow_id)

    if workflow_id == "wan2.2animate-v4-motion_retargeting":
        _setdefault(result, "ref_image", _first_value(payload, "ref_image") or _item(images, 0))
        _setdefault(result, "ref_video", _first_value(payload, "ref_video") or _item(videos, 0))
        return result

    if workflow_id == "minimax_h3_image_audio_to_video":
        if resolution:
            result["resolution"] = resolution
        if duration is not None:
            result["audio_duration"] = duration
        _setdefault(result, "ref_image_0", _item(images, 0))
        _setdefault(result, "ref_audio_0", _item(audios, 0))
        return result

    prompt = payload.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        result["prompt"] = prompt.strip()
    if resolution:
        result["resolution"] = resolution

    if workflow_id == "minimax_h3_lightx2v":
        if duration is not None:
            result["duration"] = duration
        first_frame = _first_value(
            payload, "first_frame", "first_frame_url", "first_image", "first_frame_image"
        )
        last_frame = _first_value(
            payload, "last_frame", "last_frame_url", "last_image", "last_frame_image"
        )
        _setdefault(result, "first_frame", _as_url(first_frame) or _item(images, 0))
        _setdefault(result, "last_frame", _as_url(last_frame) or _item(images, 1))
        return result

    if duration is not None:
        result["duration"] = duration
    if workflow_id == "minimax_h3_lightx2v_no_pic":
        return result

    for index, image in enumerate(images[:9]):
        _setdefault(result, f"ref_image_{index}", image)
    if workflow_id in {
        "minimax_h3_image_audio_to_video_v2",
        "minimax_h3_image_audio_to_video_v2_15s",
    }:
        for index, audio in enumerate(audios[:3]):
            _setdefault(result, f"ref_audio_{index}", audio)
    if payload.get("seed") is not None:
        result["seed"] = payload["seed"]
    return result


def extract_create_task_id(payload: dict[str, Any]) -> str:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return str(data.get("task_id") or payload.get("task_id") or payload.get("id") or "").strip()


def extract_create_status(payload: dict[str, Any]) -> Any:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return data.get("status") or payload.get("status") or "QUEUED"


def extract_task_fields(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    status = data.get("status") or payload.get("status") or "QUEUED"
    results = data.get("results") if isinstance(data.get("results"), list) else []
    video_url = next(
        (
            str(item["url"]).strip()
            for item in results
            if isinstance(item, dict)
            and isinstance(item.get("url"), str)
            and item["url"].strip()
            and str(item.get("type") or "video").lower() == "video"
        ),
        None,
    )
    code = str(payload.get("code") or "Success")
    error = data.get("message") or payload.get("msg") or payload.get("message")
    if code.lower() != "success":
        status = "FAILED"
        error = error or f"AutoDL API returned code {code}"
    return {
        "status": status,
        "video_url": video_url,
        "error": error,
        "progress": payload.get("progress"),
    }


def _native_fields(payload: dict[str, Any], workflow_id: str) -> dict[str, Any]:
    allowed: set[str] = set()
    if workflow_id == "wan2.2animate-v4-motion_retargeting":
        allowed.update({"ref_image", "ref_video", "seed"})
    elif workflow_id == "minimax_h3_image_audio_to_video":
        allowed.update({"audio_duration", "resolution", "ref_image_0", "ref_audio_0"})
    elif workflow_id == "minimax_h3_lightx2v":
        allowed.update({"prompt", "duration", "resolution", "first_frame", "last_frame"})
    elif workflow_id == "minimax_h3_lightx2v_no_pic":
        allowed.update({"prompt", "duration", "resolution"})
    else:
        allowed.update({"prompt", "duration", "resolution", "seed"})
        allowed.update({f"ref_image_{index}" for index in range(9)})
        if workflow_id in {
            "minimax_h3_image_audio_to_video_v2",
            "minimax_h3_image_audio_to_video_v2_15s",
        }:
            allowed.update({f"ref_audio_{index}" for index in range(3)})
    result: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in allowed or value in (None, ""):
            continue
        if key.startswith("ref_") or key in {"first_frame", "last_frame"}:
            value = _as_url(value) or value
        result[key] = value
    return result


def _provider_resolution(payload: dict[str, Any], metadata: dict[str, Any]) -> str | None:
    value = payload.get("resolution") or metadata.get("resolution")
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value.endswith(("竖", "横", "(1:1)")):
        return value
    ratio = str(
        payload.get("aspect_ratio")
        or payload.get("aspectRatio")
        or payload.get("ratio")
        or metadata.get("aspect_ratio")
        or metadata.get("ratio")
        or "9:16"
    ).strip()
    suffix = "横" if ratio == "16:9" else "(1:1)" if ratio == "1:1" else "竖"
    return f"{value}{suffix}"


def _media_urls(
    payload: dict[str, Any],
    list_keys: tuple[str, ...],
    single_keys: tuple[str, ...],
) -> list[str]:
    primary = [url for key in single_keys if (url := _as_url(payload.get(key)))]
    references: list[str] = []
    for key in list_keys:
        value = payload.get(key)
        if isinstance(value, list):
            references.extend(url for item in value if (url := _as_url(item)))
            if references:
                break
    result: list[str] = []
    for url in [*primary, *references]:
        if url not in result:
            result.append(url)
    return result


def _as_url(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, dict):
        return None
    direct = value.get("url")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    for key in ("image_url", "video_url", "audio_url"):
        nested = value.get(key)
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
        if isinstance(nested, dict) and isinstance(nested.get("url"), str) and nested["url"].strip():
            return nested["url"].strip()
    return None


def _first_value(payload: dict[str, Any], *keys: str) -> Any:
    return next((payload[key] for key in keys if payload.get(key) not in (None, "")), None)


def _item(values: list[str], index: int) -> str | None:
    return values[index] if index < len(values) else None


def _setdefault(payload: dict[str, Any], key: str, value: Any) -> None:
    if value not in (None, ""):
        payload.setdefault(key, value)
