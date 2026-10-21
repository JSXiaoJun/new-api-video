from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Iterator, Mapping

from .channels import autodl_comfyui, funai, fuyao, mai_token, o10_grok, pro666, rolldek, sub2api_video, wangyue


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


class ResolutionNotAllowedError(Exception):
    """请求的分辨率不在该模型支持的分辨率列表里。"""


# A tier, optionally followed by an orientation suffix. AutoDL's workflows carry
# the orientation in the same string (``768p横`` / ``768p(1:1)``), so the match
# must read the tier and ignore what trails it rather than reject the request.
_RESOLUTION_VALUE = re.compile(r'^(\d{3,4})\s*p(?![0-9])', re.IGNORECASE)


def pinned_resolution(resolutions: list[str] | None) -> str | None:
    """返回这条路由唯一的分辨率，没有唯一值就返回 ``None``。

    路由只配了一个分辨率时，它既是公布给调用方的唯一选项，也是这条路由必须
    出片的档位：调用方没传就补上它。这覆盖了拆分按钮生成的
    ``wan3.0-video-720p`` → ``wan3.0-video``，也覆盖了 MAI Token 这类上游——
    它要求请求体里必须带 ``resolution``，只靠模型名不够。

    ``自动`` 和空值表示“不指定档位”，不能补，否则等于替调用方选了分辨率。
    """
    values = allowed_resolutions(None, resolutions)
    return values[0] if len(values) == 1 else None


def allowed_resolutions(
    profile: str | None,
    resolution_overrides: list[str] | None = None,
) -> list[str]:
    """这条路由对外公布的分辨率，也是代理执行时用来校验的列表；空列表表示不限。

    路由上配置了分辨率就用配置的，否则沿用渠道默认值，与 ``capabilities_for``
    公布的 ``resolutions`` 同源——调用方在 ``/v1/model-capabilities`` 看到的
    选项就是这里放行的选项。
    """
    if resolution_overrides:
        return [
            value.strip()
            for value in resolution_overrides
            if value and value.strip() and value.strip() != '自动'
        ]
    capabilities = PROFILE_DEFINITIONS.get(profile or '', {}).get('capabilities', {})
    return list(capabilities.get('resolutions') or [])


# 常见视频分辨率档位。``size`` 只有在两个边长都属于这些档位、又确实是一个视频
# 尺寸时才当分辨率读：``1280x720`` / ``1920*1080`` 是分辨率，图片里的
# ``1280x32``、``2x2`` 不是，不能因为尺寸里碰巧有个数字就拒掉请求。
_RESOLUTION_TIERS = frozenset({'480p', '540p', '720p', '768p', '1080p', '1440p', '2160p'})


def _size_resolution(value: Any) -> str | None:
    """把 ``1280x720`` 读成 ``720p``；尺寸不像视频分辨率时返回 ``None``。"""
    if not isinstance(value, str):
        return None
    size = re.fullmatch(r'(\d{2,5})\s*[x*×]\s*(\d{2,5})', value.strip().lower())
    if size is None:
        return None
    width, height = int(size.group(1)), int(size.group(2))
    if width <= 0 or height <= 0:
        return None
    long_side, short_side = max(width, height), min(width, height)
    # 只有一边必须落在档位上，另一边是按比例算出来的（1280x720 → 720p）。
    if f'{short_side}p' not in _RESOLUTION_TIERS or long_side < short_side:
        return None
    return f'{short_side}p'


def _resolution_requested(value: Any) -> str | None:
    """把一个分辨率的写法规范成 ``720p`` 这种形式；不是分辨率就返回 ``None``。

    调用方常用大写（``720P``），也常把方向或尺寸写在一起（``768p横``、
    ``1280x720``、``1920*1080``），这些写法都按同一个档位比较，避免同一件事
    因为写法不同被拒。
    """
    if not isinstance(value, str):
        return None
    match = _RESOLUTION_VALUE.match(value.strip().lower())
    if match is None:
        return None
    return f'{match.group(1)}p'


