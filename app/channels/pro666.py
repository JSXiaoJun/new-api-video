from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


RATIOS_WIDE = ['21:9', '16:9', '4:3', '1:1', '3:4', '9:16']


PROFILE_DEFINITIONS: dict[str, dict[str, Any]] = {
    'pro666-video-v1': {
        'label': 'Pro666 · video-v1',
        'request_format': 'pro666-video-v1',
        'capabilities': {
            'ratios': ['16:9', '9:16', '1:1'],
            'durations': [5, 10, 15],
            'resolutions': ['720p'],
            'maxImages': 9,
            'referenceVideo': False,
            'maxAudios': 0,
        },
    },
    'pro666-video-900': {
        'label': 'Pro666 · video-900',
        'request_format': 'pro666-video-900',
        'capabilities': {
            'ratios': RATIOS_WIDE,
            'durations': list(range(5, 16)),
            'resolutions': ['720p'],
            'maxImages': 9,
            'referenceVideo': False,
            'maxAudios': 0,
            'experimental': True,
        },
    },
    'pro666-sd2-431': {
        'label': 'Pro666 · sd2-431',
        'request_format': 'pro666-sd2',
        'capabilities': {
            'ratios': RATIOS_WIDE,
            'durations': list(range(4, 16)),
            'resolutions': ['720p'],
            'maxImages': 4,
            'referenceVideo': True,
            'maxAudios': 1,
        },
    },
    'pro666-sd2-5': {
        'label': 'Pro666 · sd2-5',
        'request_format': 'pro666-sd2',
        'capabilities': {
            'ratios': RATIOS_WIDE,
            'durations': list(range(4, 30)),
            'resolutions': ['720p'],
            'maxImages': 30,
            'referenceVideo': True,
            'maxAudios': 10,
        },
    },
    'pro666-firefly-480p': {
        'label': 'Pro666 · Firefly 480p',
        'request_format': 'pro666-firefly',
        'capabilities': {
            'ratios': RATIOS_WIDE,
            'durations': list(range(4, 16)),
            'resolutions': ['480p'],
            'maxImages': 9,
            'referenceVideo': True,
            'maxAudios': 3,
        },
    },
    'pro666-firefly-720p': {
        'label': 'Pro666 · Firefly 720p',
        'request_format': 'pro666-firefly',
        'capabilities': {
            'ratios': RATIOS_WIDE,
            'durations': list(range(4, 16)),
            'resolutions': ['720p'],
            'maxImages': 9,
            'referenceVideo': True,
            'maxAudios': 3,
        },
    },
    'pro666-firefly-1080p': {
        'label': 'Pro666 · Firefly 1080p',
        'request_format': 'pro666-firefly',
        'capabilities': {
            'ratios': RATIOS_WIDE,
            'durations': list(range(4, 16)),
            'resolutions': ['1080p'],
            'maxImages': 9,
            'referenceVideo': True,
            'maxAudios': 3,
        },
    },
    'pro666-veo-omni': {
        'label': 'Pro666 · veo-omni',
        'request_format': 'pro666-veo-omni',
        'capabilities': {
            'ratios': ['16:9', '9:16'],
            'durations': [10],
            'resolutions': ['720p'],
            'maxImages': 9,
            'referenceVideo': False,
            'maxAudios': 0,
        },
    },
}


REQUEST_FORMATS = {definition['request_format'] for definition in PROFILE_DEFINITIONS.values()}


def suggest_route(model: str) -> dict[str, Any] | None:
    normalized = model.strip().lower()
    if normalized in {'video-v1', 'video-v1-face'}:
        return _route('pro666-video-v1', [5, 10, 15], 9, False, False)
    if normalized == 'video-900':
        return _route('pro666-video-900', list(range(5, 16)), 9, False, False)
    if normalized in {'sd2-431-720p-fast', 'sd2-431-720p-pro'}:
        return _route('pro666-sd2-431', list(range(4, 16)), 4, True, True)
    if normalized == 'sd2-5-720p':
        return _route('pro666-sd2-5', list(range(4, 30)), 30, True, True)
    if normalized == 'sd2-5-vref-720p':
        return _route('pro666-sd2-5', list(range(4, 31)), 30, True, True)
    if normalized.startswith('firefly-seedance2'):
        if normalized.endswith('-1080p'):
            profile = 'pro666-firefly-1080p'
        elif normalized.endswith('-480p'):
            profile = 'pro666-firefly-480p'
        elif normalized.endswith('-720p'):
            profile = 'pro666-firefly-720p'
        else:
            return None
        return _route(profile, list(range(4, 16)), 9, True, True)
    if normalized == 'veo-omni':
        return _route('pro666-veo-omni', [10], 9, False, False)
    return None


def _route(
    profile: str,
    durations: list[int],
    image_count: int,
    supports_video: bool,
    supports_audio: bool,
) -> dict[str, Any]:
    return {
        'profile': profile,
        'durations': durations,
        'image_count': image_count,
        'supports_image': image_count > 0,
        'supports_video': supports_video,
        'supports_audio': supports_audio,
    }


