"""Build the Wangyue ``POST /v1/pool/api/tasks`` body.

Reference images cannot travel as URLs: each one is uploaded as a private
asset first (see :mod:`.assets`) and the task carries the asset IDs. The
builder therefore returns the body together with the image URLs in upload
order; ``payload.referenceImageAssetIds`` is filled in after the upload.

A request the family cannot serve raises :class:`WangyueRequestError` instead
of being trimmed, so the caller never gets a different task than it asked for.
"""

from __future__ import annotations

import math
import re
import uuid
from typing import Any

from . import catalog


class WangyueRequestError(ValueError):
    """The request cannot be expressed for this Wangyue model family."""


PROJECT_ID = "new-api-video"

FIRST_FRAME = "first_frame"
LAST_FRAME = "last_frame"
REFERENCE_IMAGE = "reference_image"

AUTO = "auto"
START_FRAME = "start_frame"
START_END = "start_end"
_REFERENCE_MODES = {
    "auto": AUTO,
    "reference": AUTO,
    "start_frame": START_FRAME,
    "first_frame": START_FRAME,
    "start_end": START_END,
    "first_last_frame": START_END,
}

_FIRST_FRAME_KEYS = ("first_frame", "first_frame_url", "first_frame_image", "first_image", "start_image_url")
_LAST_FRAME_KEYS = ("last_frame", "last_frame_url", "last_frame_image", "last_image", "end_image_url")
_IMAGE_SINGLE_KEYS = ("image", "image_url", "input_reference")
_IMAGE_LIST_KEYS = ("image_urls", "images", "reference_images", "reference_image_urls", "image_refs")

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{7,99}$")


def task_id_for(relay_request_id: str) -> str:
    """The upstream task ID we assign; derived so it can be recomputed later."""
    return str(uuid.UUID(hex=relay_request_id.rsplit("_", 1)[-1][:32]))


def normalize_idempotency_key(value: str) -> str:
    """Keep a caller key the gateway accepts; hash anything else."""
    value = value.strip()
    if _IDEMPOTENCY_KEY.match(value):
        return value
    return "nav-" + uuid.uuid5(uuid.NAMESPACE_URL, value).hex


