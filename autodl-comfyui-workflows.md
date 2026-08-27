# AutoDL.Art ComfyUI 工作流参数

> 数据来源：<https://autodl.art/large-model/comfyui>、各工作流的“在线调用/API”面板及官方 [ComfyUI API 文档](https://autodl.art/docs/comfyui_api/)。
> 抓取时间：2026-08-27（页面价格和字段约束可能变更；实际请求以页面在线表单校验为准）。

## 通用调用方式

AutoDL 将 ComfyUI 工作流的核心参数包装成异步 API。所有工作流都使用两步调用：先提交任务，再使用返回的 `task_id` 轮询结果。不同工作流之间主要只有 `workflow_id` 和请求体参数不同。

### 第一步：提交任务

```http
POST https://autodl.art/api/v1/comfyui/comfyui_workflow/{workflow_id}
Authorization: <你的 ComfyUI Token>
Content-Type: application/json
```

- `{workflow_id}` 替换成下文各工作流 ID。
- 请求体是该工作流对应的 JSON 参数。
- 该请求只提交任务，不会等待图片、视频或音频生成完成。

提交成功响应示例：

```json
{
  "code": "Success",
  "data": {
    "task_id": "2a25da1d-39ad-495c-8dac-bae8e8f6b1a1",
    "workflow": "H3文生视频",
    "status": "QUEUED",
    "client_id": "8c93a8000ef50e05d5314014756bd62c",
    "message": "工作流任务已提交",
    "created_at": "2026-08-18T11:33:02.456421825+08:00"
  },
  "msg": "",
  "request_id": "8c93a8000ef50e05d5314014756bd62c"
}
```

需要保存的关键字段是 `data.task_id`。

### 第二步：查询任务

```http
GET https://autodl.art/api/v1/comfyui/comfyui_workflow/result/{task_id}
Authorization: <你的 ComfyUI Token>
```

- GET 请求没有请求体，Headers 与提交任务相同。
- 官方示例每隔 1 秒查询一次。
- `QUEUED`：排队中；`RUNNING`：执行中；`SUCCESS`：成功；`FAILED`：失败。
- 当状态为 `SUCCESS` 时，从 `data.results` 获取资源；状态为 `FAILED` 时应终止轮询并记录完整响应。

查询响应示例：

```json
{
  "code": "Success",
  "data": {
    "task_id": "363ba3f5-f4fb-480c-afcf-b9410179c724",
    "status": "RUNNING",
    "client_id": "430e8d3d055baa169c23ba31e49a548d",
    "created_at": "2026-08-14 10:35:28",
    "started_at": "2026-08-14 10:35:30",
    "duration": 196,
    "results": []
  },
  "msg": "",
  "request_id": "8c93a8000ef50e05d5314014756bd62c"
}
```

成功后，`data.results` 是资源数组。视频结果通常包含以下字段，音频工作流的 `type`/`file_type` 通常为 `audio`/`wav`，并可能额外返回 `node_id`：

```json
{
  "url": "https://...",
  "type": "video",
  "file_type": "mp4",
  "output_type": "output"
}
```

官方特别提示：结果 URL 的有效期较短，查询成功后应尽快下载到自己的持久存储。

### 鉴权和媒体参数

- Token：在 <https://autodl.art/large-model/tokens> 创建，分组选择 `ComfyUI`。
- 官方文档直接把 Token 放入 `Authorization`，未要求添加 `Bearer ` 前缀：`{"Authorization": "你的Token"}`。
- 提交请求需使用 `Content-Type: application/json`。
- 图片、音频、视频字段支持 URL 或 base64；允许的 MIME 类型见各工作流参数表。

### Python 完整示例

下面以 `H3文生视频` 为例，包含提交、轮询、失败处理和超时控制：

