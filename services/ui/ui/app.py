from __future__ import annotations

import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.middleware.sessions import SessionMiddleware

from mf_core import db
from mf_core.config import get_settings
from mf_core.db import session_scope
from mf_core.storage import StorageRegistry
from mf_core.telemetry import get_logger, setup_logging
from ui.bus import PgEventBus
from ui.deps import BASE_DIR
from ui.scheduler import ControlPlane
from ui.security import bootstrap_admin, csrf_ok

log = get_logger("mf.ui")
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
CSRF_EXEMPT_PREFIXES = ("/internal/", "/login", "/logout")

@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    s = get_settings()
    app.state.settings = s
    app.state.registry = StorageRegistry()
    async with session_scope() as sess:
        await app.state.registry.refresh(sess)
        await bootstrap_admin(sess, s)
    app.state.bus = PgEventBus(s.asyncpg_dsn)
    await app.state.bus.start()
    app.state.control = ControlPlane(s, app.state.registry)
    await app.state.control.start()
    app.state.oauth = _build_oauth(s)
    log.info("ui_started", base_url=s.ui_base_url, oidc=bool(s.ui_oidc_issuer))
    try:
        yield
    finally:
        await app.state.control.stop()
        await app.state.bus.stop()
        await app.state.registry.close()
        await db.dispose()
        log.info("ui_stopped")

def _build_oauth(s):
    if not (s.ui_oidc_issuer and s.ui_oidc_client_id):
        return None
    from authlib.integrations.starlette_client import OAuth
    oauth = OAuth()
    oauth.register(
        name="oidc", client_id=s.ui_oidc_client_id, client_secret=s.ui_oidc_client_secret,
        server_metadata_url=f"{s.ui_oidc_issuer.rstrip('/')}/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
    return oauth

def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title="MediaFarm", version="0.1.0", lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.add_middleware(SessionMiddleware, secret_key=s.ui_secret_key, same_site="lax",
                       https_only=s.ui_base_url.startswith("https://"))

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        if request.method in UNSAFE and not request.url.path.startswith(CSRF_EXEMPT_PREFIXES):
            cookie = request.cookies.get(s.ui_session_cookie)
            bearer = request.headers.get("authorization", "")
            if cookie and not bearer:
                if not csrf_ok(cookie, request.headers.get("x-csrf-token"), s):
                    return JSONResponse({"detail": "CSRF token missing/invalid"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(HTTPException)
    async def http_exc(request: Request, exc: HTTPException):
        wants_html = "text/html" in request.headers.get("accept", "")
        if exc.status_code == 401 and wants_html:
            return RedirectResponse(f"/login?next_url={request.url.path}", 303)
        if wants_html:
            return RedirectResponse(f"/?err={exc.detail}", 303)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    from ui.auth_routes import router as auth_router
    from ui.routes.api import router as api_router
    from ui.routes.internal import router as internal_router
    from ui.routes.logs import router as logs_router
    from ui.routes.pages import router as pages_router
    from ui.routes.ws import router as ws_router

    app.include_router(auth_router)
    app.include_router(api_router)
    app.include_router(internal_router)
    app.include_router(ws_router)
    app.include_router(logs_router)
    app.include_router(pages_router)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz():
        return "ok"

    @app.get("/readyz")
    async def readyz():
        ok = await db.ping() and app.state.bus.connected.is_set()
        return JSONResponse({"ready": ok, "leader": app.state.control.leader,
                             "ws_clients": app.state.bus.subscribers},
                            status_code=200 if ok else 503)

    @app.get("/metrics")
    async def metrics():
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app

app = create_app()

def cli() -> None:
    import uvicorn
    s = get_settings()
    uvicorn.run("ui.app:app", host=s.ui_host, port=s.ui_port, proxy_headers=True,
                forwarded_allow_ips="*", log_config=None)

if __name__ == "__main__":
    cli()