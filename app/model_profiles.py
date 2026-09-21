from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from .channels import autodl_comfyui, funai, mai_token, o10_grok, pro666, rolldek, sub2api_video


MAX_DURATION_SECONDS = 60


# Reference-video spellings that only the channel adapters accept. They are
# not part of the public contract, but each one is read by at least one
# adapter, so the route limit has to account for them or it can be bypassed.
_REFERENCE_VIDEO_ALIAS_KEYS = ('videos', 'video_refs', 'reference_video_urls')

# Every field a reference video can arrive in, i.e. the contract spellings plus
# the adapter aliases. Rewriting a capped request must clear all of them.
REFERENCE_VIDEO_FIELDS = frozenset(
    ('reference_video', 'video_url', 'reference_videos', 'video_urls', 'videoUrls')
    + _REFERENCE_VIDEO_ALIAS_KEYS
)


PROFILE_DEFINITIONS: dict[str, dict[str, Any]] = {
    'default': {
        'label': '通用视频',
        'request_format': 'default',
        'capabilities': {
            'ratios': ['16:9', '9:16'],
            'durations': [4, 6, 8, 10],
            'resolutions': ['720p', '1080p'],
            'maxImages': 5,
            'referenceVideo': False,
            'experimental': True,
        },
    },
    'gemini-omni': {
        'label': 'Gemini Omni',
        'request_format': 'gemini-omni',
        'capabilities': {
            'ratios': ['16:9', '9:16'],
            'durations': [5],
            'resolutions': ['720p'],
            'maxImages': 5,
            'referenceVideo': True,
            'maxVideos': 1,
            'minReferenceVideoDuration': 0,
            'maxReferenceVideoDuration': 30,
        },
    },
    'sora2': {
        'label': 'Sora 2',
        'request_format': 'default',
        'capabilities': {
            'ratios': ['16:9', '9:16'],
            'durations': [4, 8, 12],
            'resolutions': ['720p'],
            'maxImages': 1,
            'referenceVideo': False,
            'experimental': True,
        },
    },
    'veo31-fast': {
        'label': 'Veo 3.1 Fast',
        'request_format': 'default',
        'capabilities': {
            'ratios': ['16:9', '9:16'],
            'durations': [4, 6, 8],
            'resolutions': ['720p', '1080p'],
            'maxImages': 2,
            'referenceVideo': False,
            'experimental': True,
        },
    },
    'manxue-900': {
        'label': '满血 900',
        'request_format': 'manxue-900',
        'capabilities': {
            'ratios': ['16:9', '9:16', '4:3', '3:4', '1:1', '21:9'],
            'durations': list(range(5, 16)),
            'resolutions': ['720p'],
            'maxImages': 9,
            'referenceVideo': False,
            'experimental': True,
        },
    },
    'manxue-933': {
        'label': '933 多模态',
        'request_format': 'manxue-933',
        'capabilities': {
            'ratios': ['16:9', '9:16', '4:3', '3:4', '1:1', '21:9'],
            'durations': [15],
            'resolutions': ['720p'],
            'maxImages': 9,
            'referenceVideo': True,
            'maxVideos': 3,
            'maxAudios': 3,
            'maxReferences': 12,
            'minReferenceVideoDuration': 2,
            'maxReferenceVideoDuration': 15,
            'minAudioDuration': 2,
            'maxAudioDuration': 15,
            'maxTotalAudioDuration': 15,
            'experimental': True,
        },
    },
    'ark-seedance-2': {
        'label': '方舟 Seedance 2.0',
        'request_format': 'default',
        'capabilities': {
            'ratios': ['16:9', '9:16', '4:3', '3:4', '1:1', '21:9'],
            'durations': list(range(4, 16)),
            'resolutions': ['480p', '720p'],
            'maxImages': 9,
            'referenceVideo': True,
            'maxVideos': 3,
            'maxAudios': 3,
            'maxReferences': 12,
            'minReferenceVideoDuration': 2,
            'maxReferenceVideoDuration': 15,
            'minAudioDuration': 2,
            'maxAudioDuration': 15,
            'maxTotalAudioDuration': 15,
            'experimental': True,
        },
    },
    'grok-auto': {
        'label': 'Grok 自动参数',
        'request_format': 'grok',
        'capabilities': {
            'ratios': ['16:9', '9:16'],
            'durations': list(range(1, 16)),
            'resolutions': ['480p', '720p'],
            'maxImages': 1,
            'referenceVideo': False,
            'experimental': True,
        },
    },
    'grok-fast': {
        'label': 'Grok Fast',
        'request_format': 'default',
        'capabilities': {
            'ratios': ['16:9', '9:16'],
            'durations': [10],
            'resolutions': ['720p'],
            'maxImages': 5,
            'referenceVideo': False,
            'experimental': True,
        },
    },
    **funai.PROFILE_DEFINITIONS,
    **pro666.PROFILE_DEFINITIONS,
    **autodl_comfyui.PROFILE_DEFINITIONS,
    **sub2api_video.PROFILE_DEFINITIONS,
    **mai_token.PROFILE_DEFINITIONS,
    **rolldek.PROFILE_DEFINITIONS,
}