```python
import json
import time

import requests

BASE_URL = "https://autodl.art/api/v1/comfyui/comfyui_workflow"
TOKEN = "请替换成自己的 ComfyUI Token"
WORKFLOW_ID = "minimax_h3_lightx2v_no_pic"

headers = {
    "Authorization": TOKEN,
    "Content-Type": "application/json",
}

body = {
    "prompt": "一只小猫在云端漫步，电影感运镜",
    "duration": 5,
    "resolution": "480p竖",
}

# 1. 提交任务
response = requests.post(
    f"{BASE_URL}/{WORKFLOW_ID}",
    headers=headers,
    json=body,
    timeout=60,
)
response.raise_for_status()
created = response.json()
if created.get("code") != "Success" or not created.get("data", {}).get("task_id"):
    raise RuntimeError(f"提交失败: {json.dumps(created, ensure_ascii=False)}")

task_id = created["data"]["task_id"]
deadline = time.monotonic() + 30 * 60

# 2. 轮询任务
while True:
    response = requests.get(
        f"{BASE_URL}/result/{task_id}",
        headers=headers,
        timeout=60,
    )
    response.raise_for_status()
    queried = response.json()
    if queried.get("code") != "Success":
        raise RuntimeError(f"查询失败: {json.dumps(queried, ensure_ascii=False)}")

    data = queried.get("data", {})
    status = data.get("status")
    print(f"任务状态: {status}, 已执行: {data.get('duration')} 秒")

    if status == "SUCCESS":
        print(json.dumps(data.get("results", []), ensure_ascii=False, indent=2))
        break
    if status == "FAILED":
        raise RuntimeError(f"任务失败: {json.dumps(queried, ensure_ascii=False)}")
    if time.monotonic() >= deadline:
        raise TimeoutError(f"任务查询超时: {task_id}")

    time.sleep(1)
```

## 工作流总览

| 名称 | workflow_id | 计费 |
|---|---|---|
| 动作迁移 | `wan2.2animate-v4-motion_retargeting` | 高峰 ￥0.04/秒；空闲 ￥0.03/秒，按实际视频时长 |
| H3多图多音频生视频15秒 | `minimax_h3_image_audio_to_video_v2_15s` | 480p/768p：高峰 ￥0.02/秒；空闲 ￥0.01/秒 |
| H3多图生视频15秒 | `minimax_h3_lightx2v_v5_15s` | 480p/768p：高峰 ￥0.02/秒；空闲 ￥0.01/秒 |
| H3多图多音频生视频 | `minimax_h3_image_audio_to_video_v2` | 480p/768p：￥0.02/￥0.01 秒；1080p：￥0.10/￥0.06 秒（高峰/空闲） |
| H3图生视频-音频同步（自动对口型） | `minimax_h3_image_audio_to_video` | 480p/768p：￥0.02/￥0.01 秒；1080p：￥0.09/￥0.05 秒 |
| H3多图参考生视频 | `minimax_h3_lightx2v_v5` | 480p/768p：￥0.02/￥0.01 秒；1080p：￥0.09/￥0.05 秒 |
| H3文生视频 | `minimax_h3_lightx2v_no_pic` | 480p/768p：高峰 ￥0.02/秒；空闲 ￥0.01/秒 |
| H3首尾帧生成视频 | `minimax_h3_lightx2v` | 480p/768p：高峰 ￥0.02/秒；空闲 ￥0.01/秒 |
| indextts2 | `indextts2-v1` | ￥0.02/次 |

查询接口对所有工作流相同：`/api/v1/comfyui/comfyui_workflow/result/{task_id}`。

## 1. 动作迁移

- 工作流 ID：`wan2.2animate-v4-motion_retargeting`
- 功能：将参考动作视频迁移到人物照片上。
- 提交接口：`/api/v1/comfyui/comfyui_workflow/wan2.2animate-v4-motion_retargeting`

| 参数 | 必填 | 类型/范围 | 说明 |
|---|---|---|---|
| `ref_image` | 是 | URL/base64；`image/jpeg`、`image/png`、`image/webp` | 人物参考图片 |
| `ref_video` | 是 | URL/base64；`video/mp4`、`video/webm` | 动作参考视频 |
| `seed` | 否 | 整数，`1-999999999999999` | 随机种子；相同输入和 seed 通常得到相近结果 |

## 2. H3多图多音频生视频15秒

- 工作流 ID：`minimax_h3_image_audio_to_video_v2_15s`
- 功能：多图、多音频参考生视频，最长 15 秒；页面提示该工作流包装较少，需要精确控制提示词。
- 提交接口：`/api/v1/comfyui/comfyui_workflow/minimax_h3_image_audio_to_video_v2_15s`

