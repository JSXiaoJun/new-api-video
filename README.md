# Video Relay Console

独立的视频上游协议转接器。它向 New API 提供统一的 `/v1/videos` 接口，并将不同模型转发到 PIDOI 对应协议。

## 功能

- `videos` 与 `seedance` 两种上游协议转换
- Seedance 嵌套状态响应归一化
- `Idempotency-Key` 自动生成或透传
- 多上游、模型路由、优先级和启停管理
- SQLite 任务归属记录，确保轮询回到原上游
- 上游 API Key 使用 Fernet 加密保存
- 管理员登录、签名会话、CSRF 防护和登录限速
- 视频内容代理及 Range 请求透传

## 本机运行

编辑 `.env` 后执行：

```powershell
.\run.ps1
```

管理页面：`http://127.0.0.1:8787/admin`

## New API 渠道配置

```text
类型：Sora
Base URL：http://宿主机地址:8787
密钥：.env 中的 ADAPTER_API_KEY
```

在管理页面添加 PIDOI 上游，Base URL 填写 `https://pidoi.com`。模型路由示例：

```text
sora2 | videos
gemini-omni-flash | videos
veo31-fast | videos
sora-vip3-pro-720p | videos
seedance-2.0-fast | seedance
```

## Docker

确认 `.env` 中 `DATA_DIR=./data`，然后执行：

```bash
docker compose up -d --build
```

Compose 默认监听所有网卡的 `8787` 端口。需要限制为本机时，可将端口映射改为 `127.0.0.1:8787:8787`。New API 位于其他主机或容器时，需要按实际网络修改端口绑定和 `PUBLIC_BASE_URL`。

## API

所有 `/v1/*` 请求使用：

```http
Authorization: Bearer <ADAPTER_API_KEY>
```

接口：

```text
GET  /v1/models
POST /v1/videos
GET  /v1/videos/{task_id}
GET  /v1/videos/{task_id}/content
GET  /healthz
```