def suggest_profile(model: str, protocol: str) -> str:
    if protocol == funai.PROTOCOL:
        route = funai.suggest_route(model)
        return route['profile'] if route else 'funai-veo'
    if protocol == autodl_comfyui.PROTOCOL:
        return autodl_comfyui.PROFILE
    if protocol == o10_grok.PROTOCOL:
        return 'grok-auto'
    if protocol == sub2api_video.PROTOCOL:
        return sub2api_video.PROFILE
    if protocol == mai_token.PROTOCOL:
        route = mai_token.suggest_route(model)
        return route['profile'] if route else 'mai-token-720p'
    if protocol == 'ark-v3':
        return 'ark-seedance-2'
    if protocol == rolldek.PROTOCOL:
        route = rolldek.suggest_route(model)
        return route['profile'] if route else 'rolldek-sd2-ch4'
    if protocol == 'seedance':
        return 'default'
    pro666_route = pro666.suggest_route(model)
    if pro666_route:
        return pro666_route['profile']
    return {
        'gemini-omni-flash': 'gemini-omni',
        'omni-flash-720p': 'gemini-omni',
        'sora2': 'sora2',
        'veo31-fast': 'veo31-fast',
        'manxue-900': 'manxue-900',
        'manxue-933': 'manxue-933',
        'sora-v3-933-pro': 'manxue-933',
        'tejiasd2': 'manxue-933',
        'manxue-900-10s': 'manxue-933',
        'grok-imagine-1.0-video': 'grok-auto',
        'grok-imagine-video-1.5-fast': 'grok-fast',
        'grok-imagine-video-1.5-preview': 'grok-auto',
    }.get(model, 'default')


def suggest_protocol(model: str) -> str:
    if autodl_comfyui.suggest_route(model):
        return autodl_comfyui.PROTOCOL
    if o10_grok.suggest_route(model):
        return o10_grok.PROTOCOL
    if sub2api_video.suggest_route(model):
        return sub2api_video.PROTOCOL
    if mai_token.suggest_route(model):
        return mai_token.PROTOCOL
    if rolldek.suggest_route(model):
        return rolldek.PROTOCOL
    if pro666.suggest_route(model):
        return 'videos'
    return 'seedance' if 'seedance' in model.lower() else 'videos'


def suggest_duration_override(model: str) -> int | None:
    match = re.search(r'-(\d{1,2})s(?:$|-)', model.lower())
    if not match:
        return None
    duration = int(match.group(1))
    return duration if 1 <= duration <= MAX_DURATION_SECONDS else None


def profile_options() -> list[dict[str, str]]:
    return [{'id': profile, 'label': data['label']} for profile, data in PROFILE_DEFINITIONS.items()]


