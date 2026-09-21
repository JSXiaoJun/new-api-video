from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Iterator, Mapping

from .channels import autodl_comfyui, funai, mai_token, o10_grok, pro666, rolldek, sub2api_video


MAX_DURATION_SECONDS = 60


# 接入新上游时先看这里：一个渠道适配器能读到的每个参考媒体字段都必须登记在
# 下面三张表里，否则调用方只要换个字段名就能绕过后台配置的数量上限。字段名
# 先做规范化比较（去掉标点再转小写），所以 ``imageUrls`` 与 ``image_urls``
# 属于同一个字段；前缀表用来兜住 ``reference_image_url_1`` 这类带序号的写法。
MEDIA_KINDS = ('image', 'video', 'audio')
URL_VALUE_KEYS = frozenset({'url', 'uri', 'href'})
INLINE_VALUE_KEYS = frozenset({'data', 'base64', 'b64json', 'bytes', 'content', 'source'})
MEDIA_FIELD_NAMES: dict[str, frozenset[str]] = {
    'image': frozenset({
        'image', 'images', 'imageurl', 'imageurls', 'imagebase64', 'imageref', 'imagerefs',
        'inputimage', 'inputimages', 'inputreference', 'initimage', 'initimages',
        'referenceimage', 'referenceimages', 'referenceimageurl', 'referenceimageurls',
        'firstimage', 'lastimage', 'startimageurl', 'endimageurl', 'startframeurl', 'endframeurl',
        'firstframe', 'lastframe', 'firstframeimage', 'lastframeimage', 'firstframeurl', 'lastframeurl',
        'refimage', 'refimages',
    }),
    'video': frozenset({
        'referencevideo', 'referencevideos', 'referencevideourls',
        'videourl', 'videourls', 'videos', 'videoref', 'videorefs', 'refvideo', 'refvideos',
    }),
    'audio': frozenset({
        'audiourl', 'audiourls', 'audios', 'audioref', 'audiorefs',
        'referenceaudio', 'referenceaudios', 'audioreference',
    }),
}
MEDIA_FIELD_PREFIXES: dict[str, tuple[str, ...]] = {
    'image': ('imageurl', 'imageref', 'referenceimage', 'refimage'),
    'video': ('videourl', 'videoref', 'referencevideo', 'refvideo'),
    'audio': ('audiourl', 'audioref', 'referenceaudio'),
}
MEDIA_LABELS: dict[str, tuple[str, str]] = {
    'image': ('图片', '张'),
    'video': ('视频', '个'),
    'audio': ('音频', '个'),
}


class MediaLimitError(Exception):
    """请求携带的参考媒体数量超过该模型配置的上限。"""


def _normalize_field_name(key: Any) -> str:
    return re.sub(r'[^a-z0-9]', '', str(key).lower())


def _is_media_field(kind: str, normalized: str) -> bool:
    return normalized in MEDIA_FIELD_NAMES[kind] or normalized.startswith(MEDIA_FIELD_PREFIXES[kind])


def _iter_media_field_values(value: Any, kind: str) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _iter_media_field_values(item, kind)
    elif isinstance(value, dict):
        # OpenAI 风格的媒体对象把地址放在 ``url``/``image_url`` 下，把内联数据放在
        # ``data``/``source`` 下；其余键（``type`` 之类）只是元数据，不能当成引用。
        for key, child in value.items():
            normalized = _normalize_field_name(key)
            if (
                normalized in URL_VALUE_KEYS
                or normalized in INLINE_VALUE_KEYS
                or any(_is_media_field(media, normalized) for media in MEDIA_KINDS)
            ):
                yield from _iter_media_field_values(child, kind)