| 参数 | 必填 | 类型/范围 | 说明 |
|---|---|---|---|
| `prompt` | 是 | 长文本，长度 `1-10000` | 描述主体、动作、场景、镜头运动等 |
| `duration` | 否 | 整数，`1-15`，默认 `5` | 视频时长（秒） |
| `resolution` | 否 | 枚举，默认 `768p竖` | `480p竖`、`768p竖`、`480p横`、`768p横` |
| `ref_audio_0` - `ref_audio_2` | 否 | URL/base64；`audio/mpeg`、`audio/wav`、`audio/mp4`、`audio/flac` | 最多 3 个参考音频 |
| `ref_image_0` - `ref_image_8` | 否 | URL/base64；`image/jpeg`、`image/png`、`image/webp` | 最多 9 张参考图片 |
| `seed` | 否 | 数字，`1-999999999999999` | 随机种子 |

## 3. H3多图生视频15秒

- 工作流 ID：`minimax_h3_lightx2v_v5_15s`
- 功能：多图参考生视频，最长 15 秒。
- 提交接口：`/api/v1/comfyui/comfyui_workflow/minimax_h3_lightx2v_v5_15s`

| 参数 | 必填 | 类型/范围 | 说明 |
|---|---|---|---|
| `prompt` | 是 | 长文本，长度 `1-500000` | 视频生成提示词 |
| `duration` | 否 | 整数，`1-15`，默认 `5` | 视频时长（秒） |
| `resolution` | 否 | 枚举，默认 `768p竖` | `480p竖`、`480p横`、`768p竖`、`768p横`、`480p(1:1)`、`768p(1:1)` |
| `ref_image_0` | 是 | URL/base64；`image/jpeg`、`image/png`、`image/webp` | 第一张参考图片 |
| `ref_image_1` - `ref_image_8` | 否 | 同上 | 追加参考图片，最多共 9 张 |
| `seed` | 否 | 数字，`1-999999999999999` | 随机种子 |

## 4. H3多图多音频生视频

- 工作流 ID：`minimax_h3_image_audio_to_video_v2`
- 功能：多图、多音频参考生视频；页面提示该工作流包装较少，需要精确控制提示词。
- 提交接口：`/api/v1/comfyui/comfyui_workflow/minimax_h3_image_audio_to_video_v2`

| 参数 | 必填 | 类型/范围 | 说明 |
|---|---|---|---|
| `prompt` | 是 | 长文本，长度 `1-10000` | 视频生成提示词 |
| `duration` | 否 | 整数，`1-10`，默认 `5` | 视频时长（秒） |
| `resolution` | 否 | 枚举，默认 `768p竖` | `480p竖`、`768p竖`、`1080p竖`、`480p横`、`768p横`、`1080p横` |
| `ref_audio_0` - `ref_audio_2` | 否 | URL/base64；`audio/mpeg`、`audio/wav`、`audio/mp4`、`audio/flac` | 最多 3 个参考音频 |
| `ref_image_0` - `ref_image_8` | 否 | URL/base64；`image/jpeg`、`image/png`、`image/webp` | 最多 9 张参考图片 |
| `seed` | 否 | 数字，`1-999999999999999` | 随机种子 |

## 5. H3图生视频-音频同步（自动对口型）

- 工作流 ID：`minimax_h3_image_audio_to_video`
- 功能：根据单张图片和音频生成同步口型的视频。
- 提交接口：`/api/v1/comfyui/comfyui_workflow/minimax_h3_image_audio_to_video`

| 参数 | 必填 | 类型/范围 | 说明 |
|---|---|---|---|
| `ref_audio_0` | 是 | URL/base64；`audio/mpeg`、`audio/wav`、`audio/mp4`、`audio/flac` | 参考音频 |
| `ref_image_0` | 是 | URL/base64；`image/jpeg`、`image/png`、`image/webp` | 参考图片 |
| `audio_duration` | 否 | 整数，`1-15`，默认 `5` | 截取的音频时长（秒） |
| `resolution` | 否 | 枚举，默认 `768p竖` | `480p竖`、`768p竖`、`1080p竖`、`480p横`、`768p横`、`1080p横` |

## 6. H3多图参考生视频

- 工作流 ID：`minimax_h3_lightx2v_v5`
- 功能：多图参考生成视频。
- 提交接口：`/api/v1/comfyui/comfyui_workflow/minimax_h3_lightx2v_v5`

