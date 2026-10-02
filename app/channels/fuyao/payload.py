"""Build the Fuyao ``POST /v1/videos`` JSON body for each model family.

Only documented fields are emitted. Reference media always travel as public
URLs (the relay rejects base64 before it gets here):

* images -> ``reference_images: [{"url", "role"}]`` with ``reference_image``,
  ``first_frame`` or ``last_frame`` roles; Grok takes a single image as
  ``image: {"url"}``
* videos -> ``reference_videos: [url]``
* audios -> ``reference_audios: [url]``

Wan3 is the exception: options go under ``metadata.parameters`` and media
under ``metadata.input.media[]`` as ``{type, url}`` items (see ``_wan3_body``).

A request the family cannot serve raises :class:`FuyaoRequestError` instead of
being trimmed, so the caller never gets a different task than it asked for.
"""

from __future__ import annotations

import math
from typing import Any

from . import catalog


class FuyaoRequestError(ValueError):
    """The request cannot be expressed for this Fuyao model family."""


FIRST_FRAME = "first_frame"
LAST_FRAME = "last_frame"
REFERENCE_IMAGE = "reference_image"
_ROLE_ORDER = {FIRST_FRAME: 0, LAST_FRAME: 1, REFERENCE_IMAGE: 2}

_IMAGE_LIST_KEYS = ("reference_images", "images", "image_urls", "reference_image_urls")
_IMAGE_SINGLE_KEYS = ("image", "image_url", "input_reference")
_FIRST_FRAME_KEYS = ("first_frame", "first_frame_url", "first_frame_image", "first_image")
_LAST_FRAME_KEYS = ("last_frame", "last_frame_url", "last_frame_image", "last_image")
_VIDEO_LIST_KEYS = ("reference_videos", "video_urls", "videos")
_VIDEO_SINGLE_KEYS = ("reference_video", "video_url")
_AUDIO_LIST_KEYS = ("reference_audios", "audio_urls", "audios")
_AUDIO_SINGLE_KEYS = ("reference_audio", "audio_url")


def transform_create_payload(payload: dict[str, Any], profile: str | None = None) -> dict[str, Any]:
    """Convert the canonical relay payload into the Fuyao request body.

    The route's profile picks the family, so an operator can override the
    name-based guess; without a Fuyao profile the model name decides.
    """
    model = str(payload.get("model") or "").strip()
    if not model:
        raise FuyaoRequestError("model is required")
    family = catalog.family_for_profile(profile) or catalog.family_for_model(model)

    images = _images(payload)
    videos = _urls(payload, _VIDEO_LIST_KEYS, _VIDEO_SINGLE_KEYS)
    audios = _urls(payload, _AUDIO_LIST_KEYS, _AUDIO_SINGLE_KEYS)
    seconds = _seconds(payload)
    ratio = _ratio(payload)
    resolution = _resolution(payload)
    body: dict[str, Any] = {"model": model, "prompt": payload.get("prompt")}

    if family == catalog.GROK:
        return _grok_body(body, images, videos, audios, seconds, ratio, resolution)
    if family == catalog.WAN3:
        return _wan3_body(body, payload, images, videos, audios, seconds, ratio, resolution)

    if family == catalog.MINIMAX_H3:
        allowed = catalog.fixed_durations(model)
        if seconds is None and len(allowed) == 1:
            seconds = allowed[0]
        if seconds not in allowed:
            raise FuyaoRequestError(
                f"{model} 的时长由模型 ID 固定，seconds 只能是 "
                + " 或 ".join(str(value) for value in allowed)
            )
    elif family == catalog.SD2 and catalog.requires_reference(model) and not (images or videos or audios):
        raise FuyaoRequestError(f"{model} 不支持纯文生视频，请提供首帧、首尾帧或参考素材")

    if seconds is not None:
        body["seconds"] = str(seconds)
    if ratio:
        body["aspect_ratio"] = ratio
    # H3 and SD 2.0 encode the output resolution in the model ID; a second
    # value could only conflict with it.
    if family == catalog.GENERIC and resolution:
        body["resolution"] = resolution
    if family == catalog.GENERIC and isinstance(payload.get("size"), str) and payload["size"].strip():
        body["size"] = payload["size"].strip()
    return _with_media(body, images, videos, audios)


