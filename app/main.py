from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Path as ApiPath, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import database, image_database, image_proxy, new_api_gateway, proxy
from .config import PUBLIC_LINK_BASE_URLS, ROOT_DIR, settings
from .integration_doc import build_integration_document
from .image_integration_doc import build_image_integration_document
from .model_profiles import profile_options, suggest_protocol, suggest_route
from .schemas import (
    ImageUpstreamInput,
    LoginInput,
    ModelDiscoveryInput,
    PublicLinkSettingsInput,
    PublicVideoDownloadSettingsInput,
    PublicTaskInput,
    UpstreamInput,
)
from .security import (
    SESSION_COOKIE,
    create_session,
    csrf_token,
    login_limiter,
    read_session,
    verify_adapter_key,
    verify_admin_credentials,
    verify_csrf,
)


app = FastAPI(title="Video Relay Console", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.workbench_origin],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=[
        "Content-Type",
        "Content-Length",
        "Content-Disposition",
        "Content-Range",
        "Accept-Ranges",
    ],
)
app.mount("/static", StaticFiles(directory=ROOT_DIR / "static"), name="static")
templates = Jinja2Templates(directory=ROOT_DIR / "templates")


@app.on_event("startup")
def startup() -> None:
    database.initialize()
    image_database.initialize()


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; media-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    return response


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def admin_session(session: str | None = Cookie(default=None, alias=SESSION_COOKIE)) -> tuple[str, dict]:
    payload = read_session(session)
    if payload is None or session is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return session, payload


def admin_mutation(
    session_data: tuple[str, dict] = Depends(admin_session),
    csrf: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> dict:
    session, payload = session_data
    if not verify_csrf(session, csrf):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    return payload


def adapter_auth(authorization: str | None = Header(default=None)) -> None:
    if not verify_adapter_key(authorization):
        raise HTTPException(status_code=401, detail="Invalid adapter API key")


def normalize_discovered_models(payload: Any, protocol_override: str | None = None) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        raw_models = payload.get("data", payload.get("models", []))
    else:
        raw_models = payload
    if not isinstance(raw_models, list):
        return []

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_models:
        if isinstance(item, str):
            model_id = item.strip()
        elif isinstance(item, dict):
            model_id = str(item.get("id") or item.get("model") or item.get("name") or "").strip()
        else:
            model_id = ""
        if not model_id or len(model_id) > 160 or model_id in seen:
            continue
        seen.add(model_id)
        protocol = protocol_override or suggest_protocol(model_id)
        result.append({
            "model": "",
            "upstream_model": model_id,
            "protocol": protocol,
            **suggest_route(model_id, protocol),
        })
    return result


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "time": int(time.time())}


