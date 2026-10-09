"""Isolated adapter for the FunAI video API."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit


PROTOCOL = "funai"
CREATE_PATH = "/v1/videos"

KNOWN_MODELS = (
    "minimax-h3",
    "kling-o3",
    "kling-o3-pro-v2v-reference",
    "kling-o3-standard-v2v-reference",
    "kling-v3",
    "kling-v3-omni-v2v-create",
    "runway-gen4-turbo",
    "runway-gen4.5",
    "sora-2",
    "sora-2-pro",
    "gemini-omni",
    "gemini-omni-flash",
    "veo-3.1",
    "veo-3.1-fast",
    "veo-3.1-lite",
)

ALIASES = {
    "sora": "sora-2",
    "sora2": "sora-2",
    "sora-pro": "sora-2-pro",
    "sora2-pro": "sora-2-pro",
    "veo-3.1-generate-preview": "veo-3.1",
    "veo-3.1-generate-001": "veo-3.1",
    "veo-3.1-fast-generate-preview": "veo-3.1-fast",
    "veo-3.1-fast-generate-001": "veo-3.1-fast",
    "veo-3.1-lite-generate": "veo-3.1-lite",
    "veo:3.1-lite-generate": "veo-3.1-lite",
    "veo@3.1-lite-generate": "veo-3.1-lite",
    "gemini-omni@omni-flash": "gemini-omni-flash",
    "gemini-omni:omni-flash": "gemini-omni-flash",
    "kling-video-o3-pro-reference-video-to-video": "kling-o3-pro-v2v-reference",
    "kling:kling-o3-pro-v2v-reference": "kling-o3-pro-v2v-reference",
    "kling@kling-o3-pro-v2v-reference": "kling-o3-pro-v2v-reference",
    "kling-video-o3-standard-reference-video-to-video": "kling-o3-standard-v2v-reference",
    "kling:kling-o3-standard-v2v-reference": "kling-o3-standard-v2v-reference",
    "kling@kling-o3-standard-v2v-reference": "kling-o3-standard-v2v-reference",
    "kling-video-o3-v2v-create": "kling-v3-omni-v2v-create",
    "kling:kling-v3-omni-v2v-create": "kling-v3-omni-v2v-create",
    "kling@kling-v3-omni-v2v-create": "kling-v3-omni-v2v-create",
}

# ``veo-3.1-fl`` / ``veo-3.1-fast-fl`` 不是别名：``-fl`` 后缀本身就是首尾帧
# 模式的开关，归一化会丢掉画像里的图片数量差异。
COMPATIBLE_MODELS = set(ALIASES) | {
    "veo-3.1-fl",
    "veo-3.1-fast-fl",
}

# 结果音轨开关（``audio``）的模型差异：MiniMax H3 固定生成原生音频，只能
# 省略或传 ``true``；Gemini Omni 与 Runway 都不接受这个字段。
AUDIO_LOCKED_TRUE = frozenset({"minimax-h3"})
AUDIO_UNSUPPORTED = frozenset({"gemini-omni", "gemini-omni-flash", "runway-gen4-turbo", "runway-gen4.5"})


def canonical_model(model: str) -> str:
    """把文档里的兼容名称折叠成规范名，原样名返回自身。"""
    normalized = model.strip().lower()
    return ALIASES.get(normalized, normalized)


PROFILE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "funai-minimax-h3": {
        "label": "FunAI MiniMax H3",
        "request_format": "funai",
        "capabilities": {
            "ratios": ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
            "durations": list(range(5, 16)),
            "resolutions": ["1440p"],
            "maxImages": 5,
            "referenceVideo": False,
            "maxAudios": 3,
            "minAudioDuration": 0,
            "maxAudioDuration": 15,
            "maxTotalAudioDuration": 15,
            "experimental": True,
        },
    },
    "funai-kling": {
        "label": "FunAI Kling 参考图",
        "request_format": "funai",
        "capabilities": {
            "ratios": ["16:9", "1:1", "9:16"],
            "durations": list(range(3, 16)),
            "resolutions": ["720p", "1080p", "2160p"],
            "maxImages": 7,
            "referenceVideo": True,
            "maxVideos": 1,
            "minReferenceVideoDuration": 0,
            "maxReferenceVideoDuration": 30,
            "experimental": True,
        },
    },
    "funai-kling-frames": {
        "label": "FunAI Kling 首尾帧",
        "request_format": "funai",
        "capabilities": {
            "ratios": ["16:9", "1:1", "9:16"],
            "durations": list(range(3, 16)),
            "resolutions": ["720p", "1080p", "2160p"],
            "maxImages": 2,
            "referenceVideo": True,
            "maxVideos": 1,
            "minReferenceVideoDuration": 0,
            "maxReferenceVideoDuration": 30,
            "experimental": True,
        },
    },
    "funai-runway": {
        "label": "FunAI Runway",
        "request_format": "funai",
        "capabilities": {
            "ratios": ["16:9", "9:16"],
            "durations": [5, 10],
            "resolutions": ["720p"],
            "maxImages": 1,
            "referenceVideo": False,
            "experimental": True,
        },
    },
    "funai-sora": {
        "label": "FunAI Sora",
        "request_format": "funai",
        "capabilities": {
            "ratios": ["16:9", "9:16"],
            "durations": [4, 8, 12],
            "resolutions": ["720p"],
            "maxImages": 1,
            "referenceVideo": False,
            "experimental": True,
        },
    },
    "funai-gemini-omni": {
        "label": "FunAI Gemini Omni",
        "request_format": "funai",
        "capabilities": {
            "ratios": ["16:9", "9:16"],
            "durations": list(range(3, 11)),
            "resolutions": ["720p"],
            "maxImages": 5,
            "referenceVideo": False,
            "experimental": True,
        },
    },
    "funai-veo": {
        "label": "FunAI Veo",
        "request_format": "funai",
        "capabilities": {
            "ratios": ["16:9", "9:16"],
            "durations": [4, 6, 8],
            "resolutions": ["720p", "1080p"],
            "maxImages": 3,
            "referenceVideo": False,
            "experimental": True,
        },
    },
}


def is_funai_base_url(base_url: str) -> bool:
    try:
        hostname = (urlsplit(base_url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return hostname == "api.funai.works" or hostname.endswith(".api.funai.works")


def is_video_model(model: str) -> bool:
    normalized = model.strip().lower()
    return normalized in KNOWN_MODELS or normalized in COMPATIBLE_MODELS


def suggest_route(model: str) -> dict[str, Any] | None:
    normalized = canonical_model(model)
    if not is_video_model(normalized):
        return None
    if normalized == "minimax-h3":
        return _route("funai-minimax-h3", range(5, 16), ["1440p"], 5, False, True)
    if normalized.startswith("kling-"):
        image_count = 7
        supports_video = normalized in {
            "kling-o3",
            "kling-o3-pro-v2v-reference",
            "kling-o3-standard-v2v-reference",
            "kling-v3-omni-v2v-create",
        }
        if "v2v-reference" in normalized:
            image_count = 4
        elif normalized == "kling-v3":
            image_count = 2
        elif normalized == "kling-v3-omni-v2v-create":
            image_count = 6
        profile = "funai-kling-frames" if normalized == "kling-v3" else "funai-kling"
        return _route(
            profile,
            range(3, 16),
            ["720p", "1080p", "2160p"],
            image_count,
            supports_video,
            False,
        )
    if normalized.startswith("runway-"):
        durations = [5, 8, 10] if normalized == "runway-gen4.5" else [5, 10]
        return _route("funai-runway", durations, ["720p"], 1, False, False)
    if normalized in {"sora", "sora2", "sora-2", "sora-2-pro"}:
        return _route("funai-sora", [4, 8, 12], ["720p"], 1, False, False)
    if normalized.startswith("gemini-omni"):
        return _route("funai-gemini-omni", range(3, 11), ["720p"], 5, False, False)
    if normalized.startswith("veo-3.1"):
        durations = [8] if "fast" in normalized else [4, 6, 8]
        image_count = 2 if normalized.endswith("-fl") or normalized == "veo-3.1-lite" else 3
        return _route("funai-veo", durations, ["720p", "1080p"], image_count, False, False)
    return None


def _route(
    profile: str,
    durations: Any,
    resolutions: list[str],
    image_count: int,
    supports_video: bool,
    supports_audio: bool,
) -> dict[str, Any]:
    return {
        "profile": profile,
        "durations": list(durations),
        "resolutions": resolutions,
        "image_count": image_count,
        "supports_image": image_count > 0,
        "supports_video": supports_video,
        "supports_audio": supports_audio,
    }


def transform_create_payload(
    payload: dict[str, Any],
    profile: str | None = None,
) -> dict[str, Any]:
    model = str(payload.get("model") or "").strip()
    # 兼容名称（``sora-pro``、``kling:kling-o3-pro-v2v-reference``…）指向同一族
    # 模型，字段映射必须按规范名判断，否则会落到错误的参考字段上。
    normalized_model = canonical_model(model)
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    canonical_fields = {
        "model",
        "prompt",
        "duration",
        "seconds",
        "aspect_ratio",
        "resolution",
        "generate_audio",
        "image_url",
        "image_urls",
        "images",
        "image_reference",
        "reference_image",
        "reference_images",
        "reference_image_urls",
        "reference_video",
        "reference_videos",
        "audio_urls",
        "metadata",
    }
    result = {key: value for key, value in payload.items() if key not in canonical_fields}
    result["model"] = model
    result["prompt"] = payload.get("prompt")

    duration = payload.get("seconds")
    if duration is None:
        duration = payload.get("duration")
    if duration is not None and duration != "":
        result["seconds"] = duration

    aspect_ratio = payload.get("aspect_ratio") or metadata.get("aspect_ratio") or metadata.get("ratio")
    resolution = payload.get("resolution") or metadata.get("resolution")
    if aspect_ratio:
        result["aspect_ratio"] = aspect_ratio
    if resolution:
        result["resolution"] = resolution
    _apply_audio_switch(result, normalized_model, payload)

    images, singular_reference = _reference_images(payload)

    reference_video = _first_reference_video(payload)
    if (
        reference_video
        and _supports_reference_video(normalized_model)
        and not any(key in result for key in ("input_video", "inputVideo", "video_references"))
    ):
        result["input_video"] = reference_video

    if images:
        if normalized_model == "minimax-h3":
            result.setdefault("reference_images", images)
        elif normalized_model == "kling-o3" and profile == "funai-kling-frames":
            _set_kling_frames(result, normalized_model, images)
        elif normalized_model == "kling-o3" or normalized_model.startswith("gemini-omni"):
            if singular_reference and len(images) == 1:
                result.setdefault("image_reference", images[0])
            else:
                result.setdefault("reference_images", images)
        elif normalized_model.startswith("kling-"):
            if profile == "funai-kling-frames":
                _set_kling_frames(result, normalized_model, images)
            else:
                # The default Kling profile always treats uploads as subject references.
                result.setdefault("element_references", images)
        else:
            result.setdefault("images", images)

    audio_urls = _string_list(payload.get("audio_urls"))
    if normalized_model == "minimax-h3" and audio_urls:
        result.setdefault("audio_reference", audio_urls)

    if normalized_model == "kling-o3" and any(
        key in result for key in ("input_video", "inputVideo", "video_references")
    ):
        result.pop("seconds", None)

    return {key: value for key, value in result.items() if value is not None and value != ""}


def _set_kling_frames(result: dict[str, Any], model: str, images: list[str]) -> None:
    frames = images[:2]
    if model in {"kling-o3", "kling-v3"}:
        result.setdefault("start_frame", frames[0])
        if len(frames) > 1:
            result.setdefault("end_frame", frames[1])
    elif model in {"kling-o3-pro-v2v-reference", "kling-o3-standard-v2v-reference"}:
        result.setdefault("images", frames)
    else:
        # Kling Omni V2V does not accept first/last-frame input.
        result.setdefault("element_references", images)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"", "false", "0", "no"}
    return bool(value)


def _apply_audio_switch(result: dict[str, Any], model: str, payload: dict[str, Any]) -> None:
    """把工作台的 ``generate_audio`` 映射成 FunAI 的结果音轨开关 ``audio``。

    文档对三个模型族另有约束：MiniMax H3 固定生成原生音频（只能省略或传
    ``true``），Gemini Omni 与 Runway 不接受该字段。这两种情况下原样下发都会被
    上游 400 拒掉，所以按模型收敛：锁定的模型只在请求为真时补 ``true``，不支持的
    模型直接不带这个字段。
    """
    if model in AUDIO_UNSUPPORTED:
        result.pop("audio", None)
        return
    supplied = result["audio"] if "audio" in result else payload.get("generate_audio")
    if supplied is None:
        return
    if model in AUDIO_LOCKED_TRUE:
        if _truthy(supplied):
            result["audio"] = True
        else:
            result.pop("audio", None)
        return
    result["audio"] = supplied


def _reference_images(payload: dict[str, Any]) -> tuple[list[str], bool]:
    """Normalize local aliases while retaining explicit singular intent."""
    singular = payload.get("reference_image")
    if not isinstance(singular, str) or not singular.strip():
        singular = payload.get("image_reference")
    if isinstance(singular, str) and singular.strip():
        return [singular.strip()], True

    for field in ("reference_images", "image_urls", "images"):
        images = _string_list(payload.get(field))
        if images:
            return images, False

    image_url = payload.get("image_url")
    if isinstance(image_url, str) and image_url.strip():
        images = [image_url.strip()]
        images.extend(_string_list(payload.get("reference_image_urls")))
        return images, False
    return [], False


def _first_reference_video(payload: dict[str, Any]) -> str | None:
    value = payload.get("reference_video")
    if isinstance(value, str) and value.strip():
        return value.strip()
    videos = _string_list(payload.get("reference_videos"))
    return videos[0] if videos else None


def _supports_reference_video(model: str) -> bool:
    return model in {
        "kling-o3",
        "kling-o3-pro-v2v-reference",
        "kling-o3-standard-v2v-reference",
        "kling-v3-omni-v2v-create",
    }


def api_url(base_url: str, path: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.lower().endswith("/v1") and path.startswith("/v1/"):
        normalized = normalized[:-3]
    return normalized + path


def task_path(task_id: str) -> str:
    return f"/v1/videos/{quote(task_id, safe='')}"


def content_path(task_id: str) -> str:
    return f"{task_path(task_id)}/content"


def extract_create_task_id(payload: dict[str, Any]) -> str:
    return str(payload.get("id") or payload.get("task_id") or "").strip()


def extract_task_fields(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return {
        "status": payload.get("status") or data.get("status"),
        "video_url": (
            payload.get("url")
            or payload.get("video_url")
            or payload.get("content_url")
            or data.get("url")
            or data.get("video_url")
        ),
        "error": payload.get("error") or payload.get("message") or data.get("error"),
        "progress": payload.get("progress", data.get("progress")),
    }
