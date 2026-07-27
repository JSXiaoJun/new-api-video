from __future__ import annotations

import time
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import database, proxy
from .config import ROOT_DIR, settings
from .schemas import LoginInput, UpstreamInput
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
app.mount("/static", StaticFiles(directory=ROOT_DIR / "static"), name="static")
templates = Jinja2Templates(directory=ROOT_DIR / "templates")


@app.on_event("startup")
def startup() -> None:
    database.initialize()


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
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


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "time": int(time.time())}


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
        max_age=12 * 60 * 60,
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


@app.get("/admin/api/dashboard")
def dashboard_api(_: tuple[str, dict] = Depends(admin_session)):
    return database.dashboard_data()


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


@app.get("/v1/models", dependencies=[Depends(adapter_auth)])
def models():
    now = int(time.time())
    return {
        "object": "list",
        "data": [{"id": model, "object": "model", "created": now, "owned_by": "video-relay"} for model in database.list_models()],
    }


@app.post("/v1/videos", dependencies=[Depends(adapter_auth)])
async def create_video(request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    if not request.headers.get("content-type", "").lower().startswith("application/json"):
        raise HTTPException(status_code=415, detail="Content-Type must be application/json")
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    return await proxy.create_video(payload, idempotency_key)


@app.get("/v1/videos/{task_id}", dependencies=[Depends(adapter_auth)])
async def fetch_video(task_id: str):
    return await proxy.fetch_task(task_id)


@app.get("/v1/videos/{task_id}/content", dependencies=[Depends(adapter_auth)])
async def video_content(task_id: str, request: Request):
    return await proxy.stream_content(task_id, request)
