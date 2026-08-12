from __future__ import annotations

import json
from datetime import date
from typing import Any


def _inline(value: Any) -> str:
    return str(value).replace("`", "\\`").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _configured_models(upstreams: list[dict[str, Any]]) -> list[dict[str, Any]]:
    models: dict[str, dict[str, set[str]]] = {}
    for upstream in upstreams:
        if not upstream.get("enabled"):
            continue
        for route in upstream.get("routes", []):
            public_model = str(route.get("public_model", "")).strip()
            if not public_model:
                continue
            item = models.setdefault(public_model, {"sizes": set(), "qualities": set(), "operations": set()})
            item["sizes"].update(str(value) for value in route.get("sizes", []))
            item["qualities"].update(str(value) for value in route.get("qualities", []))
            item["operations"].update(str(value) for value in route.get("operations", []))
    return [
        {
            "model": model,
            "sizes": sorted(values["sizes"]),
            "qualities": sorted(values["qualities"]),
            "operations": sorted(values["operations"]),
        }
        for model, values in sorted(models.items())
    ]


def _display_constraints(values: list[str]) -> str:
    if not values or "*" in values:
        return "由模型决定"
    return ", ".join(_inline(value) for value in values)


def _display_operations(values: list[str]) -> str:
    labels = {"generation": "图片生成", "edit": "图片编辑"}
    ordered = [name for name in ("generation", "edit") if name in values]
    return ", ".join(labels.get(value, _inline(value)) for value in ordered) or "-"


def _example_model(models: list[dict[str, Any]]) -> str:
    for model in models:
        if not model["model"].startswith("["):
            return model["model"]
    return models[0]["model"] if models else "PUBLIC_MODEL"


