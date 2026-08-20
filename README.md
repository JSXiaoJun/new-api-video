# new-api-video

Independent video upstream adapter for New API. It exposes a normalized `/v1/videos` API and routes each model to the matching upstream protocol.

## Features

- `videos`, legacy `seedance`, and Volcengine Ark v3 protocol conversion
- Seedance nested status normalization
- Automatic or forwarded `Idempotency-Key`
- Same-origin New API upload presign forwarding for the workbench
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
seedance-2-public   ark-v3    doubao-seedance-2-*  ark-seedance-2       4-15
```

The public model name is returned by `/v1/models` and accepted by `/v1/videos`. The optional upstream model name is
substituted only when forwarding the request. Leaving it empty uses the public model name upstream. Synchronizing
models preserves existing aliases by upstream model name, adds newly discovered models, and removes models that have
disappeared upstream. `WORKBENCH_ORIGIN` controls which browser origin may read the public model capability endpoint.

### Volcengine Ark v3

Use base URL `https://ark.cn-beijing.volces.com`, protocol `ark-v3`, and profile `ark-seedance-2` for the native
`/api/v3/contents/generations/tasks` API. Ark-specific request construction and task parsing live in
`app/ark_video.py`; generic routing, auditing, and media streaming remain in `app/proxy.py`.

The adapter maps canonical prompts and reference URL arrays into Ark `content` items with `reference_image`,
`reference_video`, and `reference_audio` roles. Ark's temporary `content.video_url` is retained internally and served
through the existing public content proxy. `/new-api/v1/upload/presign` forwards workbench presign requests to New API;
the returned `public_url` is suitable as an Ark reference only when the object is publicly readable.

The admin page's model route editor keeps the discovered upstream name in `映射上游模型名`; leaving `对外模型名`
empty uses that upstream name as the public name when saving. Supported durations can be selected per route. `请求协议`
controls the upstream endpoint, while `请求格式` independently selects the saved payload transformation profile. Model
discovery suggests a format only for new rows; editing and later synchronization preserve the saved selection.
Synchronization is non-destructive: models missing from a later discovery response remain in the editor. If an upstream
renames a model, update that route's `映射上游模型名` manually, verify its saved `请求格式`, then save the upstream.

### Pro666 Channel

Add Pro666 as a regular video upstream with `https://api.pro666.top` as its Base URL and the channel API key, then use
`同步上游模型`. The adapter recognizes the current Pro666 video catalog and assigns the correct `/v1/videos` request
profile, duration list, resolution, reference-image count, and audio/video support automatically.

The Pro666 adapter is isolated in `app/channels/pro666.py`. It supports the documented `video-v1`, `sd2-431`,
`sd2.5-480p`, `sd2.5-720p`, `sd2-mini`, Firefly Seedance 2, and `veo-omni` payload families, plus the legacy
`sd2-5-720p` name and the currently advertised `video-v1-face`, `video-900`, and `sd2-5-vref-720p` aliases.
Firefly model names contain `seedance`, but they must keep
the auto-selected `videos` protocol because Pro666 exposes them through `POST /v1/videos` rather than
`POST /v1/video/generations`.

Use `生成对接文档` in the video or image admin page to download a Markdown document generated from the currently
enabled public models and their configured capabilities.

The customer-facing video document uses `API_PUBLIC_BASE_URL` for all authenticated New API requests and the
admin-selected public media domain for downloads. It never includes the middleware admin domain, adapter address, or
the internal New API gateway target. The workbench separately uses `data[].id` from `/v1/model-capabilities` as its complete model list, cache
the response locally, and refresh it only after an explicit user action. This request does not use an API key. Set
`WORKBENCH_ORIGIN` to the exact browser origin that may read the catalog.

The workbench sends authenticated video requests to `/new-api/v1/videos` on this service. The gateway forwards only
the video create, task query, and content routes to `NEW_API_GATEWAY_BASE_URL`, preserving the user's `Authorization`
header so New API remains responsible for authentication, quota, and billing. New API then calls the regular
`/v1/videos` adapter route with `ADAPTER_API_KEY`; keeping the gateway under `/new-api` prevents a proxy loop.

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
Compose exposes port `8787` on all interfaces. To bind only to localhost, change the mapping to `127.0.0.1:8787:8787`.
Set `API_PUBLIC_BASE_URL` to the customer-facing New API address. `PUBLIC_BASE_URL` is the middleware's own
admin/workbench address and must not be published as the customer API endpoint.

To roll back, replace `latest` in `docker-compose.yml` with a previously published `sha-<commit>` tag, then run `docker compose pull` and `docker compose up -d` again.

## API

Model discovery, video, and image-generation requests require:

```http
Authorization: Bearer <ADAPTER_API_KEY>
```

```text
GET  /v1/models
GET  /v1/model-capabilities  (public capability metadata)
POST /new-api/v1/videos  (workbench gateway to New API; user API key)
GET  /new-api/v1/videos/{task_id}
GET  /new-api/v1/videos/{task_id}/content
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

Task polling responses intentionally omit upstream `id`, `task_id`, and raw `video_url` fields. New API sends its
opaque task ID to the adapter in `X-Public-Task-ID` when creating a video. The adapter binds that ID after successful
creation and returns `/public/videos/{public_task_id}/content` on the selected public media domain. This public link is
valid for 24 hours and defaults to 50 download starts per video; the limit can be changed from the admin page
(1-10,000). Video bytes are served directly by the adapter and do not pass through New API.

## Task Audit

Set the initial public New API address in `.env`:

```env
NEW_API_PUBLIC_BASE_URL=https://media.yyapi.cloud
```

After the first startup, the admin page can switch the returned sanitized link domain between
`https://media.yyapi.cloud`, `https://www.yyapi.cloud`, and `https://zl.yyapi.cloud`. Use the dedicated media domain for
new deployments. The selection is stored in `data/adapter.db`; the environment variable is only used as the initial
value when the setting has not been created yet.

The adapter returns `X-Oneapi-Request-Id: vrq_...` on create and poll responses. New API `v1.0.0-rc.22`
records this value as `upstream_request_id` without any New API code changes. Search that value in the adapter's
admin task audit page to inspect the encrypted request history, upstream task ID, original responses, sanitized
responses, and the real video source URL.

Updated New API versions pass the public `task_...` ID automatically. The audit page still supports manual binding for
legacy tasks. For completed tasks, the audit view adds the public media URL to `url`, `video_url`, `result_url`, and
`download_url` in the sanitized response. Audit payloads and source URLs are stored encrypted with `ENCRYPTION_KEY`;
keep that key and `data/adapter.db` backed up together.
