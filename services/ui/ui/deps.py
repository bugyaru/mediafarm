from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from mf_core.config import Settings, get_settings
from mf_core.db import get_sessionmaker
from mf_core.models import AppUser
from ui.security import csrf_token, read_session_cookie, role_at_least

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["app_name"] = "MediaFarm"


def settings_dep() -> Settings:
    return get_settings()


async def db_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


def session_payload(request: Request, s: Settings) -> dict[str, Any] | None:
    return read_session_cookie(request.cookies.get(s.ui_session_cookie), s)


async def current_user(request: Request, session: AsyncSession = Depends(db_session),
                       s: Settings = Depends(settings_dep)) -> AppUser | None:
    data = session_payload(request, s)
    if not data:
        return None
    try:
        user = await session.get(AppUser, uuid.UUID(data["uid"]))
    except (ValueError, KeyError):
        return None
    return user if user and user.is_active else None


def require_role(minimum: str):
    async def dep(user: AppUser | None = Depends(current_user)) -> AppUser:
        if user is None:
            raise HTTPException(status_code=401, detail="требуется вход")
        if not role_at_least(user, minimum):
            raise HTTPException(status_code=403, detail=f"нужна роль {minimum}+")
        return user
    return dep


def render(request: Request, name: str, user: AppUser | None = None, **ctx: Any):
    s = get_settings()
    context = {
        "request": request, "user": user,
        "csrf": csrf_token(request.cookies.get(s.ui_session_cookie), s),
        "msg": request.query_params.get("msg", ""),
        "err": request.query_params.get("err", ""),
        **ctx,
    }
    return templates.TemplateResponse(request, name, context)


def parse_form(form, *, json_fields=(), int_fields=(), bool_fields=()) -> dict[str, Any]:
    import json
    data: dict[str, Any] = {}
    for key in form:
        if key == "csrf_token":
            continue
        data[key] = form.get(key)
    for f in json_fields:
        raw = data.get(f)
        if raw in (None, ""):
            data[f] = {} if f not in ("include", "exclude") else []
            continue
        try:
            data[f] = json.loads(raw)
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"{f}: некорректный JSON ({e})") from e
    for f in int_fields:
        if data.get(f) not in (None, ""):
            data[f] = int(data[f])
    for f in bool_fields:
        data[f] = str(data.get(f, "")).lower() in ("1", "true", "on", "yes")
    return data