@app.get("/v1/model-capabilities")
def model_capabilities() -> JSONResponse:
    return JSONResponse(
        {"data": database.list_model_capabilities()},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/admin", status_code=302)


@app.get("/admin/login", response_class=HTMLResponse)
def login_page(request: Request, session: str | None = Cookie(default=None, alias=SESSION_COOKIE)):
    if read_session(session):
        return RedirectResponse("/admin", status_code=302)
    return templates.TemplateResponse(request=request, name="login.html", context={"version": settings.app_version})


@app.post("/admin/api/login")
def login(payload: LoginInput, request: Request, response: Response):
    ip = client_ip(request)
    if not login_limiter.allow(ip):
        raise HTTPException(status_code=429, detail="Too many login attempts")
    if not verify_admin_credentials(payload.username, payload.password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    login_limiter.clear(ip)
    token = create_session(payload.username)
    result = JSONResponse({"ok": True})
    result.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    return result


@app.post("/admin/api/logout")
def logout(_: dict = Depends(admin_mutation)):
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/admin", response_class=HTMLResponse)
def dashboard(request: Request, session: str | None = Cookie(default=None, alias=SESSION_COOKIE)):
    session_payload = read_session(session)
    if session_payload is None or session is None:
        return RedirectResponse("/admin/login", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "username": session_payload["username"],
            "csrf_token": csrf_token(session),
            "version": settings.app_version,
        },
    )


@app.get("/admin/images", response_class=HTMLResponse)
def image_dashboard(request: Request, session: str | None = Cookie(default=None, alias=SESSION_COOKIE)):
    session_payload = read_session(session)
    if session_payload is None or session is None:
        return RedirectResponse("/admin/login", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="image_dashboard.html",
        context={
            "username": session_payload["username"],
            "csrf_token": csrf_token(session),
            "version": settings.app_version,
        },
    )


@app.get("/admin/api/dashboard")
def dashboard_api(_: tuple[str, dict] = Depends(admin_session)):
    return {
        **database.dashboard_data(),
        "profiles": profile_options(),
        "public_link_base_url": database.get_public_link_base_url(),
        "public_link_base_url_options": PUBLIC_LINK_BASE_URLS,
        "public_video_download_limit": database.get_public_video_download_limit(),
    }


@app.put("/admin/api/settings/public-link")
def update_public_link_settings(payload: PublicLinkSettingsInput, _: dict = Depends(admin_mutation)):
    return {"public_link_base_url": database.set_public_link_base_url(payload.public_base_url)}


@app.put("/admin/api/settings/public-video")
def update_public_video_settings(
    payload: PublicVideoDownloadSettingsInput,
    _: dict = Depends(admin_mutation),
):
    return {"public_video_download_limit": database.set_public_video_download_limit(payload.download_limit)}


@app.get("/admin/api/integration-document")
def integration_document(_: tuple[str, dict] = Depends(admin_session)):
    models = database.list_model_capabilities()
    content = build_integration_document(
        settings.api_public_base_url,
        models,
        database.get_public_video_download_limit(),
        database.get_public_link_base_url(),
    )
    return Response(
        content=content,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="video-api-integration.md"',
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "X-Model-Count": str(len(models)),
        },
    )


@app.post("/new-api/v1/videos")
async def new_api_create_video(request: Request):
    return await new_api_gateway.forward(request, "/v1/videos")


@app.post("/new-api/v1/upload/presign")
async def new_api_upload_presign(request: Request):
    return await new_api_gateway.forward(request, "/v1/upload/presign")


@app.get("/new-api/v1/videos/{task_id}")
async def new_api_fetch_video(
    request: Request,
    task_id: str = ApiPath(max_length=191, pattern=r"^[A-Za-z0-9_-]+$"),
):
    return await new_api_gateway.forward(request, f"/v1/videos/{task_id}")


@app.get("/new-api/v1/videos/{task_id}/content")
async def new_api_video_content(
    request: Request,
    task_id: str = ApiPath(max_length=191, pattern=r"^[A-Za-z0-9_-]+$"),
):
    return await new_api_gateway.forward(request, f"/v1/videos/{task_id}/content", stream=True)


