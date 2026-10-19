"""Wangyue pool host detection, model families, profiles and route hints.

Published catalog (``GET /v1/pool/api/models``, 2026-10-19), all billed per
task (``billingType: fixed``), never per second:

* ``seedance2.0fast`` -- 10 s (video-space flow) or 15 s (chat-confirm flow),
  720P, up to 9 reference images
* ``seedance2.0mini`` -- 15 s only, 720P, up to 9 reference images
* ``vidu-q3``         -- 3-16 s, 720P/1080P, up to 9 reference images

No family accepts reference videos or audios.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


PROTOCOL = "wangyue"
PROFILE = "wangyue"
MODELS_PATH = "/v1/pool/api/models"

# The production API root is a Tencent SCF host; ``*.wangyueaigc.cn`` are the
# portals (the guide says not to send API calls to them, but an operator who
# pastes one should still get the catalog).
_SCF_HOSTS = frozenset({"1456719883-78ndn27wdv.ap-guangzhou.tencentscf.com"})
_PORTAL_DOMAIN = "wangyueaigc.cn"

SEEDANCE_FAST = "seedance-fast"
SEEDANCE_MINI = "seedance-mini"
VIDU_Q3 = "vidu-q3"

PROFILE_BY_FAMILY = {
    SEEDANCE_FAST: "wangyue-seedance-fast",
    SEEDANCE_MINI: "wangyue-seedance-mini",
    VIDU_Q3: "wangyue-vidu-q3",
}
FAMILY_BY_PROFILE = {profile: family for family, profile in PROFILE_BY_FAMILY.items()}

CHANNEL_BY_FAMILY = {SEEDANCE_FAST: "doubao", SEEDANCE_MINI: "doubao", VIDU_Q3: "vidu"}
TASK_TYPE_BY_FAMILY = {SEEDANCE_FAST: "doubao_video", SEEDANCE_MINI: "doubao_video", VIDU_Q3: "pool_video"}

DURATIONS = {
    SEEDANCE_FAST: [10, 15],
    SEEDANCE_MINI: [15],
    VIDU_Q3: list(range(3, 17)),
}
RESOLUTIONS = {
    SEEDANCE_FAST: ["720P"],
    SEEDANCE_MINI: ["720P"],
    VIDU_Q3: ["720P", "1080P"],
}
RATIOS = ["16:9", "9:16", "4:3", "3:4", "1:1"]
MAX_IMAGES = 9


def _capabilities(family: str) -> dict[str, Any]:
    return {
        "ratios": list(RATIOS),
        "durations": list(DURATIONS[family]),
        "resolutions": [value.lower() for value in RESOLUTIONS[family]],
        "maxImages": MAX_IMAGES,
        "referenceVideo": False,
        "maxVideos": 0,
        "maxAudios": 0,
    }


PROFILE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "wangyue-seedance-fast": {
        "label": "望月号池 · Seedance 2.0 Fast",
        "request_format": PROFILE,
        "capabilities": _capabilities(SEEDANCE_FAST),
    },
    "wangyue-seedance-mini": {
        "label": "望月号池 · Seedance 2.0 Mini",
        "request_format": PROFILE,
        "capabilities": _capabilities(SEEDANCE_MINI),
    },
    "wangyue-vidu-q3": {
        "label": "望月号池 · Vidu Q3",
        "request_format": PROFILE,
        "capabilities": _capabilities(VIDU_Q3),
    },
}


def is_wangyue_base_url(base_url: str) -> bool:
    try:
        hostname = (urlsplit(base_url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return (
        hostname in _SCF_HOSTS
        or hostname == _PORTAL_DOMAIN
        or hostname.endswith("." + _PORTAL_DOMAIN)
    )


def family_for_model(model: str) -> str:
    """Pick the family by name; an unknown model gets the narrowest one."""
    normalized = model.strip().lower().replace(" ", "")
    if "vidu" in normalized:
        return VIDU_Q3
    if "seedance" in normalized and "fast" in normalized:
        return SEEDANCE_FAST
    return SEEDANCE_MINI


def family_for_profile(profile: str | None) -> str | None:
    return FAMILY_BY_PROFILE.get(profile or "")


def suggest_route(model: str) -> dict[str, Any]:
    family = family_for_model(model)
    return {
        "profile": PROFILE_BY_FAMILY[family],
        "durations": list(DURATIONS[family]),
        "resolutions": [value.lower() for value in RESOLUTIONS[family]],
        "image_count": MAX_IMAGES,
        "video_count": 0,
        "audio_count": 0,
        "supports_image": True,
        "supports_video": False,
        "supports_audio": False,
    }


def discovered_models(payload: Any) -> list[dict[str, Any]]:
    """Turn the ``/v1/pool/api/models`` answer into ``{"id": ...}`` items."""
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        return []
    result: list[dict[str, Any]] = []
    for item in payload["models"]:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("modelId") or "").strip()
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        if model_id and metadata.get("capability", "video") == "video":
            result.append({"id": model_id})
    return result