def iter_media_references(payload: Any, kind: str) -> Iterator[str]:
    """按渠道适配器认识的每一种写法，列出请求里的参考媒体。

    ``kind`` 取 ``image`` / ``video`` / ``audio``。公共协议字段与适配器私有别名
    都在 :data:`MEDIA_FIELD_NAMES` 里，嵌套数组（``messages[].content[].image_url``）
    也会被走一遍，因此计数结果和适配器实际转发的内容一致。
    """
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized = _normalize_field_name(key)
            if _is_media_field(kind, normalized):
                yield from _iter_media_field_values(value, kind)
            elif isinstance(value, (dict, list)):
                yield from iter_media_references(value, kind)
    elif isinstance(payload, list):
        for item in payload:
            yield from iter_media_references(item, kind)


def media_reference_counts(payload: dict[str, Any]) -> dict[str, int]:
    """统计请求里每种参考媒体的个数，同一个地址重复出现只算一次。"""
    return {
        kind: len({value.strip() for value in iter_media_references(payload, kind) if value.strip()})
        for kind in MEDIA_KINDS
    }


def enforce_reference_media_limits(payload: dict[str, Any], counts: Mapping[str, int]) -> None:
    """超过该模型配置的数量就拒绝请求，并说明是哪种媒体、超了多少。

    ``counts`` 是后台为这条路由配置的数量，与 ``/v1/model-capabilities`` 对外
    公布的数字完全一致，调用方可以提前知道预算。``0`` 表示该模型不支持这类参考
    媒体：请求会被拒绝，而不是悄悄把参考媒体丢掉——用户要的是图生视频，静默返回
    一个文生视频的结果等于交付了另一个任务。
    """
    found = media_reference_counts(payload)
    for kind in MEDIA_KINDS:
        limit = max(0, int(counts.get(kind, 0)))
        if found[kind] <= limit:
            continue
        label, unit = MEDIA_LABELS[kind]
        reason = (
            f'当前模型不支持{label}' if limit == 0
            else f'当前模型最多 {limit} {unit}'
        )
        raise MediaLimitError(
            f'{label}数量超过上限：本次请求 {found[kind]} {unit}，{reason}'
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
    # Protocol detection must stay strict. ``suggest_route`` routes any
    # ``-720p``/``-1080p`` suffix to this channel so newly published tiers keep
    # working, but that rule is far too loose to identify the channel itself:
    # Pro666's ``v1-seedance-2.0-720p`` would be captured here and get the
    # wrong request body.
    if mai_token.is_known_model(model):
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
    """Suggest a route for a discovered model, media counts included.

    渠道适配器只描述自己支持哪几类参考媒体，这里统一换算成后台要保存的数量：
    数量是唯一事实来源，发现出来的模型直接带上数字，运营不用再手动补。
    """
    route = _suggest_route(model, protocol)
    counts = resolve_media_counts(
        route.get('profile', 'default'),
        route.get('image_count'),
        route.get('video_count'),
        route.get('audio_count'),
        route.get('supports_image', True),
        route.get('supports_video', True),
        route.get('supports_audio', True),
    )
    return {**route, **counts}


def _suggest_route(model: str, protocol: str) -> dict[str, Any]:
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
    max_images: int | None = None,
    max_videos: int | None = None,
    max_audios: int | None = None,
    resolution_overrides: list[str] | None = None,
) -> dict[str, Any]:
    """对外公布某条路由的能力，数字必须与代理实际执行的上限一致。

    三个数量参数是路由上配置的媒体数量（``None`` 表示沿用渠道默认值，``0``
    表示该模型不支持这类参考媒体）。公布的 ``maxImages`` / ``maxVideos`` /
    ``maxAudios`` 就是代理会用来拒绝超量请求的数字，两边同源。
    """
    capabilities = deepcopy(PROFILE_DEFINITIONS[profile]['capabilities'])
    defaults = profile_default_counts(profile)
    if isinstance(duration_overrides, int):
        duration_overrides = [duration_overrides]
    if duration_overrides:
        capabilities['durations'] = duration_overrides
    if resolution_overrides:
        capabilities['resolutions'] = resolution_overrides
    capabilities['maxImages'] = max(
        0, defaults['image_count'] if max_images is None else max_images
    )
    max_videos = max(0, defaults['video_count'] if max_videos is None else max_videos)
    capabilities['maxVideos'] = max_videos
    # ``video_count = 0`` 表示永远不转发参考视频，消费方通过 ``referenceVideo``
    # 表达这件事，时长区间也一并撤掉，免得广告出一个不可能生效的范围。
    capabilities['referenceVideo'] = max_videos > 0
    if max_videos > 0:
        capabilities.setdefault('minReferenceVideoDuration', 0)
        capabilities.setdefault('maxReferenceVideoDuration', 30)
    else:
        for key in ('minReferenceVideoDuration', 'maxReferenceVideoDuration'):
            capabilities.pop(key, None)
    max_audios = max(0, defaults['audio_count'] if max_audios is None else max_audios)
    capabilities['maxAudios'] = max_audios
    if max_audios > 0:
        capabilities.setdefault('minAudioDuration', 2)
        capabilities.setdefault('maxAudioDuration', 15)
        capabilities.setdefault('maxTotalAudioDuration', 15)
    else:
        for key in ('minAudioDuration', 'maxAudioDuration', 'maxTotalAudioDuration'):
            capabilities.pop(key, None)
    return capabilities


