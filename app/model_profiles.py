from __future__ import annotations

from copy import deepcopy
import re
from typing import Any


SUPPORTED_DURATION_OPTIONS = (4, 5, 8, 10, 12, 15)


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
        'request_format': 'default',
        'capabilities': {
            'ratios': ['16:9', '9:16'],
            'durations': [4, 6, 8, 10],
            'resolutions': ['720p', '1080p'],
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
    'grok-auto': {
        'label': 'Grok 自动参数',
        'request_format': 'grok',
        'capabilities': {
            'ratios': ['自动'],
            'durations': [0],
            'resolutions': ['自动'],
            'maxImages': 0,
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
}


def suggest_profile(model: str, protocol: str) -> str:
    if protocol == 'seedance':
        return 'default'
    return {
        'gemini-omni-flash': 'gemini-omni',
        'sora2': 'sora2',
        'veo31-fast': 'veo31-fast',
        'manxue-900': 'manxue-900',
        'manxue-933': 'manxue-933',
        'manxue-900-10s': 'manxue-933',
        'grok-imagine-1.0-video': 'grok-auto',
        'grok-imagine-video-1.5-fast': 'grok-fast',
        'grok-imagine-video-1.5-preview': 'grok-auto',
    }.get(model, 'default')


def suggest_duration_override(model: str) -> int | None:
    match = re.search(r'-(\d{1,2})s(?:$|-)', model.lower())
    if not match:
        return None
    duration = int(match.group(1))
    return duration if duration in SUPPORTED_DURATION_OPTIONS else None


def profile_options() -> list[dict[str, str]]:
    return [{'id': profile, 'label': data['label']} for profile, data in PROFILE_DEFINITIONS.items()]


def capabilities_for(profile: str, duration_overrides: list[int] | int | None = None) -> dict[str, Any]:
    capabilities = deepcopy(PROFILE_DEFINITIONS[profile]['capabilities'])
    if isinstance(duration_overrides, int):
        duration_overrides = [duration_overrides]
    if duration_overrides:
        capabilities['durations'] = duration_overrides
    return capabilities


def transform_create_payload(payload: dict[str, Any], profile: str) -> dict[str, Any]:
    request_format = PROFILE_DEFINITIONS[profile]['request_format']
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
    aspect_ratio = payload.get('aspect_ratio') or metadata.get('ratio')
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
            **({'seconds': duration} if duration else {}),
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
