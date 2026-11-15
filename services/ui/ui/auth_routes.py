from __future__ import annotations

import datetime as dt
import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mf_core.config import Settings
from mf_core.models import AppUser
from mf_core.telemetry import get_logger
from ui.deps import current_user, db_session, render, settings_dep
from ui.security import (
    authenticate, create_user, make_session_cookie, read_session_cookie,
)
from ui.services import audit

log = get_logger("mf.ui.auth")
router = APIRouter()


def _login_response(user: AppUser, s: Settings, next_url: str = "/") -> RedirectResponse:
    resp = RedirectResponse(next_url or "/", status_code=303)
    resp.set_cookie(s.ui_session_cookie, make_session_cookie(user, s),
                    max_age=s.ui_session_ttl_s, httponly=True, samesite="lax",
                    secure=s.ui_base_url.startswith("https://"))
    return resp


@router.get("/login")
async def login_page(request: Request, user=Depends(current_user)):
    if user:
        return RedirectResponse("/", 303)
    return render(request, "login.html", oidc=bool(get_settings_safe(request).ui_oidc_issuer))


def get_settings_safe(request: Request) -> Settings:
    return request.app.state.settings


@router.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...),
                next_url: str = Form("/"), sess: AsyncSession = Depends(db_session),
                s: Settings = Depends(settings_dep)):
    user = await authenticate(sess, username, password)
    if user is None:
        await audit(sess, username, "login_failed", ip=request.client.host if request.client else None)
        await sess.commit()
        return render(request, "login.html", err="Неверный логин или пароль",
                      oidc=bool(s.ui_oidc_issuer))
    user.last_login_at = dt.datetime.now(dt.UTC)
    await audit(sess, user.username, "login", "user", str(user.id),
                ip=request.client.host if request.client else None)
    await sess.commit()
    return _login_response(user, s, next_url)


@router.post("/logout")
async def logout(request: Request, s: Settings = Depends(settings_dep)):
    resp = RedirectResponse("/login", 303)
    resp.delete_cookie(s.ui_session_cookie)
    return resp


@router.get("/auth/oidc")
async def oidc_start(request: Request):
    oauth = request.app.state.oauth
    if oauth is None:
        raise HTTPException(404, "OIDC не настроен")
    s = request.app.state.settings
    nonce = secrets.token_urlsafe(16)
    request.session["oidc_nonce"] = nonce
    return await oauth.oidc.authorize_redirect(request, f"{s.ui_base_url}/auth/oidc/callback",
                                               nonce=nonce)


@router.get("/auth/oidc/callback")
async def oidc_callback(request: Request, sess: AsyncSession = Depends(db_session)):
    oauth = request.app.state.oauth
    s = request.app.state.settings
    if oauth is None:
        raise HTTPException(404, "OIDC не настроен")
    token = await oauth.oidc.authorize_access_token(request)
    claims = token.get("userinfo") or {}
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(400, "OIDC: нет sub в userinfo")
    user = (await sess.execute(select(AppUser).where(AppUser.external_id == sub))).scalar_one_or_none()
    if user is None:
        username = claims.get("preferred_username") or claims.get("email") or f"oidc-{sub[:8]}"
        user = await create_user(sess, username, None, "viewer", external_id=sub)
        log.info("oidc_user_created", username=username)
    user.last_login_at = dt.datetime.now(dt.UTC)
    await audit(sess, user.username, "login_oidc", "user", str(user.id))
    await sess.commit()
    return _login_response(user, s, "/")