from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from .channels import autodl_comfyui, funai, o10_grok, pro666, rolldek


MAX_DURATION_SECONDS = 60


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
        for key in ('minReferenceVideoDuration', 'maxReferenceVideoDuration'):
            capabilities.pop(key, None)
    else:
        capabilities['referenceVideo'] = True
        capabilities.setdefault('minReferenceVideoDuration', 0)
        capabilities.setdefault('maxReferenceVideoDuration', 30)
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


def transform_create_payload(payload: dict[str, Any], profile: str) -> dict[str, Any]:
    request_format = PROFILE_DEFINITIONS[profile]['request_format']
    if request_format in {'rolldek-ch3', 'rolldek-ch4'}:
        return rolldek.transform_create_payload(payload)
    if request_format == autodl_comfyui.PROFILE:
        return autodl_comfyui.transform_create_payload(payload)
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
    reference_video = payload.get('reference_video')
    if not reference_video and isinstance(payload.get('reference_videos'), list) and payload['reference_videos']:
        reference_video = payload['reference_videos'][0]
    duration = payload.get('duration') or payload.get('seconds')
    aspect_ratio = payload.get('aspect_ratio') or metadata.get('aspect_ratio') or metadata.get('ratio')
    resolution = payload.get('resolution') or metadata.get('resolution')
    known_fields = {
        'model', 'prompt', 'aspect_ratio', 'duration', 'seconds', 'resolution', 'generate_audio',
        'image_url', 'image_urls', 'images', 'reference_image_urls', 'reference_video',
        'reference_videos', 'audio_urls', 'metadata',
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
            **({'reference_videos': [reference_video]} if reference_video else {}),
            **({'audio_urls': payload['audio_urls']} if payload.get('audio_urls') else {}),
        }
    return {
        **common,
        **({'aspect_ratio': aspect_ratio} if aspect_ratio else {}),
        **({'duration': duration} if duration else {}),
        **({'resolution': resolution} if resolution else {}),
        **({'image_url': images[0]} if len(images) == 1 else {}),
        **({'image_urls': images} if len(images) > 1 else {}),
        **({'reference_video': reference_video} if reference_video else {}),
        **({'audio_urls': payload['audio_urls']} if payload.get('audio_urls') else {}),
    }