def _resolution_fields(payload: Mapping[str, Any]) -> list[tuple[str, str]]:
    """只列 ``resolution`` 那三个专门的分辨率字段（不看 ``quality`` / ``size``）。"""
    metadata = payload.get('metadata') if isinstance(payload.get('metadata'), dict) else {}
    parameters = metadata.get('parameters') if isinstance(metadata.get('parameters'), dict) else {}
    fields = (
        ('resolution', payload.get('resolution')),
        ('metadata.resolution', metadata.get('resolution')),
        ('metadata.parameters.resolution', parameters.get('resolution')),
    )
    return [(field, str(value)) for field, value in fields if value is not None and value != '']


def resolution_candidates(payload: Mapping[str, Any]) -> list[tuple[str, str]]:
    """列出请求里所有会被渠道适配器当成分辨率读取的字段。

    ``quality`` 只在写成 ``720p`` 这种形式时才当分辨率（``high`` 是画质词，放行），
    ``size`` 只在整体像视频尺寸时才当分辨率（图片的 ``1280x32`` 放行，不拒）。
    分辨率字段本身填了别的东西也要报出来：调用方能立刻知道自己写错了字段。
    """
    metadata = payload.get('metadata') if isinstance(payload.get('metadata'), dict) else {}
    result = _resolution_fields(payload)
    quality = payload.get('quality')
    if quality is not None and _resolution_requested(quality) is not None:
        result.append(('quality', str(quality)))
    size = payload.get('size') or metadata.get('size')
    if _size_resolution(size) is not None:
        result.append(('size', str(size)))
    return result


def _format_resolutions(resolutions: list[str]) -> str:
    return '、'.join(resolutions)


def enforce_resolution(
    payload: Mapping[str, Any],
    allowed: list[str],
) -> None:
    """请求的分辨率不在这条路由支持的分辨率列表里就拒绝，而不是透传给上游。

    透传的后果要么是上游报错，要么是上游按别的分辨率出片——用户以为付的是
    1080p 的钱，拿到的却是上游默认档位的画面。没传分辨率时不做处理：由路由
    自己的固定分辨率（``apply_pinned_resolution``）或上游默认值决定。

    比较时容忍同一件事的不同写法（``720P``、``1280x720``）。配置项如果本身就带
    额外信息（AutoDL 的 ``768p横`` 把方向写进了同一串），就按原样精确比较：把
    它降级成 ``768p`` 会让横竖两个档位互相冒充。
    """
    if not allowed:
        return
    exact = {value.strip().lower() for value in allowed}
    bare_tiers = {
        _resolution_requested(value)
        for value in allowed
        if _resolution_requested(value) == value.strip().lower()
    }
    for field, value in resolution_candidates(payload):
        normalized = value.strip().lower()
        wanted = _resolution_requested(value) or _size_resolution(value)
        if normalized in exact or (wanted is not None and wanted in bare_tiers):
            continue
        if wanted is None:
            raise ResolutionNotAllowedError(
                f'{field} 必须是 480p、720p、1080p 这样的分辨率，收到 {value}；'
                f'当前模型只支持：{_format_resolutions(allowed)}'
            )
        raise ResolutionNotAllowedError(
            f'当前模型不支持分辨率 {value}，只支持：{_format_resolutions(allowed)}'
        )


def apply_pinned_resolution(
    payload: dict[str, Any],
    resolutions: list[str] | None,
) -> dict[str, Any]:
    """只配了一个分辨率的路由，调用方没传分辨率时补上它。

    按分辨率拆分出来的路由（``wan3.0-video-720p``）共用同一个上游模型，区别只在
    分辨率；MAI Token 这类上游还要求请求体里必须带 ``resolution``。两种情况都
    需要一个明确的档位，而路由配置的唯一选项就是它。是否拒绝别的分辨率由
    ``enforce_resolution`` 按同一份配置决定。
    """
    pinned = pinned_resolution(resolutions)
    if pinned is None:
        return payload
    # 真正的分辨率字段里已经写了档位，就按调用方的选择：不重复下发，也不覆盖
    # 它在 ``metadata`` 里的写法。``quality`` / ``size`` 不算——渠道未必把它们
    # 当分辨率读（扶摇通用档就不读 ``size``），当作"已指定"会让请求漏掉分辨率。
    if _resolution_fields(payload):
        return payload
    return {**payload, 'resolution': pinned}


