"""Fuyao host detection, model families, request profiles and route hints.

The gateway serves several model families behind one ``/v1/videos`` endpoint.
Their limits come from the per-model catalog in the tutorial and differ enough
that each family gets its own profile:

* ``grok``       -- ``grok-imagine-video-1.5`` (including the ``（zj）`` group)
* ``minimax-h3`` -- ``Minimax-H3-*``; resolution and duration are fixed by the
  model ID, so only matching ``seconds`` values are accepted
* ``sd2``        -- ``【官方稳定版】sd2.0-{480p,720p}-{fast,mini,满血}``; the
  resolution is fixed by the model ID and ``mini`` cannot do text-to-video
* ``wan3``       -- ``wan3.0-video`` / ``wan3.0-video-prime``
* ``generic``    -- every other video model (``seedance2.0-903``,
  ``Seedance2.5xg``, ``video-editor-fixed-0.5`` ...), whose limits the upstream
  does not publish; it starts with conservative defaults that the operator can
  widen per route

Families are recognised by name shape rather than a fixed list, so a model the
gateway publishes later lands in the right family without a code change.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit


PROTOCOL = "fuyao"
# Every Fuyao profile shares one request format; the family is picked from the
# profile name (see ``family_for_profile``).
PROFILE = "fuyao"
DEFAULT_PROFILE = "fuyao-video"

HOSTS = frozenset({"fuyao47.xyz", "www.fuyao47.xyz"})

GROK = "grok"
MINIMAX_H3 = "minimax-h3"
SD2 = "sd2"
WAN3 = "wan3"
GENERIC = "generic"

PROFILE_BY_FAMILY = {
    GROK: "fuyao-grok",
    MINIMAX_H3: "fuyao-minimax-h3",
    SD2: "fuyao-sd2",
    WAN3: "fuyao-wan3",
    GENERIC: DEFAULT_PROFILE,
}
FAMILY_BY_PROFILE = {profile: family for family, profile in PROFILE_BY_FAMILY.items()}

# Media limits per family: (images, videos, audios).
MEDIA_LIMITS = {
    GROK: (7, 0, 3),
    MINIMAX_H3: (9, 3, 3),
    SD2: (9, 3, 3),
    WAN3: (10, 5, 5),
    GENERIC: (1, 0, 0),
}

GROK_DURATIONS = list(range(4, 16))
H3_DEFAULT_DURATIONS = [10, 15]
SD2_DURATIONS = list(range(4, 16))
WAN3_DURATIONS = list(range(2, 31))
GENERIC_DURATIONS = list(range(4, 16))

# Keywords that mark a ``/v1/models`` entry as a video model when the gateway
# does not report ``supported_endpoint_types``. The same token also sees chat
# and image models, which must not become video routes.
_VIDEO_KEYWORDS = (
    "video", "seedance", "sd2.", "wan3", "minimax-h3", "hailuo", "kling", "veo", "sora", "vidu",
)
_RESOLUTION_IN_NAME = re.compile(r"-(\d{3,4})p(?:-|$)")
_H3_TWO_DURATIONS = re.compile(r"-(\d{1,2})s?-(\d{1,2})s$")
_H3_ONE_DURATION = re.compile(r"-(\d{1,2})s$")


def _capabilities(
    family: str,
    ratios: list[str],
    durations: list[int],
    resolutions: list[str],
    **extra: Any,
) -> dict[str, Any]:
    images, videos, audios = MEDIA_LIMITS[family]
    return {
        "ratios": ratios,
        "durations": durations,
        "resolutions": resolutions,
        "maxImages": images,
        "referenceVideo": videos > 0,
        "maxVideos": videos,
        "maxAudios": audios,
        **extra,
    }


PROFILE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "fuyao-grok": {
        "label": "扶摇 · Grok 1.5 视频",
        "request_format": PROFILE,
        "capabilities": _capabilities(
            GROK, ["16:9", "1:1", "9:16"], GROK_DURATIONS, ["480p", "720p"]
        ),
    },
    "fuyao-minimax-h3": {
        "label": "扶摇 · Minimax H3",
        "request_format": PROFILE,
        "capabilities": _capabilities(
            MINIMAX_H3,
            ["21:9", "16:9", "3:2", "4:3", "1:1", "3:4", "2:3", "9:16"],
            H3_DEFAULT_DURATIONS,
            ["768p", "1440p"],
            maxReferences=12,
        ),
    },
    "fuyao-sd2": {
        "label": "扶摇 · SD 2.0 官方稳定版",
        "request_format": PROFILE,
        "capabilities": _capabilities(
            SD2,
            ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
            SD2_DURATIONS,
            ["480p", "720p"],
        ),
    },
    "fuyao-wan3": {
        "label": "扶摇 · Wan 3.0",
        "request_format": PROFILE,
        "capabilities": _capabilities(
            WAN3,
            ["adaptive", "16:9", "4:3", "1:1", "3:4", "9:16"],
            WAN3_DURATIONS,
            ["480p", "720p", "1080p"],
            maxReferenceVideoDuration=15,
        ),
    },
    DEFAULT_PROFILE: {
        "label": "扶摇 · 通用视频",
        "request_format": PROFILE,
        "capabilities": _capabilities(
            GENERIC,
            ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"],
            GENERIC_DURATIONS,
            ["720p"],
            experimental=True,
        ),
    },
}


def is_fuyao_base_url(base_url: str) -> bool:
    """Return whether the configured base URL is the Fuyao gateway."""
    try:
        hostname = (urlsplit(base_url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return hostname in HOSTS


def family_for_model(model: str) -> str:
    normalized = model.strip().lower()
    if normalized.startswith("grok-imagine-video"):
        return GROK
    if "minimax-h3" in normalized:
        return MINIMAX_H3
    if re.search(r"sd2\.\d+-\d{3,4}p", normalized):
        return SD2
    if normalized.startswith("wan3"):
        return WAN3
    return GENERIC


def family_for_profile(profile: str | None) -> str | None:
    return FAMILY_BY_PROFILE.get(profile or "")


def is_video_model(model: str, endpoint_types: Any = None) -> bool:
    """Tell video models apart from the chat/image models on the same token.

    ``supported_endpoint_types`` is authoritative when the gateway reports it;
    otherwise fall back to the model name.
    """
    if isinstance(endpoint_types, list) and endpoint_types:
        return any(isinstance(item, str) and "video" in item.lower() for item in endpoint_types)
    normalized = model.strip().lower()
    return any(keyword in normalized for keyword in _VIDEO_KEYWORDS)


def name_resolution(model: str) -> str | None:
    """Return the output resolution encoded in the model ID, e.g. ``720p``."""
    match = _RESOLUTION_IN_NAME.search(model.strip().lower())
    return f"{match.group(1)}p" if match else None


def fixed_durations(model: str) -> list[int]:
    """Durations a Minimax H3 model ID allows; ``seconds`` must match one."""
    normalized = model.strip().lower()
    match = _H3_TWO_DURATIONS.search(normalized)
    if match:
        return sorted({int(match.group(1)), int(match.group(2))})
    match = _H3_ONE_DURATION.search(normalized)
    if match:
        return [int(match.group(1))]
    return list(H3_DEFAULT_DURATIONS)


def requires_reference(model: str) -> bool:
    """SD 2.0 ``mini`` only runs first-frame, first/last-frame or reference modes."""
    normalized = model.strip().lower()
    return family_for_model(normalized) == SD2 and "-mini" in normalized


def suggest_route(model: str) -> dict[str, Any]:
    """Route hint for a model discovered on the Fuyao gateway.

    The host already identified the channel, so every model gets a route; the
    family decides the profile and the per-model limits.
    """
    family = family_for_model(model)
    images, videos, audios = MEDIA_LIMITS[family]
    if family == GROK:
        durations, resolutions = list(GROK_DURATIONS), ["480p", "720p"]
        # The catalog allows 3 audios, but the Grok request table documents no
        # audio field, so audio stays opt-in per route.
        audios = 0
    elif family == MINIMAX_H3:
        durations = fixed_durations(model)
        resolutions = [name_resolution(model) or "768p"]
    elif family == SD2:
        durations = list(SD2_DURATIONS)
        resolutions = [name_resolution(model) or "720p"]
    elif family == WAN3:
        durations, resolutions = list(WAN3_DURATIONS), ["480p", "720p", "1080p"]
    else:
        # Limits are unpublished: leave duration/resolution to the workbench
        # default and start with a single reference image.
        durations, resolutions = [], []
    return {
        "profile": PROFILE_BY_FAMILY[family],
        "durations": durations,
        "resolutions": resolutions,
        "image_count": images,
        "video_count": videos,
        "audio_count": audios,
        "supports_image": images > 0,
        "supports_video": videos > 0,
        "supports_audio": audios > 0,
    }