def transform_create_payload(payload: dict[str, Any], request_format: str) -> dict[str, Any]:
    metadata = payload.get('metadata') if isinstance(payload.get('metadata'), dict) else {}
    duration = payload.get('duration') or payload.get('seconds')
    aspect_ratio = (
        payload.get('aspect_ratio')
        or payload.get('aspectRatio')
        or payload.get('ratio')
        or metadata.get('aspect_ratio')
        or metadata.get('ratio')
    )
    resolution = payload.get('resolution') or metadata.get('resolution')
    images = _images(payload)
    videos = _media_urls(
        payload,
        ('videos', 'reference_videos', 'video_urls', 'videoUrls'),
        ('reference_video', 'video_url'),
    )
    audios = _media_urls(
        payload,
        ('audios', 'reference_audios', 'audio_urls', 'audioUrls'),
        ('reference_audio', 'audio_url'),
    )
    generate_audio = payload.get('generateAudio') if 'generateAudio' in payload else payload.get('generate_audio')
    first_frame = _first(payload, 'first_frame_url', 'firstFrameUrl', 'first_frame_image', 'first_frame')
    last_frame = _first(payload, 'last_frame_url', 'lastFrameUrl', 'last_frame_image', 'last_frame')
    common = {
        **_extra_fields(payload),
        'model': payload.get('model'),
        'prompt': payload.get('prompt'),
    }

    if request_format == 'pro666-video-v1':
        return {
            **common,
            **({'duration': duration} if duration else {}),
            **({'aspect_ratio': aspect_ratio} if aspect_ratio else {}),
            **({'images': images} if images else {}),
        }
    if request_format == 'pro666-video-900':
        return {
            **common,
            **({'duration': duration} if duration else {}),
            **({'aspect_ratio': aspect_ratio} if aspect_ratio else {}),
            **({'resolution': resolution} if resolution else {}),
            **({'images': images} if images else {}),
        }
    if request_format == 'pro666-sd2':
        return {
            **common,
            **({'duration': duration} if duration else {}),
            **({'aspect_ratio': aspect_ratio} if aspect_ratio else {}),
            **({'generateAudio': generate_audio} if generate_audio is not None else {}),
            **({'resolution': resolution} if resolution else {}),
            **({'first_frame_url': first_frame} if first_frame else {}),
            **({'last_frame_url': last_frame} if last_frame else {}),
            **({'images': images} if images and not (first_frame and last_frame) else {}),
            **({'videos': videos} if videos and not (first_frame and last_frame) else {}),
            **({'audios': audios} if audios and not (first_frame and last_frame) else {}),
        }
    if request_format == 'pro666-firefly':
        return {
            **common,
            **({'duration': duration} if duration else {}),
            **({'aspect_ratio': aspect_ratio} if aspect_ratio else {}),
            **({'generateAudio': generate_audio} if generate_audio is not None else {}),
            **({'first_frame_url': first_frame} if first_frame else {}),
            **({'last_frame_url': last_frame} if last_frame else {}),
            **({'images': images} if images and not (first_frame and last_frame) else {}),
            **({'videos': videos} if videos and not (first_frame and last_frame) else {}),
            **({'audios': audios} if audios and not (first_frame and last_frame) else {}),
        }
    if request_format == 'pro666-veo-omni':
        messages = payload.get('messages') if isinstance(payload.get('messages'), list) and payload['messages'] else None
        if messages is None and images:
            messages = [{
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': payload.get('prompt')},
                    *[
                        {'type': 'image_url', 'image_url': {'url': image, 'detail': 'high'}}
                        for image in images
                    ],
                ],
            }]
        return {
            **common,
            **({'seconds': str(duration)} if duration else {}),
            **({'aspect_ratio': aspect_ratio} if aspect_ratio else {}),
            **({'resolution': resolution} if resolution else {}),
            **({'messages': messages} if messages else {}),
        }
    raise ValueError(f'Unsupported Pro666 request format: {request_format}')


def _images(payload: dict[str, Any]) -> list[Any]:
    for key in ('images', 'image_urls'):
        value = payload.get(key)
        if isinstance(value, list) and value:
            return value
    image = _first(payload, 'image_url', 'image', 'input_reference')
    references = _first_list(payload, 'reference_image_urls', 'reference_images', 'imageUrls')
    if image:
        return [image, *[item for item in references if item != image]]
    return references


def _media_urls(
    payload: dict[str, Any],
    list_keys: tuple[str, ...],
    single_keys: tuple[str, ...],
) -> list[Any]:
    values = _first_list(payload, *list_keys)
    if values:
        return values
    value = _first(payload, *single_keys)
    return [value] if value else []


def _first(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if payload.get(key):
            return payload[key]
    return None


def _first_list(payload: dict[str, Any], *keys: str) -> list[Any]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list) and value:
            return value
    return []


_CONSUMED_FIELDS = {
    'model', 'prompt', 'duration', 'seconds', 'aspect_ratio', 'aspectRatio', 'ratio', 'resolution',
    'generate_audio', 'generateAudio', 'metadata', 'messages',
    'images', 'image_urls', 'image_url', 'image', 'input_reference',
    'reference_image_urls', 'reference_images', 'imageUrls',
    'videos', 'reference_videos', 'video_urls', 'videoUrls', 'reference_video', 'video_url',
    'audios', 'reference_audios', 'audio_urls', 'audioUrls', 'reference_audio', 'audio_url',
    'reference_mode', 'extra_body',
    'first_frame_url', 'firstFrameUrl', 'first_frame_image', 'first_frame',
    'last_frame_url', 'lastFrameUrl', 'last_frame_image', 'last_frame',
}


def _extra_fields(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in _CONSUMED_FIELDS}


def permits_api_key_forwarding(source_url: str, base_url: str) -> bool:
    try:
        source = urlsplit(source_url)
        base = urlsplit(base_url)
        source_port = source.port or (443 if source.scheme.lower() == 'https' else 80)
        base_port = base.port or (443 if base.scheme.lower() == 'https' else 80)
    except ValueError:
        return False
    return (
        base.scheme.lower() == 'https'
        and (base.hostname or '').lower() == 'api.pro666.top'
        and base_port == 443
        and source.scheme.lower() == 'https'
        and (source.hostname or '').lower() == 'pro666.top'
        and source_port == 443
        and source.path.startswith('/v1/videos/')
        and source.path.endswith('/content')
    )