def build_image_integration_document(
    base_url: str,
    upstreams: list[dict[str, Any]],
    public_base_url: str | None = None,
) -> str:
    base_url = base_url.rstrip("/")
    public_base_url = (public_base_url or base_url).rstrip("/")
    models = _configured_models(upstreams)
    example_model = _example_model(models)
    generation_request = {
        "model": example_model,
        "prompt": "一只坐在窗边的橘猫，电影感光影，细节清晰",
        "size": "1024x1024",
        "quality": "standard",
        "n": 1,
    }
    url_response = {
        "created": 1786400000,
        "data": [{"url": f"{public_base_url}/public/images/assets/img_xxx"}],
    }
    base64_response = {
        "created": 1786400000,
        "data": [{"b64_json": "iVBORw0KGgoAAAANSUhEUgAA..."}],
    }
    lines = [
        "# 图片接口对接文档",
        "",
        f"> Base URL：`{_inline(base_url)}`  ",
        "> 协议：OpenAI Images 兼容接口  ",
        f"> 生成日期：{date.today().isoformat()}",
        "",
        "## 快速接入",
        "",
        "1. 准备平台提供的 API Key。",
        "2. 从本文末尾的模型表中选择公开模型名。",
        "3. 图片生成使用 JSON；图片编辑使用 multipart/form-data。",
        "4. 成功响应位于 `data` 数组，可返回公开图片 URL 或 Base64 图片数据。",
        "",
        "## 鉴权与请求头",
        "",
        "除公开图片下载地址外，其余接口均需携带：",
        "",
        "```http",
        "Authorization: Bearer <API_KEY>",
        "```",
        "",
        "| 请求头 | 必填 | 说明 |",
        "| --- | --- | --- |",
        "| `Authorization` | 是 | 使用 `Bearer <API_KEY>` 格式 |",
        "| `Content-Type` | 是 | 图片生成使用 `application/json`；图片编辑使用 `multipart/form-data` |",
        "| `Idempotency-Key` | 否 | 自定义唯一字符串，用于降低网络重试造成重复请求的风险 |",
        "",
        "## 接口一览",
        "",
        "| 操作 | 方法与地址 | 鉴权 |",
        "| --- | --- | --- |",
        f"| 获取模型 | `GET {base_url}/v1/models` | 需要 |",
        f"| 生成图片 | `POST {base_url}/v1/images/generations` | 需要 |",
        f"| 编辑图片 | `POST {base_url}/v1/images/edits` | 需要 |",
        f"| 访问公开图片 | `GET {public_base_url}/public/images/assets/{{asset_id}}` | 不需要 |",
        "",
        "## 图片生成",
        "",
        f"`POST {base_url}/v1/images/generations`",
        "",
        "请求体必须是 JSON 对象。未使用的可选字段可以省略；具体能力以所选模型为准。",
        "",
        "### 请求参数",
        "",
        "| 参数 | 类型 | 必填 | 说明 |",
        "| --- | --- | --- | --- |",
        "| `model` | string | 是 | 公开模型名，使用本文模型表中的值 |",
        "| `prompt` | string | 是 | 图片描述或生成要求 |",
        "| `n` | integer | 否 | 生成数量，通常默认为 `1`；是否支持多图由模型决定 |",
        "| `size` | string | 否 | 输出尺寸，例如 `1024x1024`、`1536x1024`；可用值见模型表 |",
        "| `resolution` | string | 否 | `size` 的兼容字段；优先使用 `size` |",
        "| `quality` | string | 否 | 输出质量，例如 `standard`、`high`、`medium`、`low`；由模型决定 |",
        "| `response_format` | string | 否 | `url` 或 `b64_json`；默认行为由模型决定 |",
        "| `background` | string | 否 | 背景模式，例如 `auto`、`transparent`、`opaque`；由模型决定 |",
        "| `output_format` | string | 否 | 图片格式，例如 `png`、`jpeg`、`webp`；由模型决定 |",
        "| `output_compression` | integer | 否 | JPEG/WebP 压缩质量，通常为 `0-100`；由模型决定 |",
        "| `moderation` | string | 否 | 内容审核级别，例如 `auto`；由模型决定 |",
        "| `style` | string | 否 | 风格选项，例如 `vivid`、`natural`；仅部分模型支持 |",
        "| `user` | string | 否 | 调用方用户标识，用于请求追踪 |",
        "",
        "### JSON 请求示例",
        "",
        "```json",
        json.dumps(generation_request, ensure_ascii=False, indent=2),
        "```",
        "",
        "### cURL 示例",
        "",
        "```bash",
        f"curl \"{base_url}/v1/images/generations\" \\",
        "  -H \"Authorization: Bearer <API_KEY>\" \\",
        "  -H \"Content-Type: application/json\" \\",
        f"  -d '{json.dumps(generation_request, ensure_ascii=False, separators=(',', ':'))}'",
        "```",
        "",
        "## 图片编辑",
        "",
        f"`POST {base_url}/v1/images/edits`",
        "",
        "请求必须使用 multipart/form-data。可以重复提交同名 `image` 字段上传多张图片，但模型是否支持多图由模型能力决定。",
        "",
        "### 请求参数",
        "",
        "| 参数 | 类型 | 必填 | 说明 |",
        "| --- | --- | --- | --- |",
        "| `model` | string | 是 | 支持图片编辑的公开模型名 |",
        "| `image` | file | 是 | 待编辑图片；文件类型和大小限制由模型决定 |",
        "| `prompt` | string | 是 | 编辑要求，例如换背景、增删元素或调整风格 |",
        "| `mask` | file | 否 | 遮罩图片；透明区域通常表示需要修改的位置 |",
        "| `n` | integer | 否 | 输出数量，通常默认为 `1` |",
        "| `size` | string | 否 | 输出尺寸；可用值见模型表 |",
        "| `quality` | string | 否 | 输出质量；可用值见模型表 |",
        "| `response_format` | string | 否 | `url` 或 `b64_json` |",
        "| `background` | string | 否 | 背景模式；仅部分模型支持 |",
        "| `input_fidelity` | string | 否 | 输入图像保真度，例如 `low`、`high`；仅部分模型支持 |",
        "| `output_format` | string | 否 | 输出格式；仅部分模型支持 |",
        "| `output_compression` | integer | 否 | JPEG/WebP 压缩质量；仅部分模型支持 |",
        "| `user` | string | 否 | 调用方用户标识，用于请求追踪 |",
        "",
        "### cURL 示例",
        "",
        "```bash",
        f"curl \"{base_url}/v1/images/edits\" \\",
        "  -H \"Authorization: Bearer <API_KEY>\" \\",
        f"  -F \"model={_inline(example_model)}\" \\",
        "  -F \"prompt=保留主体，将背景替换为纯白色\" \\",
        "  -F \"image=@input.png\" \\",
        "  -F \"size=1024x1024\" \\",
        "  -F \"n=1\"",
        "```",
        "",
        "## 成功响应",
        "",
        "### URL 响应",
        "",
        "```json",
        json.dumps(url_response, ensure_ascii=False, indent=2),
        "```",
        "",
        "`data[].url` 为免鉴权公开图片地址，可以直接用于浏览器、`<img>` 或下载程序。链接默认保留 7 天。",
        "",
        "### Base64 响应",
        "",
        "```json",
        json.dumps(base64_response, ensure_ascii=False, indent=2),
        "```",
        "",
        "### 响应字段",
        "",
        "| 字段 | 类型 | 说明 |",
        "| --- | --- | --- |",
        "| `created` | integer | 响应创建时间，Unix 秒级时间戳 |",
        "| `data` | array | 图片结果数组 |",
        "| `data[].url` | string | 免鉴权公开图片地址，与 `b64_json` 通常二选一 |",
        "| `data[].b64_json` | string | Base64 编码图片，与 `url` 通常二选一 |",
        "| `data[].revised_prompt` | string | 模型修改后的提示词；仅部分模型返回 |",
        "| `usage` | object | 各类 token 或计费用量；仅部分模型返回 |",
        "",
        "## 公开图片访问",
        "",
        f"`GET {public_base_url}/public/images/assets/{{asset_id}}`",
        "",
        "该接口不需要 API Key，成功时直接返回图片二进制，不返回 JSON。响应 `Content-Type` 通常为 `image/png`、`image/jpeg` 或 `image/webp`。",
        "链接失效或资源不存在时返回 `404`。不要将公开图片接口误写成图片生成接口的 JSON 响应。",
        "",
        "## Python SDK 示例",
        "",
        "```python",
        "from openai import OpenAI",
        "",
        "client = OpenAI(",
        '    api_key="<API_KEY>",',
        f'    base_url="{base_url}/v1",',
        ")",
        "",
        "result = client.images.generate(",
        f'    model="{_inline(example_model)}",',
        '    prompt="一只坐在窗边的橘猫，电影感光影",',
        '    size="1024x1024",',
        "    n=1,",
        ")",
        "print(result.data[0].url or result.data[0].b64_json)",
        "```",
        "",
        "图片编辑示例：",
        "",
        "```python",
        "from openai import OpenAI",
        "",
        "client = OpenAI(",
        '    api_key="<API_KEY>",',
        f'    base_url="{base_url}/v1",',
        ")",
        "",
        'with open("input.png", "rb") as image_file:',
        "    result = client.images.edit(",
        f'        model="{_inline(example_model)}",',
        "        image=image_file,",
        '        prompt="保留主体，将背景替换为纯白色",',
        '        size="1024x1024",',
        "        n=1,",
        "    )",
        "print(result.data[0].url or result.data[0].b64_json)",
        "```",
        "",
        "## 已开放模型",
        "",
        "下表只展示对外公开模型能力；`由模型决定` 表示接口会接受该字段，但最终可用值取决于所选模型。",
        "",
        "| 对外模型名 | 支持尺寸 | 支持质量 | 支持操作 |",
        "| --- | --- | --- | --- |",
    ]
    if models:
        for model in models:
            lines.append(
                f"| `{_inline(model['model'])}` | {_display_constraints(model['sizes'])} | "
                f"{_display_constraints(model['qualities'])} | {_display_operations(model['operations'])} |"
            )
    else:
        lines.append("| 暂无启用模型 | - | - | - |")
    lines.extend(
        [
            "",
            "## 常见错误",
            "",
            "| 状态码 | 常见原因 | 处理建议 |",
            "| --- | --- | --- |",
            "| `400` | JSON 无效、缺少模型或字段类型错误 | 检查请求体是否为有效 JSON 对象，并确认参数类型 |",
            "| `401` | API Key 缺失、错误或已失效 | 检查 `Authorization: Bearer <API_KEY>` |",
            "| `402` | 余额或额度不足 | 充值或调整可用额度 |",
            "| `404` | 没有匹配的模型、尺寸或质量，或公开图片已失效 | 对照模型表检查参数；图片链接超过 7 天后需重新生成 |",
            "| `415` | Content-Type 不正确 | 图片生成使用 JSON；图片编辑使用 multipart/form-data |",
            "| `422` | 表单字段或上传文件不符合要求 | 检查字段名、文件类型和模型能力 |",
            "| `429` | 请求频率或并发超过限制 | 降低并发并按 `Retry-After` 稍后重试 |",
            "| `500` / `502` / `503` / `524` | 服务暂时不可用或生成超时 | 保留请求 ID，稍后重试或联系服务管理员 |",
            "",
            "响应头 `X-Oneapi-Request-Id` 可用于定位请求。向服务管理员反馈问题时，请一并提供该值、请求时间和公开模型名。",
        ]
    )
    return "\n".join(lines) + "\n"
