# new-api-video

Independent video upstream adapter for New API. It exposes a normalized `/v1/videos` API and routes each model to the matching upstream protocol.

## Features

- `videos` and `seedance` protocol conversion
- Seedance nested status normalization
- Automatic or forwarded `Idempotency-Key`
- Multiple upstreams, model routing, priority, and enable/disable controls
- SQLite task ownership so polling returns to the original upstream
- Upstream task IDs, media URLs, and error details stay internal to the adapter
- Request correlation with New API logs through `upstream_request_id`
- Encrypted task audit history with original and sanitized upstream responses
- Admin-only video preview, source URL inspection, and public task link mapping
- One-click copying for request, original response, and sanitized response payloads
- Fernet-encrypted upstream API keys
- Admin login, signed sessions, CSRF protection, and login rate limiting
- Configurable persistent admin sessions (`SESSION_TTL_DAYS`, default 30 days)
- Video content proxy with Range forwarding

## Run Locally

Edit `.env`, then run:

```powershell
.\run.ps1
```

Open `http://127.0.0.1:8787/admin`.

Admin sessions remain valid for 30 days by default. Set `SESSION_TTL_DAYS` to an integer from `1` to `365` to
change the signed session and persistent browser cookie lifetime. Browsers configured to clear cookies on exit
will still require a new login after closing the browser.

## New API Channel

```text
Type: Sora
Base URL: http://adapter-host:8787
Key: ADAPTER_API_KEY from .env
```

Add the PIDOI upstream in the admin page. Its base URL is `https://pidoi.com`. Model routes use one route per line:

```text
sora2 | videos | sora2
gemini-omni-flash | videos | gemini-omni
veo31-fast | videos | veo31-fast
manxue-900-10s | videos | manxue-933 | 10
seedance-2.0-fast | seedance | default
```

The columns are `model | protocol | capability profile | optional fixed duration`. The model discovery picker keeps
existing routes selected, lets you add new models and profiles, and removes disappeared models when you apply the
selection. `WORKBENCH_ORIGIN` controls which browser origin may read the public model capability endpoint.

## Docker

Set `DATA_DIR=./data` in `.env`, then run:

```bash
docker compose up -d --build
```

Compose exposes port `8787` on all interfaces. To bind only to localhost, change the mapping to `127.0.0.1:8787:8787`. Set `PUBLIC_BASE_URL` and the port mapping according to the network where New API runs.

## API

Video creation, polling, content, and `/v1/models` require:

```http
Authorization: Bearer <ADAPTER_API_KEY>
```

```text
GET  /v1/models
GET  /v1/model-capabilities  (public capability metadata)
POST /v1/videos
GET  /v1/videos/{task_id}
GET  /v1/videos/{task_id}/content
GET  /healthz
```

Task polling responses intentionally omit upstream `id`, `task_id`, and `video_url` fields. Clients must keep the
public task ID returned by New API when the task is created and download through
`/v1/videos/{public_task_id}/content` on the New API domain.

## Task Audit

Set the public New API address in `.env`:

```env
NEW_API_PUBLIC_BASE_URL=https://zl.yyapi.cloud
```

The adapter returns `X-Oneapi-Request-Id: vrq_...` on create and poll responses. New API `v1.0.0-rc.22`
records this value as `upstream_request_id` without any New API code changes. Search that value in the adapter's
admin task audit page to inspect the encrypted request history, upstream task ID, original responses, sanitized
responses, and the real video source URL.

New API's public `task_...` ID is generated outside the adapter, so paste it into the matching audit detail when a
public `https://zl.yyapi.cloud/v1/videos/{task_id}/content` link is needed. For completed tasks, the audit view adds
that public URL to `url`, `video_url`, `result_url`, and `download_url` in the sanitized response. Audit payloads and
source URLs are stored encrypted with `ENCRYPTION_KEY`; keep that key and `data/adapter.db` backed up together.