def suggest_route(model: str, protocol: str) -> dict[str, Any]:
    if protocol == funai.PROTOCOL:
        return funai.suggest_route(model) or {
            'profile': 'funai-veo',
            'durations': [],
            'resolutions': [],
            'image_count': 1,
            'supports_image': True,
            'supports_video': False,
            'supports_audio': False,
        }
    if protocol == autodl_comfyui.PROTOCOL:
        return autodl_comfyui.suggest_route(model) or {
            'profile': autodl_comfyui.PROFILE,
            'durations': [],
            'resolutions': [],
            'image_count': 0,
            'supports_image': False,
            'supports_video': False,
            'supports_audio': False,
        }
    if protocol == o10_grok.PROTOCOL:
        return o10_grok.suggest_route(model) or {
            'profile': 'grok-auto',
            'durations': list(range(1, 16)),
            'resolutions': ['480p', '720p'],
            'image_count': 1,
            'supports_image': True,
            'supports_video': False,
            'supports_audio': False,
        }
    if protocol == sub2api_video.PROTOCOL:
        return sub2api_video.suggest_route(model) or {
            'profile': sub2api_video.PROFILE,
            'durations': list(range(1, 16)),
            'resolutions': ['480p', '720p', '1080p'],
            'image_count': 7,
            'supports_image': True,
            'supports_video': False,
            'supports_audio': False,
        }
    if protocol == mai_token.PROTOCOL:
        return mai_token.suggest_route(model) or {
            'profile': 'mai-token-720p',
            'durations': list(mai_token.DURATIONS),
            'resolutions': ['720p'],
            'image_count': mai_token.MAX_IMAGES,
            'supports_image': True,
            'supports_video': True,
            'supports_audio': True,
        }
    if protocol == rolldek.PROTOCOL:
        return rolldek.suggest_route(model) or {
            'profile': 'rolldek-sd2-ch4',
            'durations': [],
            'resolutions': ['720p'],
            'image_count': 9,
            'supports_image': True,
            'supports_video': True,
            'supports_audio': True,
        }
    channel_route = pro666.suggest_route(model) if protocol == 'videos' else None
    if channel_route:
        return channel_route
    duration = suggest_duration_override(model)
    return {
        'profile': suggest_profile(model, protocol),
        'durations': [duration] if duration else [],
    }


def capabilities_for(
    profile: str,
    duration_overrides: list[int] | int | None = None,
    supports_image: bool = True,
    supports_video: bool = True,
    supports_audio: bool = True,
    max_images: int | None = None,
    resolution_overrides: list[str] | None = None,
    max_videos: int | None = None,
) -> dict[str, Any]:
    capabilities = deepcopy(PROFILE_DEFINITIONS[profile]['capabilities'])
    if isinstance(duration_overrides, int):
        duration_overrides = [duration_overrides]
    if duration_overrides:
        capabilities['durations'] = duration_overrides
    if resolution_overrides:
        capabilities['resolutions'] = resolution_overrides
    if max_images is not None:
        capabilities['maxImages'] = max(0, max_images)
    elif not supports_image:
        capabilities['maxImages'] = 0
    elif not capabilities.get('maxImages'):
        capabilities['maxImages'] = 1
    if not supports_video:
        capabilities['referenceVideo'] = False
        capabilities['maxVideos'] = 0
        for key in ('minReferenceVideoDuration', 'maxReferenceVideoDuration'):
            capabilities.pop(key, None)
    else:
        # An explicit route ``video_count`` wins over the channel default: it is
        # the number the proxy enforces, so reporting anything else here would
        # advertise a limit the relay does not honour.
        capabilities['maxVideos'] = (
            max(0, max_videos) if max_videos is not None
            else max(1, capabilities.get('maxVideos', 0))
        )
        # ``video_count = 0`` means "never forward a reference video", which the
        # consumers of this payload express through ``referenceVideo``.
        capabilities['referenceVideo'] = capabilities['maxVideos'] > 0
        if capabilities['referenceVideo']:
            capabilities.setdefault('minReferenceVideoDuration', 0)
            capabilities.setdefault('maxReferenceVideoDuration', 30)
        else:
            for key in ('minReferenceVideoDuration', 'maxReferenceVideoDuration'):
                capabilities.pop(key, None)
    if not supports_audio:
        capabilities['maxAudios'] = 0
        for key in ('minAudioDuration', 'maxAudioDuration', 'maxTotalAudioDuration'):
            capabilities.pop(key, None)
    else:
        capabilities['maxAudios'] = max(1, capabilities.get('maxAudios', 0))
        capabilities.setdefault('minAudioDuration', 2)
        capabilities.setdefault('maxAudioDuration', 15)
        capabilities.setdefault('maxTotalAudioDuration', 15)
    return capabilities


