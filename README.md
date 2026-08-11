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

Add the PIDOI upstream in the admin page. Its base URL is `https://pidoi.com`. Model routes are edited as table rows:

```text
Public model        Request   Upstream model       Workbench profile   Duration
sora2               videos    (empty)              sora2               (default)
video-pro-10s       videos    manxue-900-10s       manxue-933          10
seedance-public     seedance  seedance-2.0-fast    default              (default)
```

The public model name is returned by `/v1/models` and accepted by `/v1/videos`. The optional upstream model name is
substituted only when forwarding the request. Leaving it empty uses the public model name upstream. Synchronizing
models preserves existing aliases by upstream model name, adds newly discovered models, and removes models that have
disappeared upstream. `WORKBENCH_ORIGIN` controls which browser origin may read the public model capability endpoint.

The admin page's model route editor keeps the discovered upstream name in `映射上游模型名`; leaving `对外模型名`
empty uses that upstream name as the public name when saving. Supported fixed durations are `4s`, `5s`, `8s`, `10s`,
`12s`, and `15s`, and multiple values can be selected for one model. `工作台类型` remains a single selection because
it determines the upstream request format.

Use `生成对接文档` in the video or image admin page to download a Markdown document generated from the currently
enabled public models and their configured capabilities.

## Docker

Pushes to `main` automatically build the `linux/amd64` image and publish both `latest` and `sha-<commit>` tags to:

```text
ghcr.io/jsxiaojun/new-api-video
```

Set `DATA_DIR=./data` in `.env`, then deploy:

```bash
docker compose pull
docker compose up -d
```

For later updates, back up the database first and run the same two commands:

```bash
cp data/adapter.db "data/adapter.db.$(date +%Y%m%d-%H%M%S).bak"
docker compose pull
docker compose up -d
```

The `./data:/app/data` volume keeps the database outside the image, so replacing the container does not remove it.
Compose exposes port `8787` on all interfaces. To bind only to localhost, change the mapping to `127.0.0.1:8787:8787`. Set `PUBLIC_BASE_URL` and the port mapping according to the network where New API runs.

To roll back, replace `latest` in `docker-compose.yml` with a previously published `sha-<commit>` tag, then run `docker compose pull` and `docker compose up -d` again.

## API

Model discovery, video, and image-generation requests require:

```http
Authorization: Bearer <ADAPTER_API_KEY>
```

```text
GET  /v1/models
GET  /v1/model-capabilities  (public capability metadata)
POST /v1/videos
GET  /v1/videos/{task_id}
GET  /v1/videos/{task_id}/content
POST /v1/images/generations
POST /v1/images/edits
GET  /public/images/assets/{asset_id}  (public image asset, no adapter key)
GET  /healthz
```

Image generation and editing responses replace upstream image URLs with
`/public/images/assets/{asset_id}` links. These links do not require the adapter API key and are retained for 7
days; `b64_json` response data is returned unchanged.

Task polling responses intentionally omit upstream `id`, `task_id`, and `video_url` fields. Clients must keep the
public task ID returned by New API when the task is created and download through
`/public/videos/{public_task_id}/content` on the New API domain. This public link is valid for 24 hours and allows up to
The default is 50 download starts per video; this limit can be changed from the admin page (1-10,000). New API must
proxy this path to the adapter's unauthenticated public-video route.

## Task Audit

Set the initial public New API address in `.env`:

```env
NEW_API_PUBLIC_BASE_URL=https://zl.yyapi.cloud
```

After the first startup, the admin page can switch the returned sanitized link domain between
`https://www.yyapi.cloud` and `https://zl.yyapi.cloud`. The selection is stored in `data/adapter.db`; the environment
variable is only used as the initial value when the setting has not been created yet.

The adapter returns `X-Oneapi-Request-Id: vrq_...` on create and poll responses. New API `v1.0.0-rc.22`
records this value as `upstream_request_id` without any New API code changes. Search that value in the adapter's
admin task audit page to inspect the encrypted request history, upstream task ID, original responses, sanitized
responses, and the real video source URL.

New API's public `task_...` ID is generated outside the adapter, so paste it into the matching audit detail when a
public `https://zl.yyapi.cloud/public/videos/{task_id}/content` link is needed. For completed tasks, the audit view
adds that public URL to `url`, `video_url`, `result_url`, and `download_url` in the sanitized response. Audit payloads
and source URLs are stored encrypted with `ENCRYPTION_KEY`; keep that key and `data/adapter.db` backed up together.