class DurationNotAllowedError(Exception):
    """请求的时长不在该路由公布的时长列表里。"""


def allowed_durations(profile: str, duration_overrides: list[int] | int | None = None) -> list[int]:
    """这条路由对外公布的时长，也是代理执行时用来校验的列表；空列表表示不限制。

    路由上配置了时长就用配置的，否则沿用渠道默认值，与 ``capabilities_for``
    公布的 ``durations`` 同源。
    """
    if isinstance(duration_overrides, int):
        duration_overrides = [duration_overrides]
    if duration_overrides:
        return list(duration_overrides)
    capabilities = PROFILE_DEFINITIONS.get(profile, {}).get('capabilities', {})
    return list(capabilities.get('durations') or [])


def _requested_duration_values(payload: dict[str, Any]) -> list[tuple[str, Any]]:
    """列出请求里所有会被渠道适配器当成时长读取的字段。"""
    metadata = payload.get('metadata') if isinstance(payload.get('metadata'), dict) else {}
    parameters = metadata.get('parameters') if isinstance(metadata.get('parameters'), dict) else {}
    candidates = (
        ('duration', payload.get('duration')),
        ('seconds', payload.get('seconds')),
        ('metadata.duration', metadata.get('duration')),
        ('metadata.seconds', metadata.get('seconds')),
        ('metadata.parameters.duration', parameters.get('duration')),
        ('metadata.parameters.seconds', parameters.get('seconds')),
    )
    return [(field, value) for field, value in candidates if value is not None and value != '']


def _parse_duration(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else None


def enforce_duration(
    payload: dict[str, Any],
    allowed: list[int],
    extra_allowed: list[int] | tuple[int, ...] = (),
) -> None:
    """请求的时长不在这条路由支持的列表里就拒绝，而不是透传给上游。

    透传的后果要么是上游报错，要么是上游按别的时长出片（不少渠道会把不支持的
    时长悄悄改成默认值），用户付了一个任务的钱拿到另一个任务的结果。没传时长时
    不做处理，由上游使用默认时长。``extra_allowed`` 放渠道认可但不对外公布的
    特殊值，例如 Wan3 的 ``-1``（自动时长）。
    """
    if not allowed:
        return
    permitted = set(allowed) | set(extra_allowed)
    for field, value in _requested_duration_values(payload):
        duration = _parse_duration(value)
        if duration is None:
            raise DurationNotAllowedError(f'{field} 必须是整数秒，收到 {value!r}')
        if duration not in permitted:
            raise DurationNotAllowedError(
                f'当前模型不支持 {duration} 秒，可选时长：{_format_durations(allowed)}'
            )


def _format_durations(durations: list[int]) -> str:
    """把 ``[4, 5, 6, 8]`` 写成 ``4–6、8 秒``，长区间不至于刷屏。"""
    values = sorted(set(durations))
    ranges: list[str] = []
    start = previous = values[0]
    for value in values[1:] + [None]:
        if value is not None and value == previous + 1:
            previous = value
            continue
        ranges.append(str(start) if start == previous else f'{start}–{previous}')
        if value is not None:
            start = previous = value
    return '、'.join(ranges) + ' 秒'


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
    **fuyao.PROFILE_DEFINITIONS,
    **wangyue.PROFILE_DEFINITIONS,
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
    if protocol == fuyao.PROTOCOL:
        return fuyao.suggest_route(model)['profile']
    if protocol == wangyue.PROTOCOL:
        return wangyue.suggest_route(model)['profile']
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
    if protocol == fuyao.PROTOCOL:
        return fuyao.suggest_route(model)
    if protocol == wangyue.PROTOCOL:
        return wangyue.suggest_route(model)
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
    capabilities['durations'] = allowed_durations(profile, duration_overrides)
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
    if request_format == fuyao.PROFILE:
        return fuyao.transform_create_payload(payload, profile)
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