def _profile_max_videos(profile: str) -> int:
    """Return how many reference videos this profile's upstream accepts.

    Mirrors ``capabilities_for``: a profile without an explicit ``maxVideos``
    keeps the historical single-video behaviour instead of forwarding extras
    the upstream is not known to accept.
    """
    capabilities = PROFILE_DEFINITIONS.get(profile, {}).get('capabilities', {})
    return max(1, capabilities.get('maxVideos', 1))


def profile_default_counts(profile: str) -> dict[str, int]:
    """路由没有配置数量时沿用的渠道默认值，规则与 ``capabilities_for`` 一致。

    旧数据里“没填数量”表示沿用渠道默认值，这份默认值就是当时对外公布的数字，
    所以用它回填不会改变老路由已经公告出去的能力。
    """
    capabilities = PROFILE_DEFINITIONS.get(profile, {}).get('capabilities', {})
    return {
        'image_count': max(1, capabilities.get('maxImages') or 0),
        'video_count': _profile_max_videos(profile),
        'audio_count': max(1, capabilities.get('maxAudios') or 0),
    }


def resolve_media_counts(
    profile: str,
    image_count: int | None = None,
    video_count: int | None = None,
    audio_count: int | None = None,
    supports_image: bool = True,
    supports_video: bool = True,
    supports_audio: bool = True,
) -> dict[str, int]:
    """把一条路由的能力配置换算成三个明确的媒体数量。

    数量是唯一事实来源：填了就用填的（``0`` 表示不支持，与留空同义），没填
    才回退到旧的勾选式配置——勾选为否记 0，勾选为是沿用渠道默认值。旧版前端
    只会提交勾选，这个回退保证它们保存出来的路由行为不变。
    """
    defaults = profile_default_counts(profile)
    configured = {
        'image_count': image_count,
        'video_count': video_count,
        'audio_count': audio_count,
    }
    legacy_flags = {
        'image_count': supports_image,
        'video_count': supports_video,
        'audio_count': supports_audio,
    }
    resolved: dict[str, int] = {}
    for field, value in configured.items():
        if value is None:
            value = defaults[field] if legacy_flags[field] else 0
        resolved[field] = max(0, int(value))
    return resolved


def route_media_counts(row: Mapping[str, Any]) -> dict[str, int]:
    """读出路由行上执行用的数量；留空（NULL）按 0 = 不支持处理。

    行既可能是普通字典，也可能是 ``sqlite3.Row``（只有下标访问），所以这里不
    用 ``.get()``，缺列一律按未配置处理。
    """
    counts: dict[str, int] = {}
    for kind in MEDIA_KINDS:
        try:
            value = row[f'{kind}_count']
        except (KeyError, IndexError, TypeError):
            value = None
        counts[kind] = max(0, int(value or 0))
    return counts


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