def _grok_body(
    body: dict[str, Any],
    images: list[tuple[str, str]],
    videos: list[str],
    audios: list[str],
    seconds: int | None,
    ratio: str | None,
    resolution: str | None,
) -> dict[str, Any]:
    if videos:
        raise FuyaoRequestError("Grok 1.5 不支持参考视频")
    roles = [role for role, _ in images]
    if LAST_FRAME in roles:
        raise FuyaoRequestError("Grok 1.5 不支持尾帧")
    if FIRST_FRAME in roles and len(images) > 1:
        raise FuyaoRequestError("Grok 1.5 的 first_frame 只能单独使用，不能和多张参考图混用")

    # Grok documents ``seconds`` as an integer.
    if seconds is not None:
        body["seconds"] = seconds
    if resolution:
        body["resolution"] = resolution
    if ratio:
        body["aspect_ratio"] = ratio
    if len(images) == 1 and images[0][0] == REFERENCE_IMAGE:
        # The documented single-image form; never send a bare URL string.
        body["image"] = {"url": images[0][1]}
    elif images:
        body["reference_images"] = [{"url": url, "role": role} for role, url in images]
    if audios:
        body["reference_audios"] = audios
    return body


WAN3_AUTO_DURATION = -1
_WAN3_RESOLUTIONS = ("480P", "720P", "1080P")
_WAN3_AUTO_RATIOS = frozenset({"adaptive", "auto"})
_WAN3_EXTRA_PARAMETERS = ("prompt_extend", "watermark", "seed")


def _wan3_body(
    body: dict[str, Any],
    payload: dict[str, Any],
    images: list[tuple[str, str]],
    videos: list[str],
    audios: list[str],
    seconds: int | None,
    ratio: str | None,
    resolution: str | None,
) -> dict[str, Any]:
    """Wan3 takes its options under ``metadata.parameters`` and its media as
    typed items in ``metadata.input.media`` (tutorial update of 2026-10-02).

    The gateway still converts the legacy top-level fields, but documents the
    structured form as the one it bills and validates against, and asks not to
    send ``metadata.video_urls``/``metadata.audio_urls`` at all.
    """
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    if isinstance(metadata.get("input"), dict) and metadata["input"].get("media"):
        # Media given here would bypass the relay's per-route media limits.
        raise FuyaoRequestError(
            "请用 reference_images / reference_videos / reference_audios 传参考素材，"
            "不要直接传 metadata.input.media"
        )
    explicit = metadata.get("parameters") if isinstance(metadata.get("parameters"), dict) else {}

    roles = [role for role, _ in images]
    has_frames = FIRST_FRAME in roles or LAST_FRAME in roles
    if LAST_FRAME in roles and FIRST_FRAME not in roles:
        raise FuyaoRequestError("Wan3 的尾帧必须配合首帧使用")
    if has_frames and (REFERENCE_IMAGE in roles or videos or audios):
        raise FuyaoRequestError("Wan3 的首尾帧模式不能与参考图、参考视频或参考音频混用")

    parameters: dict[str, Any] = {}
    if seconds is not None:
        if seconds != WAN3_AUTO_DURATION and not 2 <= seconds <= 30:
            raise FuyaoRequestError("Wan3 的时长必须是 2–30 秒，或 -1 表示自动时长")
        parameters["duration"] = seconds
    resolution = resolution or _resolution_from_size(payload.get("size") or metadata.get("size"))
    if resolution:
        parameters["resolution"] = _wan3_resolution(resolution)
    # ``adaptive``/``auto`` mean "let the model decide", which Wan3 expresses
    # by leaving ``ratio`` out.
    if ratio and ratio.lower() not in _WAN3_AUTO_RATIOS:
        parameters["ratio"] = ratio
    audio = _first_bool(payload, metadata, ("generate_audio", "generateAudio"))
    if audio is not None:
        parameters["audio"] = audio
    for key in _WAN3_EXTRA_PARAMETERS:
        if payload.get(key) is not None:
            parameters[key] = payload[key]
    # A caller who already speaks the Wan3 form fills the gaps; the relay's own
    # fields carry a split route's pinned resolution, so they come first.
    for key, value in explicit.items():
        if value is None or key in parameters:
            continue
        parameters[key] = _wan3_resolution(value) if key == "resolution" and isinstance(value, str) else value

    media = [{"type": role, "url": url} for role, url in images]
    media += [{"type": "reference_video", "url": url} for url in videos]
    media += [{"type": "reference_audio", "url": url} for url in audios]

    wan_metadata: dict[str, Any] = {}
    if parameters:
        wan_metadata["parameters"] = parameters
    if media:
        wan_metadata["input"] = {"media": media}
    if wan_metadata:
        body["metadata"] = wan_metadata
    return body