@app.get("/admin/api/image-integration-document")
def image_integration_document(_: tuple[str, dict] = Depends(admin_session)):
    content = build_image_integration_document(
        settings.api_public_base_url,
        image_database.dashboard_data()["upstreams"],
        database.get_public_link_base_url(),
    )
    return Response(
        content=content,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="image-api-integration.md"',
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app.get("/admin/api/tasks")
def audit_tasks(
    q: str = Query(default="", max_length=191),
    status: str = Query(default="", pattern="^(|queued|processing|completed|failed)$"),
    _: tuple[str, dict] = Depends(admin_session),
):
    return {"tasks": database.list_audit_requests(q.strip(), status)}


@app.get("/admin/api/tasks/{relay_request_id}")
def audit_task(relay_request_id: str, _: tuple[str, dict] = Depends(admin_session)):
    task = database.get_audit_request(relay_request_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.put("/admin/api/tasks/{relay_request_id}/public-task")
def update_public_task(
    relay_request_id: str,
    payload: PublicTaskInput,
    _: dict = Depends(admin_mutation),
):
    try:
        updated = database.set_public_task_id(relay_request_id, payload.public_task_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="Public task ID is already assigned") from exc
    if not updated:
        raise HTTPException(status_code=404, detail="Task not found")
    task = database.get_audit_request(relay_request_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.get("/admin/api/tasks/{relay_request_id}/content")
async def audit_task_content(
    relay_request_id: str,
    request: Request,
    _: tuple[str, dict] = Depends(admin_session),
):
    task = database.get_task_by_relay_request_id(relay_request_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return await proxy.stream_content(task["task_id"], request)


@app.post("/admin/api/upstreams/models")
async def discover_upstream_models(payload: ModelDiscoveryInput, _: dict = Depends(admin_mutation)):
    api_key = payload.api_key.strip()
    if payload.upstream_id and not api_key:
        existing = database.get_upstream(payload.upstream_id, include_key=True)
        if existing is None:
            raise HTTPException(status_code=404, detail="Upstream not found")
        api_key = existing["api_key"]

    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    discovery_protocol = None
    try:
        async with httpx.AsyncClient(timeout=min(settings.upstream_timeout_seconds, 30), follow_redirects=True) as client:
            response = await client.get(f"{payload.base_url}/v1/models", headers=headers)
            if response.status_code == 404:
                response = await client.get(f"{payload.base_url}/api/v3/models", headers=headers)
                discovery_protocol = "ark-v3"
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"上游模型请求失败: {exc}") from exc
    if not 200 <= response.status_code < 300:
        raise HTTPException(status_code=502, detail=f"上游模型接口返回 HTTP {response.status_code}")
    try:
        upstream_payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="上游模型接口返回的不是有效 JSON") from exc
    models = normalize_discovered_models(upstream_payload, discovery_protocol)
    if not models:
        raise HTTPException(status_code=422, detail="上游没有返回可用模型")
    return {"models": models}


@app.post("/admin/api/upstreams")
def create_upstream(payload: UpstreamInput, _: dict = Depends(admin_mutation)):
    if not payload.api_key:
        raise HTTPException(status_code=422, detail="api_key is required")
    return database.save_upstream(payload.model_dump())


@app.put("/admin/api/upstreams/{upstream_id}")
def update_upstream(upstream_id: int, payload: UpstreamInput, _: dict = Depends(admin_mutation)):
    try:
        return database.save_upstream(payload.model_dump(), upstream_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Upstream not found") from exc


@app.delete("/admin/api/upstreams/{upstream_id}")
def remove_upstream(upstream_id: int, _: dict = Depends(admin_mutation)):
    try:
        database.delete_upstream(upstream_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Upstream not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="Disable this upstream because it has tracked tasks") from exc
    return {"ok": True}


@app.get("/admin/api/images/dashboard")
def image_dashboard_api(_: tuple[str, dict] = Depends(admin_session)):
    return image_database.dashboard_data()


@app.get("/admin/api/images/requests")
def image_requests(
    q: str = Query(default="", max_length=191),
    outcome: str = Query(default="", pattern="^(|success|failed)$"),
    _: tuple[str, dict] = Depends(admin_session),
):
    return {"requests": image_database.list_requests(q.strip(), outcome)}


@app.post("/admin/api/images/upstreams/models")
async def discover_image_upstream_models(payload: ModelDiscoveryInput, _: dict = Depends(admin_mutation)):
    api_key = payload.api_key.strip()
    if payload.upstream_id and not api_key:
        existing = image_database.get_upstream(payload.upstream_id, include_key=True)
        if existing is None:
            raise HTTPException(status_code=404, detail="Image upstream not found")
        api_key = existing["api_key"]
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=min(settings.upstream_timeout_seconds, 30), follow_redirects=True) as client:
            response = await client.get(
                image_proxy.upstream_api_url(payload.base_url, "/v1/models"), headers=headers
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"上游模型请求失败: {exc}") from exc
    if not 200 <= response.status_code < 300:
        raise HTTPException(status_code=502, detail=f"上游模型接口返回 HTTP {response.status_code}")
    try:
        upstream_payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="上游模型接口返回的不是有效 JSON") from exc
    models = normalize_discovered_models(upstream_payload)
    if not models:
        raise HTTPException(status_code=422, detail="上游没有返回可用模型")
    return {"models": [item["upstream_model"] for item in models]}


@app.post("/admin/api/images/upstreams")
def create_image_upstream(payload: ImageUpstreamInput, _: dict = Depends(admin_mutation)):
    if not payload.api_key:
        raise HTTPException(status_code=422, detail="api_key is required")
    return image_database.save_upstream(payload.model_dump())


@app.put("/admin/api/images/upstreams/{upstream_id}")
def update_image_upstream(
    upstream_id: int, payload: ImageUpstreamInput, _: dict = Depends(admin_mutation)
):
    try:
        return image_database.save_upstream(payload.model_dump(), upstream_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Image upstream not found") from exc


@app.delete("/admin/api/images/upstreams/{upstream_id}")
def remove_image_upstream(upstream_id: int, _: dict = Depends(admin_mutation)):
    try:
        image_database.delete_upstream(upstream_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Image upstream not found") from exc
    return {"ok": True}


@app.get("/v1/models", dependencies=[Depends(adapter_auth)])
def models():
    now = int(time.time())
    models = sorted(set(database.list_models()) | set(image_database.list_models()))
    return {
        "object": "list",
        "data": [{"id": model, "object": "model", "created": now, "owned_by": "relay"} for model in models],
    }


@app.get("/v1/images/assets/{asset_id}", include_in_schema=False)
async def image_asset(asset_id: str, request: Request):
    return await image_proxy.stream_image_asset(asset_id, request)


@app.get("/public/images/assets/{asset_id}", include_in_schema=False)
async def public_image_asset(asset_id: str, request: Request):
    return await image_proxy.stream_image_asset(asset_id, request)


def _starts_public_video_download(request: Request) -> bool:
    range_header = request.headers.get("range", "").strip().lower()
    return not range_header or range_header.startswith("bytes=0-")


@app.get("/public/videos/{public_task_id}/content", include_in_schema=False)
async def public_video_content(public_task_id: str, request: Request):
    task = database.get_task_by_public_task_id(public_task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Public video not found")

    counted = _starts_public_video_download(request)
    if counted:
        reservation = database.reserve_public_video_download(public_task_id)
        if reservation == "expired":
            raise HTTPException(status_code=410, detail="Public video link has expired")
        if reservation == "limit_reached":
            raise HTTPException(status_code=429, detail="Public video download limit reached")
        if reservation != "reserved":
            raise HTTPException(status_code=404, detail="Public video not found")
    try:
        response = await proxy.stream_content(task["task_id"], request)
        response.headers["Cache-Control"] = "private, no-store"
        return response
    except Exception:
        if counted:
            database.release_public_video_download(public_task_id)
        raise


@app.post("/v1/images/generations", dependencies=[Depends(adapter_auth)])
async def create_image(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    if not request.headers.get("content-type", "").lower().startswith("application/json"):
        raise HTTPException(status_code=415, detail="Content-Type must be application/json")
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    return await image_proxy.forward_json(payload, "generation", idempotency_key)


@app.post("/v1/images/edits", dependencies=[Depends(adapter_auth)])
async def edit_image(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        raise HTTPException(status_code=415, detail="Content-Type must be multipart/form-data")
    return await image_proxy.forward_edit(request, idempotency_key)


@app.post("/v1/videos", dependencies=[Depends(adapter_auth)])
async def create_video(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    public_task_id: str | None = Header(
        default=None,
        alias="X-Public-Task-ID",
        max_length=191,
        pattern=r"^task_[A-Za-z0-9_-]+$",
    ),
):
    if not request.headers.get("content-type", "").lower().startswith("application/json"):
        raise HTTPException(status_code=415, detail="Content-Type must be application/json")
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    return await proxy.create_video(payload, idempotency_key, public_task_id)


@app.get("/v1/videos/{task_id}", dependencies=[Depends(adapter_auth)])
async def fetch_video(task_id: str):
    return await proxy.fetch_task(task_id)


@app.get("/v1/videos/{task_id}/content", dependencies=[Depends(adapter_auth)])
async def video_content(task_id: str, request: Request):
    return await proxy.stream_content(task_id, request)
