# new-api-video

Independent video upstream adapter for New API. It exposes a normalized `/v1/videos` API and routes each model to the matching upstream protocol.

## Features

- `videos` and `seedance` protocol conversion
- Seedance nested status normalization
- Automatic or forwarded `Idempotency-Key`
- Multiple upstreams, model routing, priority, and enable/disable controls
- SQLite task ownership so polling returns to the original upstream
- Fernet-encrypted upstream API keys
- Admin login, signed sessions, CSRF protection, and login rate limiting
- Video content proxy with Range forwarding

## Run Locally

Edit `.env`, then run:

```powershell
.\run.ps1
```

Open `http://127.0.0.1:8787/admin`.

## New API Channel

```text
Type: Sora
Base URL: http://adapter-host:8787
Key: ADAPTER_API_KEY from .env
```

Add the PIDOI upstream in the admin page. Its base URL is `https://pidoi.com`. Model routes use one route per line:

```text
sora2 | videos
gemini-omni-flash | videos
veo31-fast | videos
sora-v3-933-pro | videos
seedance-2.0-fast | seedance
```

## Docker

Set `DATA_DIR=./data` in `.env`, then run:

```bash
docker compose up -d --build
```

Compose exposes port `8787` on all interfaces. To bind only to localhost, change the mapping to `127.0.0.1:8787:8787`. Set `PUBLIC_BASE_URL` and the port mapping according to the network where New API runs.

## API

All `/v1/*` requests require:

```http
Authorization: Bearer <ADAPTER_API_KEY>
```

```text
GET  /v1/models
POST /v1/videos
GET  /v1/videos/{task_id}
GET  /v1/videos/{task_id}/content
GET  /healthz
```
