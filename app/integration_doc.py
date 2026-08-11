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


def _model_capability_section(model: dict[str, Any]) -> list[str]:
    capabilities = model["capabilities"]
    durations = [f"{value}s" if value else "自动" for value in capabilities.get("durations", [])]
    lines = [
        f"### `{_inline_code(model['id'])}`",
        "",
        "| 参数 | 支持范围 |",
        "| --- | --- |",
        f"| 分辨率 | {_table_text(capabilities.get('resolutions', []))} |",
        f"| 比例 | {_table_text(capabilities.get('ratios', []))} |",
        f"| 时长 | {_table_text(durations)} |",
        f"| 参考图片 | 最多 {capabilities.get('maxImages', 0)} 张 |",
        f"| 参考视频 | {'支持' if capabilities.get('referenceVideo') else '不支持'} |",
        f"| 参考音频 | {'支持（最多 ' + str(capabilities.get('maxAudios', 0)) + ' 个）' if capabilities.get('maxAudios', 0) else '不支持'} |",
    ]
    if capabilities.get("maxReferences"):
        lines.append(f"| 图片、视频和音频总数 | 最多 {capabilities['maxReferences']} 个 |")
    if capabilities.get("minReferenceVideoDuration") is not None:
        lines.append(
            f"| 参考视频时长 | {capabilities['minReferenceVideoDuration']}-{capabilities['maxReferenceVideoDuration']} 秒 |"
        )
    if capabilities.get("minAudioDuration") is not None:
        lines.append(
            f"| 单个参考音频时长 | {capabilities['minAudioDuration']}-{capabilities['maxAudioDuration']} 秒 |"
        )
    if capabilities.get("maxTotalAudioDuration") is not None:
        lines.append(f"| 参考音频总时长 | 不超过 {capabilities['maxTotalAudioDuration']} 秒 |")
    return lines