def _reference_videos(payload: dict[str, Any]) -> list[str]:
    """Collect every reference video the client sent, in order.

    The public contract accepts both the documented single ``reference_video``
    and the plural ``reference_videos`` / ``video_urls`` arrays, so a request
    that passes several clips must not lose them here.
    """
    result: list[str] = []
    # Keep the documented singular field first: it previously took precedence
    # over the plural array, and existing callers rely on that ordering.
    for key in ('reference_video', 'video_url'):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            result.append(value.strip())
            break
    for key in ('reference_videos', 'video_urls', 'videoUrls'):
        value = payload.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, str) and item.strip() and item.strip() not in result:
                result.append(item.strip())
    return result


def _reference_video_aliases(payload: dict[str, Any]) -> list[str]:
    """Collect reference videos using every spelling an adapter understands.

    ``_reference_videos`` follows the public contract only. Enforcement also has
    to look at the channel-level aliases (``videos``, ``video_refs``,
    ``reference_video_urls``): an adapter reads whichever one the caller sent,
    so ignoring them here would let a caller step around the configured limit
    simply by renaming the field. Entries are returned in the order the
    adapters resolve them.
    """
    videos = _reference_videos(payload)
    for key in _REFERENCE_VIDEO_ALIAS_KEYS:
        value = payload.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, str) and item.strip() and item.strip() not in videos:
                videos.append(item.strip())
    return videos


def _profile_max_videos(profile: str) -> int:
    """Return how many reference videos this profile's upstream accepts.

    Mirrors ``capabilities_for``: a profile without an explicit ``maxVideos``
    keeps the historical single-video behaviour instead of forwarding extras
    the upstream is not known to accept.
    """
    capabilities = PROFILE_DEFINITIONS.get(profile, {}).get('capabilities', {})
    return max(1, capabilities.get('maxVideos', 1))


def effective_video_limit(configured: int | None) -> int | None:
    """Resolve how many reference videos one route may forward.

    ``configured`` is the operator's per-route ``video_count``. An explicit
    value -- including ``0``, which drops every reference video -- always wins,
    so the field the admin sets is the field that takes effect.

    ``None`` means the route was never configured, and the request is then
    forwarded untouched. Returning ``None`` rather than a number is what makes
    this setting strictly additive: every existing route relays exactly what it
    relayed before, so a caller sending a single ``reference_video`` cannot
    regress, and channels that already accept several clips keep receiving
    them. Each profile keeps applying its own documented ``maxVideos`` in that
    case, which is exactly what ``capabilities_for`` advertises for it.
    """
    if configured is not None:
        return max(0, configured)
    return None


def limit_reference_videos(payload: dict[str, Any], limit: int | None) -> dict[str, Any]:
    """Cap the request's reference videos to the route's configured limit.

    ``limit`` is the resolved ``video_count`` for the route. The payload is
    rewritten only when the request actually has to change: a request that is
    already within budget is returned as-is, so a legacy caller sending a single
    ``reference_video`` reaches the adapter with its original spelling intact
    and no adapter-visible difference from before this setting existed.

    When clips must be dropped the survivors are rewritten to the canonical
    plural ``reference_videos`` array, which every adapter reads, and all the
    other spellings are removed so an adapter's fallback lookup cannot
    resurrect a clip that was meant to be dropped.
    """
    if limit is None:
        return payload
    videos = _reference_video_aliases(payload)
    if len(videos) <= limit:
        return payload
    trimmed = {
        key: value
        for key, value in payload.items()
        if key not in REFERENCE_VIDEO_FIELDS
    }
    if limit > 0:
        trimmed['reference_videos'] = videos[:limit]
    return trimmed