def transform_create_payload(
    payload: dict[str, Any],
    profile: str | None,
    relay_request_id: str,
    idempotency_key: str,
    allowed_durations: list[int] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    model = str(payload.get("model") or "").strip()
    if not model:
        raise WangyueRequestError("model is required")
    family = catalog.family_for_profile(profile) or catalog.family_for_model(model)

    images = _images(payload)
    if len(images) > catalog.MAX_IMAGES:
        raise WangyueRequestError(f"参考图片最多 {catalog.MAX_IMAGES} 张")

    settings: dict[str, Any] = {
        "model": model,
        "channel": catalog.CHANNEL_BY_FAMILY[family],
        "duration": _duration(payload, family, allowed_durations),
    }
    ratio = _ratio(payload)
    if ratio is not None:
        if ratio not in catalog.RATIOS:
            raise WangyueRequestError("画面比例只能是 " + "、".join(catalog.RATIOS))
        settings["aspectRatio"] = ratio
    settings["resolution"] = _resolution(payload, family)

    mode, ordered = _reference_mode(payload, images)
    if mode is not None:
        settings["referenceMode"] = mode
    if family == catalog.VIDU_Q3:
        # One relay task is one video.
        settings["quantity"] = 1
        for key in ("codec", "generationMode"):
            if isinstance(payload.get(key), str) and payload[key].strip():
                settings[key] = payload[key].strip()

    task_payload: dict[str, Any] = {
        "projectId": PROJECT_ID,
        "rowId": relay_request_id,
        "prompt": str(payload.get("prompt") or "").strip(),
        "settings": settings,
    }
    body = {
        "type": catalog.TASK_TYPE_BY_FAMILY[family],
        "taskId": task_id_for(relay_request_id),
        "idempotencyKey": idempotency_key,
        "payload": task_payload,
    }
    return body, ordered


def attach_assets(body: dict[str, Any], asset_ids: list[str]) -> dict[str, Any]:
    if not asset_ids:
        return body
    return {**body, "payload": {**body["payload"], "referenceImageAssetIds": list(asset_ids)}}


def audit_view(
    body: dict[str, Any],
    image_urls: list[str],
    asset_ids: list[str] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """The body as recorded for the admin page, never sent upstream.

    Reference images leave the body as URLs and return as asset IDs only after
    the upload, so ``_referenceImages`` keeps every image visible meanwhile:
    its URL, the asset it became (``None`` until uploaded) and any upload error.
    """
    if not image_urls:
        return body
    ids = list(asset_ids or [])
    view: dict[str, Any] = {
        **body,
        "_referenceImages": [
            {"url": url, "assetId": ids[index] if index < len(ids) else None}
            for index, url in enumerate(image_urls)
        ],
    }
    if error:
        view["_referenceImageUploadError"] = error
    return view


def _reference_mode(
    payload: dict[str, Any], images: list[tuple[str, str]]
) -> tuple[str | None, list[str]]:
    """Choose ``auto`` / ``start_frame`` / ``start_end`` and order the images."""
    requested = _requested_mode(payload)
    roles = [role for role, _ in images]
    urls = [url for _, url in images]
    has_frames = FIRST_FRAME in roles or LAST_FRAME in roles
    if not images:
        if requested not in (None, AUTO):
            raise WangyueRequestError(f"{requested} 模式需要参考图片")
        return None, []
    if has_frames:
        if REFERENCE_IMAGE in roles:
            raise WangyueRequestError("首帧/尾帧不能与普通参考图同时使用")
        if LAST_FRAME in roles and FIRST_FRAME not in roles:
            raise WangyueRequestError("使用尾帧时必须同时提供首帧")
        inferred = START_END if LAST_FRAME in roles else START_FRAME
        if requested not in (None, inferred):
            raise WangyueRequestError(f"首帧/尾帧图片与 reference_mode={requested} 不一致")
        mode = inferred
    else:
        mode = requested or AUTO
    if mode == START_FRAME and len(urls) != 1:
        raise WangyueRequestError("首帧模式必须正好 1 张图片")
    if mode == START_END and len(urls) != 2:
        raise WangyueRequestError("首尾帧模式必须正好 2 张图片")
    return mode, urls


def _requested_mode(payload: dict[str, Any]) -> str | None:
    for key in ("reference_mode", "referenceMode"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            mode = _REFERENCE_MODES.get(value.strip().lower())
            if mode is None:
                raise WangyueRequestError("reference_mode 只能是 auto、start_frame 或 start_end")
            return mode
    return None


def _images(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """``(role, url)`` pairs from every image alias, frames first.

    Every alias is merged (top level and ``metadata``), because the relay
    counts all of them against the route limit and must not forward fewer.
    """
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    found: list[tuple[str, str]] = []
    for source in (payload, metadata):
        for role, keys in ((FIRST_FRAME, _FIRST_FRAME_KEYS), (LAST_FRAME, _LAST_FRAME_KEYS)):
            for key in keys:
                if url := _as_url(source.get(key)):
                    found.append((role, url))
        for key in _IMAGE_SINGLE_KEYS:
            if url := _as_url(source.get(key)):
                found.append((_role_of(source.get(key)), url))
        for key in _IMAGE_LIST_KEYS:
            value = source.get(key)
            if isinstance(value, list):
                for item in value:
                    if url := _as_url(item):
                        found.append((_role_of(item), url))
    order = {FIRST_FRAME: 0, LAST_FRAME: 1, REFERENCE_IMAGE: 2}
    by_url: dict[str, str] = {}
    for role, url in found:
        if url not in by_url or order[role] < order[by_url[url]]:
            by_url[url] = role
    for role in (FIRST_FRAME, LAST_FRAME):
        if sum(1 for value in by_url.values() if value == role) > 1:
            raise WangyueRequestError(f"{role} 最多只能有 1 张")
    return sorted(((role, url) for url, role in by_url.items()), key=lambda item: order[item[0]])


def _role_of(value: Any) -> str:
    role = value.get("role") if isinstance(value, dict) else None
    return role if role in (FIRST_FRAME, LAST_FRAME) else REFERENCE_IMAGE


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


def _duration(payload: dict[str, Any], family: str, allowed: list[int] | None) -> int:
    """The guide asks for an explicit duration; only a single choice is implied."""
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    values: list[int] = []
    for source in (payload, metadata):
        for key in ("duration", "seconds"):
            value = source.get(key)
            if value is None or value == "" or isinstance(value, bool):
                continue
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise WangyueRequestError(f"{key} 必须是整数秒") from exc
            if not number.is_integer():
                raise WangyueRequestError(f"{key} 必须是整数秒")
            values.append(int(number))
    if len(set(values)) > 1:
        raise WangyueRequestError("seconds 与 duration 同时传入时必须一致")
    if values:
        if values[0] not in catalog.DURATIONS[family]:
            raise WangyueRequestError(
                "当前模型的时长只能是 " + "、".join(str(value) for value in catalog.DURATIONS[family]) + " 秒"
            )
        return values[0]
    choices = list(allowed or catalog.DURATIONS[family])
    if len(choices) == 1:
        return choices[0]
    raise WangyueRequestError(
        "请明确传入 duration（可选 " + "、".join(str(value) for value in choices) + " 秒）"
    )


def _resolution(payload: dict[str, Any], family: str) -> str:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    supported = catalog.RESOLUTIONS[family]
    for value in (payload.get("resolution"), metadata.get("resolution"), payload.get("quality")):
        if isinstance(value, str) and re.fullmatch(r"\d{3,4}[pP]", value.strip()):
            normalized = value.strip().upper()
            if normalized not in supported:
                raise WangyueRequestError("当前模型的分辨率只能是 " + "、".join(supported))
            return normalized
    return supported[-1] if family == catalog.VIDU_Q3 else supported[0]


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
