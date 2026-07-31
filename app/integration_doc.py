from __future__ import annotations

import json
from datetime import date
from typing import Any


def _table_text(values: list[Any]) -> str:
    if not values:
        return "-"
    return ", ".join(
        str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")
        for value in values
    )


def _inline_code(value: Any) -> str:
    return str(value).replace("`", "\\`").replace("\r", " ").replace("\n", " ")


def _request_example(model: dict[str, Any]) -> dict[str, Any]:
    capabilities = model["capabilities"]
    ratios = capabilities.get("ratios", [])
    durations = capabilities.get("durations", [])
    resolutions = capabilities.get("resolutions", [])
    payload: dict[str, Any] = {
        "model": model["id"],
        "prompt": "你的视频提示词",
    }
    if ratios and ratios[0] != "自动":
        payload["aspect_ratio"] = ratios[0]
    if durations and durations[0]:
        payload["duration"] = durations[0]
    if resolutions and resolutions[0] != "自动":
        payload["resolution"] = resolutions[0]
    payload["generate_audio"] = True
    if capabilities.get("maxImages", 0):
        payload["image_urls"] = ["https://example.com/reference.png"]
    if capabilities.get("referenceVideo"):
        payload["reference_video"] = "https://example.com/reference.mp4"
    if capabilities.get("maxAudios", 0):
        payload["audio_urls"] = ["https://example.com/reference.mp3"]
    return payload


def build_integration_document(base_url: str, models: list[dict[str, Any]]) -> str:
    lines = [
        "# 视频接口接入文档",
        "",
        f"> Base URL：`{base_url}`  ",
        "> 协议：OpenAI Videos 兼容接口  ",
        f"> 生成日期：{date.today().isoformat()}",
        "",
        "## 接入流程",
        "",
        "1. 使用 API Key 创建视频任务。",
        "2. 保存创建响应中的 `task_id`。",
        "3. 每 10-15 秒查询任务状态。",
        "4. 状态变为 `completed` 后下载视频。",
        "",
        "所有请求均需携带：",
        "",
        "```http",
        "Authorization: Bearer sk-你的API令牌",
        "```",
        "",
        "## 接口地址",
        "",
        "| 操作 | 方法与地址 |",
        "| --- | --- |",
        f"| 获取模型 | `GET {base_url}/v1/models` |",
        f"| 创建视频 | `POST {base_url}/v1/videos` |",
        f"| 查询任务 | `GET {base_url}/v1/videos/{{task_id}}` |",
        f"| 下载视频 | `GET {base_url}/v1/videos/{{task_id}}/content` |",
        "",
        "## 当前开放模型",
        "",
        "| 对外模型名 | 画面比例 | 时长 | 分辨率 | 图片 | 视频 | 音频 |",
        "| --- | --- | --- | --- | ---: | --- | ---: |",
    ]
    if models:
        for model in models:
            capabilities = model["capabilities"]
            lines.append(
                f"| `{_inline_code(model['id']).replace('|', '&#124;')}` | "
                f"{_table_text(capabilities.get('ratios', []))} | "
                f"{_table_text([f'{value}s' if value else '自动' for value in capabilities.get('durations', [])])} | "
                f"{_table_text(capabilities.get('resolutions', []))} | "
                f"{capabilities.get('maxImages', 0)} | "
                f"{'支持' if capabilities.get('referenceVideo') else '不支持'} | "
                f"{capabilities.get('maxAudios', 0)} |"
            )
    else:
        lines.append("| 暂无启用模型 | - | - | - | 0 | 不支持 | 0 |")

    lines.extend([
        "",
        "模型列表和能力由后台配置实时生成，请以鉴权后的 `/v1/models` 返回结果为准。",
        "",
        "## 创建任务",
        "",
        f"`POST {base_url}/v1/videos`",
        "",
        "请求头：",
        "",
        "```http",
        "Authorization: Bearer sk-你的API令牌",
        "Content-Type: application/json",
        "```",
    ])

    for model in models:
        lines.extend([
            "",
            f"### `{_inline_code(model['id'])}`",
            "",
            "```json",
            json.dumps(_request_example(model), ensure_ascii=False, indent=2),
            "```",
        ])

    lines.extend([
        "",
        "素材 URL 必须是服务端可直接访问的公开 HTTP/HTTPS 地址。不使用的素材字段可以删除。",
        "",
        "## 查询任务",
        "",
        "```bash",
        f"curl \"{base_url}/v1/videos/task_xxx\" \\",
        "  -H \"Authorization: Bearer sk-你的API令牌\"",
        "```",
        "",
        "任务状态包括 `queued`、`processing`、`completed` 和 `failed`。",
        "",
        "## 下载视频",
        "",
        "```bash",
        f"curl -L \"{base_url}/v1/videos/task_xxx/content\" \\",
        "  -H \"Authorization: Bearer sk-你的API令牌\" \\",
        "  -o output.mp4",
        "```",
        "",
    ])
    return "\n".join(lines)
