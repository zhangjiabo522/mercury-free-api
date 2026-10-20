import time
from collections import defaultdict, deque
from typing import Literal

from fastapi import Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from protocol import ProxyError


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    password: str = Field(min_length=1, max_length=512)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    api_key: str | None = Field(default=None, min_length=16, max_length=256, pattern=r"^[!-~]+$")
    reasoning_effort: Literal["instant", "low", "medium", "high"] = "medium"
    web_search: bool = False
    upstream_timeout: int = Field(default=180, ge=10, le=600)


def install_admin(app, root):
    attempts = defaultdict(deque)

    async def csrf(request: Request):
        origin = request.headers.get("origin")
        expected = str(request.base_url).rstrip("/")
        if request.headers.get("x-admin-request") != "1" or origin and origin.rstrip("/") != expected:
            raise ProxyError("控制台请求来源不正确，请刷新页面后重试", 403, "invalid_origin")

    async def authenticated(request: Request):
        if not request.app.state.store.check_session(request.cookies.get("admin_session", "")):
            raise ProxyError("请先登录控制台", 401, "admin_login_required")

    app.mount("/static", StaticFiles(directory=root / "static"), name="static")

    @app.get("/", include_in_schema=False)
    async def home():
        return RedirectResponse("/admin")

    @app.get("/admin", include_in_schema=False)
    async def page():
        return FileResponse(root / "static" / "index.html", headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'"})

    @app.get("/api/admin/session", include_in_schema=False)
    async def session(request: Request):
        return JSONResponse({"authenticated": request.app.state.store.check_session(request.cookies.get("admin_session", ""))}, headers={"Cache-Control": "no-store"})

    @app.post("/api/admin/login", dependencies=[Depends(csrf)], include_in_schema=False)
    async def login(payload: Login, request: Request):
        address = request.client.host if request.client else "local"
        now = time.monotonic()
        if len(attempts) > 1024:
            for key in list(attempts):
                if not attempts[key] or attempts[key][-1] < now - 60:
                    attempts.pop(key, None)
        recent = attempts[address]
        while recent and recent[0] < now - 60:
            recent.popleft()
        if len(recent) >= 10:
            raise ProxyError("登录尝试过多，请一分钟后重试", 429, "login_rate_limit", retry_after="60")
        recent.append(now)
        if not request.app.state.store.check_password(payload.password):
            raise ProxyError("管理密码不正确，请检查 data/admin-password.txt", 401, "invalid_admin_password")
        recent.clear()
        response = JSONResponse({"authenticated": True}, headers={"Cache-Control": "no-store"})
        response.set_cookie("admin_session", request.app.state.store.session(), max_age=43200, httponly=True, secure=request.url.scheme == "https", samesite="strict", path="/api/admin")
        return response

    @app.post("/api/admin/logout", dependencies=[Depends(csrf), Depends(authenticated)], include_in_schema=False)
    async def logout():
        response = JSONResponse({"authenticated": False})
        response.delete_cookie("admin_session", path="/api/admin")
        return response

    @app.get("/api/admin/overview", dependencies=[Depends(authenticated)], include_in_schema=False)
    async def overview(request: Request, date: str | None = None, status: Literal["all", "success", "error", "cancelled"] = "all", offset: int = Query(default=0, ge=0, le=10000000)):
        data = request.app.state.store.overview(date, status, offset)
        data["browser_ready"] = request.app.state.upstream.ready
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    @app.get("/api/admin/settings", dependencies=[Depends(authenticated)], include_in_schema=False)
    async def settings(request: Request):
        store = request.app.state.store
        key = store.get("api_key")
        return JSONResponse({"api_key_preview": key[:7] + "…" + key[-4:], "reasoning_effort": store.get("reasoning_effort"), "web_search": store.get("web_search") == "true", "upstream_timeout": int(store.get("upstream_timeout"))}, headers={"Cache-Control": "no-store"})

    @app.get("/api/admin/api-key", dependencies=[Depends(authenticated)], include_in_schema=False)
    async def api_key(request: Request):
        return JSONResponse({"api_key": request.app.state.store.get("api_key")}, headers={"Cache-Control": "no-store"})

    @app.put("/api/admin/settings", dependencies=[Depends(csrf), Depends(authenticated)], include_in_schema=False)
    async def update_settings(payload: Settings, request: Request):
        store = request.app.state.store
        if payload.api_key is not None:
            store.set_key(payload.api_key)
        store.set("reasoning_effort", payload.reasoning_effort)
        store.set("web_search", "true" if payload.web_search else "false")
        store.set("upstream_timeout", payload.upstream_timeout)
        request.app.state.upstream.timeout = payload.upstream_timeout
        return {"saved": True}