def _wan3_resolution(value: str) -> str:
    normalized = value.strip().upper()
    if normalized not in _WAN3_RESOLUTIONS:
        raise FuyaoRequestError("Wan3 的分辨率只能是 480P、720P 或 1080P")
    return normalized


def _resolution_from_size(value: Any) -> str | None:
    """``size=720P`` names the resolution; ``size=1280x720`` implies it."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized[:-1].isdigit() and normalized.endswith("p"):
        return normalized
    parts = normalized.split("x")
    if len(parts) == 2 and all(part.strip().isdigit() and int(part) > 0 for part in parts):
        return f"{min(int(part) for part in parts)}p"
    return None


def _first_bool(payload: dict[str, Any], metadata: dict[str, Any], keys: tuple[str, ...]) -> bool | None:
    for source in (payload, metadata):
        for key in keys:
            value = source.get(key)
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
                return value.strip().lower() == "true"
    return None


def _with_media(
    body: dict[str, Any],
    images: list[tuple[str, str]],
    videos: list[str],
    audios: list[str],
) -> dict[str, Any]:
    if images:
        body["reference_images"] = [{"url": url, "role": role} for role, url in images]
    if videos:
        body["reference_videos"] = videos
    if audios:
        body["reference_audios"] = audios
    return body


def _images(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """Collect ``(role, url)`` pairs from every documented image alias.

    Frames come first (first, then last). A URL sent both as a frame and as a
    reference keeps its frame role. Every alias is merged rather than taking
    the first non-empty one, because the relay counts all of them against the
    route limit and must not forward fewer than it counted.
    """
    found: list[tuple[str, str]] = []
    for role, keys in ((FIRST_FRAME, _FIRST_FRAME_KEYS), (LAST_FRAME, _LAST_FRAME_KEYS)):
        for key in keys:
            if url := _as_url(payload.get(key)):
                found.append((role, url))
                break
    for key in _IMAGE_SINGLE_KEYS:
        if url := _as_url(payload.get(key)):
            found.append((_role_of(payload.get(key)), url))
    for key in _IMAGE_LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            for item in value:
                if url := _as_url(item):
                    found.append((_role_of(item), url))

    by_url: dict[str, str] = {}
    order: list[str] = []
    for role, url in found:
        if url not in by_url:
            order.append(url)
            by_url[url] = role
        elif _ROLE_ORDER[role] < _ROLE_ORDER[by_url[url]]:
            by_url[url] = role
    result = sorted(((by_url[url], url) for url in order), key=lambda item: _ROLE_ORDER[item[0]])
    for role in (FIRST_FRAME, LAST_FRAME):
        if sum(1 for item_role, _ in result if item_role == role) > 1:
            raise FuyaoRequestError(f"{role} 最多只能有 1 张")
    return result


def _urls(payload: dict[str, Any], list_keys: tuple[str, ...], single_keys: tuple[str, ...]) -> list[str]:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    result: list[str] = []
    for source in (payload, metadata):
        for key in single_keys:
            if (url := _as_url(source.get(key))) and url not in result:
                result.append(url)
        for key in list_keys:
            value = source.get(key)
            if isinstance(value, list):
                for item in value:
                    if (url := _as_url(item)) and url not in result:
                        result.append(url)
    return result


def _role_of(value: Any) -> str:
    role = value.get("role") if isinstance(value, dict) else None
    return role if role in _ROLE_ORDER else REFERENCE_IMAGE


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


def _seconds(payload: dict[str, Any]) -> int | None:
    """Read ``seconds``/``duration``; the gateway rejects the two disagreeing."""
    values = []
    for key in ("seconds", "duration"):
        value = payload.get(key)
        if value is None or value == "":
            continue
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise FuyaoRequestError(f"{key} 必须是整数秒") from exc
        if not number.is_integer():
            raise FuyaoRequestError(f"{key} 必须是整数秒")
        values.append(int(number))
    if len(set(values)) > 1:
        raise FuyaoRequestError("seconds 与 duration 同时传入时必须一致")
    return values[0] if values else None


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
    separator = ":" if normalized.count(":") == 1 else "x" if normalized.count("x") == 1 else None
    if separator is None:
        return None
    left, right = (part.strip() for part in normalized.split(separator, 1))
    if not left.isdigit() or not right.isdigit() or int(left) <= 0 or int(right) <= 0:
        return None
    divisor = math.gcd(int(left), int(right))
    return f"{int(left) // divisor}:{int(right) // divisor}"


def _resolution(payload: dict[str, Any]) -> str | None:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    for value in (payload.get("resolution"), payload.get("quality"), metadata.get("resolution")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
