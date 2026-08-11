from __future__ import annotations

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


def build_image_integration_document(base_url: str, upstreams: list[dict[str, Any]]) -> str:
    base_url = base_url.rstrip("/")
    models = _configured_models(upstreams)
    lines = [
        "# 图片接口对接文档",
        "",
        f"> Base URL：`{_inline(base_url)}`  ",
        "> 协议：OpenAI Images 兼容接口  ",
        f"> 生成日期：{date.today().isoformat()}",
        "",
        "## 鉴权和返回结果",
        "",
        "图片生成、图片编辑和模型列表接口需要携带 API Key：",
        "",
        "```http",
        "Authorization: Bearer <API_KEY>",
        "```",
        "",
        "返回的 `data[].url` 是免鉴权公开图片链接，默认保留 7 天，之后链接失效。",
        "`b64_json` 内容不会被改写。",
        "",
        "## 接口地址",
        "",
        "| 操作 | 方法与地址 | 鉴权 |",
        "| --- | --- | --- |",
        f"| 获取模型 | `GET {base_url}/v1/models` | 需要 |",
        f"| 生成图片 | `POST {base_url}/v1/images/generations` | 需要 |",
        f"| 编辑图片 | `POST {base_url}/v1/images/edits` | 需要 |",
        f"| 下载脱敏图片 | `GET {base_url}/public/images/assets/{{asset_id}}` | 不需要 |",
        "",
        "## 生成图片",
        "",
        "```bash",
        f"curl \"{base_url}/v1/images/generations\" \\",
        "  -H \"Authorization: Bearer <API_KEY>\" \\",
        "  -H \"Content-Type: application/json\" \\",
        '  -d \'{"model":"<PUBLIC_MODEL>","prompt":"a clean product photo","size":"1024x1024","quality":"standard"}\'',
        "```",
        "",
        "## 编辑图片",
        "",
        "```bash",
        f"curl \"{base_url}/v1/images/edits\" \\",
        "  -H \"Authorization: Bearer <API_KEY>\" \\",
        "  -F \"model=<PUBLIC_MODEL>\" \\",
        "  -F \"prompt=remove the background\" \\",
        "  -F \"image=@input.png\"",
        "```",
        "",
        "## 已配置模型",
        "",
        "| 对外模型名 | 尺寸 | 质量 | 操作 |",
        "| --- | --- | --- | --- |",
    ]
    if models:
        for model in models:
            lines.append(
                f"| `{_inline(model['model'])}` | {', '.join(_inline(value) for value in model['sizes']) or '-'} | "
                f"{', '.join(_inline(value) for value in model['qualities']) or '-'} | "
                f"{', '.join(_inline(value) for value in model['operations']) or '-'} |"
            )
    else:
        lines.append("| 暂无启用模型 | - | - | - |")
    lines.extend(
        [
            "",
            "## 常见错误",
            "",
            "| 状态码 | 处理建议 |",
            "| --- | --- |",
            "| `401` | 检查 API Key 和 Authorization 请求头 |",
            "| `404` | 检查模型、尺寸、质量或资源 ID 是否正确 |",
            "| `415` | 生成接口使用 JSON，编辑接口使用 multipart/form-data |",
            "| `502` | 稍后重试或联系服务管理员 |",
        ]
    )
    return "\n".join(lines) + "\n"