def build_integration_document(base_url: str, models: list[dict[str, Any]]) -> str:
    example_model = models[0] if models else {"id": "模型名", "capabilities": {}}
    example_capabilities = example_model["capabilities"]
    example_durations = [value for value in example_capabilities.get("durations", []) if value]
    example_ratios = [value for value in example_capabilities.get("ratios", []) if value != "自动"]
    example_resolutions = [value for value in example_capabilities.get("resolutions", []) if value != "自动"]
    example_duration = example_durations[0] if example_durations else 10
    example_ratio = example_ratios[0] if example_ratios else "16:9"
    example_resolution = example_resolutions[0] if example_resolutions else "720p"
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
        "创建和查询接口需携带 API Key；任务完成后的公开下载链接无需鉴权。",
        "",
        "```http",
        "Authorization: Bearer sk-你的API令牌",
        "```",
        "",
        "## 通用请求规则",
        "",
        "- 请求体只接受 `application/json`。",
        "- `model` 和 `prompt` 必填。",
        "- 本接口不接受 `multipart/form-data` 文件上传。",
        "- 参考图片、视频和音频必须是服务端可访问的公网 HTTP/HTTPS URL。",
        "- 时长使用 `duration` 字段，比例使用 `aspect_ratio`，分辨率使用 `resolution`。",
        "- `generate_audio` 表示是否生成音频，与上传参考音频不是同一个功能。",
        "- 客户端应先读取 `/v1/models`，不要永久写死模型列表。",
        "",
        "### 提示词限制",
        "",
        "不要在 `prompt` 中填写视频时长、分镜时间码或画面比例。",
        "时长、比例和分辨率请使用对应的请求字段。",
        "",
        "错误示例：",
        "",
        "```json",
        json.dumps(
            {
                "model": example_model["id"],
                "prompt": "生成一个10秒的16:9视频，0s-2s展示人物",
                "duration": 10,
                "aspect_ratio": "16:9",
            },
            ensure_ascii=False,
            indent=2,
        ),
        "```",
        "",
        "正确示例：",
        "",
        "```json",
        json.dumps(
            {
                "model": example_model["id"],
                "prompt": "电影感角色展示，人物出场后镜头环绕，最终定格。",
                "duration": example_duration,
                "aspect_ratio": example_ratio,
                "resolution": example_resolution,
            },
            ensure_ascii=False,
            indent=2,
        ),
        "```",
        "",
        "## 接口地址",
        "",
        "| 操作 | 方法与地址 |",
        "| --- | --- |",
        f"| 获取模型 | `GET {base_url}/v1/models` |",
        f"| 创建视频 | `POST {base_url}/v1/videos` |",
        f"| 查询任务 | `GET {base_url}/v1/videos/{{task_id}}` |",
        f"| 下载视频 | `GET {base_url}/public/videos/{{task_id}}/content`（免鉴权，24 小时有效，默认最多 50 次） |",
        "",
        "## 当前开放模型",
        "",
        "| 对外模型名 | 画面比例 | 时长 | 分辨率 | 图片数量 | 视频 | 音频 |",
        "| --- | --- | --- | --- | ---: | --- | --- |",
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
                f"{'支持' if capabilities.get('maxAudios', 0) else '不支持'} |"
            )
    else:
        lines.append("| 暂无启用模型 | - | - | - | 0 | 不支持 | 不支持 |")

    lines.extend([
        "",
        "模型列表和能力由后台配置实时生成，请以鉴权后的 `/v1/models` 返回结果为准。",
        "",
        "## 模型请求示例",
    ])

    for model in models:
        lines.extend([
            "",
            *_model_capability_section(model),
            "",
            "请求示例：",
            "",
            "```json",
            json.dumps(_request_example(model), ensure_ascii=False, indent=2),
            "```",
        ])

    lines.extend([
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
        "创建响应中的任务 ID 必须保存。后续轮询优先使用创建响应中的 `task_id`，若响应只有 `id` 则使用 `id`。",
        "不要用轮询响应中的内部字段覆盖创建时保存的公开任务 ID。",
        "",
        "任务状态包括 `queued`、`processing`、`completed` 和 `failed`。",
        "",
        "| 状态 | 处理 |",
        "| --- | --- |",
        "| `queued` / `pending` | 继续轮询 |",
        "| `processing` / `in_progress` | 继续轮询 |",
        "| `completed` | 下载视频 |",
        "| `failed` / `cancelled` | 停止轮询并记录错误 |",
        "",
        "## 下载视频",
        "",
        "```bash",
        f"curl -L \"{base_url}/public/videos/task_xxx/content\" \\",
        "  -o output.mp4",
        "```",
        "",
        "下载成功后建议检查 HTTP 状态、`Content-Type` 是否为 `video/*`、文件大小是否大于零，并确认文件可以被播放器打开。",
        "",
        "## Python 示例",
        "",
        "```python",
        "import time",
        "from pathlib import Path",
        "import requests",
        "",
        f'BASE_URL = "{base_url}"',
        'API_KEY = "sk-你的API令牌"',
        'HEADERS = {"Authorization": f"Bearer {API_KEY}"}',
        "payload = {",
        f'    "model": "{_inline_code(example_model["id"])}",',
        '    "prompt": "电影感角色展示，无字幕无水印。",',
        f'    "duration": {example_duration},',
        f'    "aspect_ratio": "{example_ratio}",',
        f'    "resolution": "{example_resolution}",',
        "}",
        'created = requests.post(f"{BASE_URL}/v1/videos", headers={**HEADERS, "Content-Type": "application/json"}, json=payload, timeout=(10, 120))',
        "created.raise_for_status()",
        "body = created.json()",
        'task_id = body.get("task_id") or body.get("id")',
        'if not task_id: raise RuntimeError("create response has no task id")',
        "",
        "for _ in range(5760):",
        '    response = requests.get(f"{BASE_URL}/v1/videos/{task_id}", headers=HEADERS, timeout=(10, 30))',
        "    response.raise_for_status()",
        "    task = response.json()",
        '    if task.get("status") == "completed": break',
        '    if task.get("status") in {"failed", "cancelled"}: raise RuntimeError(task.get("error") or "video generation failed")',
        "    time.sleep(15)",
        "else:",
        '    raise TimeoutError("video task did not finish")',
        "",
        'with requests.get(f"{BASE_URL}/public/videos/{task_id}/content", stream=True, timeout=(10, 120)) as response:',
        "    response.raise_for_status()",
        '    if not response.headers.get("Content-Type", "").lower().startswith("video/"): raise RuntimeError("unexpected content type")',
        '    with Path("result.mp4").open("wb") as output:',
        "        for chunk in response.iter_content(chunk_size=256 * 1024):",
        "            if chunk: output.write(chunk)",
        "```",
        "",
        "## 常见错误",
        "",
        "| 错误 | 处理 |",
        "| --- | --- |",
        "| `401` | 检查 API Key 和 Authorization 请求头 |",
        "| `400` | 检查模型、时长、比例、分辨率和参考素材数量 |",
        "| `404` / `Task not found` | 检查是否使用了创建响应中的任务 ID |",
        "| `409 Video is not completed` | 等状态变为 `completed` 后再下载 |",
        "| `429` | 降低请求频率，稍后重试 |",
        "| `502` | 检查上游状态并联系服务管理员 |",
        "",
        "创建请求在结果未知时不要自动重放，避免重复创建任务。",
        "",
        "## 接入验收",
        "",
        "1. 能获取实时模型列表。",
        "2. 能保存创建响应中的任务 ID。",
        "3. 能持续轮询到终态。",
        "4. 失败后能停止轮询并显示错误。",
        "5. 完成后能下载并校验视频文件。",
        "6. 创建请求不会因客户端自动重试而重复提交。",
        "",
    ])
    return "\n".join(lines)