| 参数 | 必填 | 类型/范围 | 说明 |
|---|---|---|---|
| `prompt` | 是 | 长文本，长度 `1-500000` | 视频生成提示词 |
| `duration` | 否 | 整数，`1-10`，默认 `5` | 视频时长（秒） |
| `resolution` | 否 | 枚举，默认 `768p竖` | `480p竖`、`480p横`、`768p竖`、`768p横`、`1080p竖`、`1080p横`、`480p(1:1)`、`768p(1:1)`、`1080p(1:1)` |
| `ref_image_0` | 是 | URL/base64；`image/jpeg`、`image/png`、`image/webp` | 第一张参考图片 |
| `ref_image_1` - `ref_image_8` | 否 | 同上 | 追加参考图片，最多共 9 张 |
| `seed` | 否 | 数字，`1-999999999999999` | 随机种子 |

## 7. H3文生视频

- 工作流 ID：`minimax_h3_lightx2v_no_pic`
- 功能：只用提示词生成视频。
- 提交接口：`/api/v1/comfyui/comfyui_workflow/minimax_h3_lightx2v_no_pic`

| 参数 | 必填 | 类型/范围 | 说明 |
|---|---|---|---|
| `prompt` | 是 | 长文本，长度 `1-200000` | 描述主体、动作、场景、镜头等 |
| `duration` | 否 | 整数，`1-15`，默认 `5` | 视频时长（秒） |
| `resolution` | 否 | 枚举，默认 `768p竖` | `480p竖`、`480p横`、`768p竖`、`768p横`、`480p(1:1)`、`768p(1:1)` |

## 8. H3首尾帧生成视频

- 工作流 ID：`minimax_h3_lightx2v`
- 功能：使用首帧和尾帧图片生成中间运动视频。
- 提交接口：`/api/v1/comfyui/comfyui_workflow/minimax_h3_lightx2v`

| 参数 | 必填 | 类型/范围 | 说明 |
|---|---|---|---|
| `prompt` | 是 | 长文本，长度 `1-2000000` | 视频生成提示词 |
| `first_frame` | 是 | URL/base64；`image/jpeg`、`image/png`、`image/webp` | 首帧图片 |
| `last_frame` | 是 | URL/base64；`image/jpeg`、`image/png`、`image/webp` | 尾帧图片 |
| `duration` | 否 | 整数，`1-15`，默认 `5` | 视频时长（秒） |
| `resolution` | 否 | 枚举，默认 `768p竖` | `480p竖`、`480p横`、`768p竖`、`768p横` |

## 9. indextts2

- 工作流 ID：`indextts2-v1`
- 功能：基于文本和参考音频生成目标语音；保留说话人音色并支持情绪表达。
- 提交接口：`/api/v1/comfyui/comfyui_workflow/indextts2-v1`

| 参数 | 必填 | 类型/范围 | 说明 |
|---|---|---|---|
| `prompt_text` | 是 | 文本，长度 `1-2048` | 要合成的文本 |
| `prompt_simple` | 是 | URL/base64；`audio/mpeg`、`audio/wav` | 音色参考音频 |
| `emo_control_method` | 是 | 枚举 | 当前在线表单显示 `与音色参考音频相同` |
| `emo_ref_audio` | 否 | URL/base64；`audio/mpeg`、`audio/wav` | 情感参考音频 |
| `emo_random` | 否 | 布尔值，默认 `false` | 是否随机情感 |
| `emo_afraid` | 否 | 数字，`0-1.4`，默认 `0` | 害怕情绪权重 |
| `emo_angry` | 否 | 数字，`0-1.4`，默认 `0` | 生气情绪权重 |
| `emo_calm` | 否 | 数字，`0-1.4`，默认 `0` | 平静情绪权重 |
| `emo_disgusted` | 否 | 数字，`0-1.4`，默认 `0` | 厌恶情绪权重 |
| `emo_happy` | 否 | 数字，`0-1.4`，默认 `0` | 开心情绪权重 |
| `emo_melancholic` | 否 | 数字，`0-1.4`，默认 `0` | 忧郁情绪权重 |
| `emo_sad` | 否 | 数字，`0-1.4`，默认 `0` | 悲伤情绪权重 |
| `emo_surprised` | 否 | 枚举，当前表单值为字符串 `"0"` | 惊讶情绪字段 |

> 注意：indextts2 的 API 示例中 `emo_control_method` 曾显示为“使用情感参考音频”，而当前在线表单显示的唯一选项是“与音色参考音频相同”；两者不一致时以在线表单和实际接口校验为准。