def transform_create_payload(
    payload: dict[str, Any],
    profile: str,
    video_limit: int | None = None,
) -> dict[str, Any]:
    request_format = PROFILE_DEFINITIONS[profile]['request_format']
    if request_format in {'rolldek-ch1', 'rolldek-ch2', 'rolldek-ch3', 'rolldek-ch4'}:
        return rolldek.transform_create_payload(payload)
    if request_format == autodl_comfyui.PROFILE:
        return autodl_comfyui.transform_create_payload(payload)
    if request_format == sub2api_video.PROFILE:
        return sub2api_video.transform_create_payload(payload)
    if request_format == mai_token.PROFILE:
        return mai_token.transform_create_payload(payload)
    if request_format in pro666.REQUEST_FORMATS:
        return pro666.transform_create_payload(payload, request_format)
    metadata = payload.get('metadata') if isinstance(payload.get('metadata'), dict) else {}
    images = payload.get('image_urls') if isinstance(payload.get('image_urls'), list) else []
    if not images and isinstance(payload.get('images'), list):
        images = payload['images']
    if not images and payload.get('image_url'):
        images = [payload['image_url']]
        if isinstance(payload.get('reference_image_urls'), list):
            images.extend(payload['reference_image_urls'])
    reference_videos = _reference_videos(payload)
    if video_limit is None:
        # No route-level setting: keep the historical profile-only cap.
        profile_limit = _profile_max_videos(profile)
        if profile_limit > 0:
            reference_videos = reference_videos[:profile_limit]
    else:
        reference_videos = reference_videos[:max(0, video_limit)]
    reference_video = reference_videos[0] if reference_videos else None
    duration = payload.get('duration') or payload.get('seconds')
    aspect_ratio = payload.get('aspect_ratio') or metadata.get('aspect_ratio') or metadata.get('ratio')
    resolution = payload.get('resolution') or metadata.get('resolution')
    known_fields = {
        'model', 'prompt', 'aspect_ratio', 'duration', 'seconds', 'resolution', 'generate_audio',
        'image_url', 'image_urls', 'images', 'reference_image_urls', 'reference_video',
        'reference_videos', 'video_url', 'video_urls', 'audio_urls', 'metadata',
    }
    extra = {key: value for key, value in payload.items() if key not in known_fields}
    common = {
        **extra,
        'model': payload.get('model'),
        'prompt': payload.get('prompt'),
        **({'generate_audio': payload['generate_audio']} if 'generate_audio' in payload else {}),
    }

    if request_format == 'grok':
        return common
    if request_format == 'gemini-omni':
        return {
            **{key: value for key, value in common.items() if key != 'generate_audio'},
            'duration': 5,
            **({'resolution': str(resolution).upper()} if resolution else {}),
            **({'metadata': {'aspect_ratio': aspect_ratio}} if aspect_ratio else {}),
            **({'images': images} if images else {}),
            **({'reference_video': reference_video} if reference_video else {}),
            **({'audio_urls': payload['audio_urls']} if payload.get('audio_urls') else {}),
        }
    if request_format == 'manxue-900':
        return {
            **common,
            **({'duration': duration} if duration else {}),
            **({'images': images} if images else {}),
            'metadata': {
                **({'ratio': aspect_ratio} if aspect_ratio else {}),
                **({'resolution': resolution} if resolution else {}),
            },
        }
    if request_format == 'manxue-933':
        return {
            **common,
            **({'aspect_ratio': aspect_ratio} if aspect_ratio else {}),
            **({'seconds': str(duration)} if duration else {}),
            **({'resolution': resolution} if resolution else {}),
            **({'image_url': images[0]} if images else {}),
            **({'reference_image_urls': images[1:]} if len(images) > 1 else {}),
            **({'reference_videos': reference_videos} if reference_videos else {}),
            **({'audio_urls': payload['audio_urls']} if payload.get('audio_urls') else {}),
        }
    return {
        **common,
        **({'aspect_ratio': aspect_ratio} if aspect_ratio else {}),
        **({'duration': duration} if duration else {}),
        **({'resolution': resolution} if resolution else {}),
        **({'image_url': images[0]} if len(images) == 1 else {}),
        **({'image_urls': images} if len(images) > 1 else {}),
        **(
            {'reference_videos': reference_videos}
            if len(reference_videos) > 1
            else {'reference_video': reference_video} if reference_video else {}
        ),
        **({'audio_urls': payload['audio_urls']} if payload.get('audio_urls') else {}),
    }
