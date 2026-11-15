from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mf_core.config import Settings
from mf_core.errors import ConfigInvalidError
from mf_core.models import AppUser
from mf_core.telemetry import get_logger

log = get_logger("mf.ui.auth")
_ph = PasswordHasher()
ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}
MIN_PASSWORD_LEN = 12


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LEN:
        raise ConfigInvalidError(f"пароль короче {MIN_PASSWORD_LEN} символов")
    return _ph.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    if not password_hash:
        return False
    try:
        _ph.verify(password_hash, password)
        return True
    except (VerifyMismatchError, InvalidHashError):
        return False


def _serializer(s: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(s.ui_secret_key, salt="mf-session")


def make_session_cookie(user: AppUser, s: Settings) -> str:
    return _serializer(s).dumps({"uid": str(user.id), "u": user.username, "r": user.role})


def read_session_cookie(value: str | None, s: Settings) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        return _serializer(s).loads(value, max_age=s.ui_session_ttl_s)
    except (BadSignature, SignatureExpired):
        return None


def csrf_token(cookie_value: str | None, s: Settings) -> str:
    if not cookie_value:
        return ""
    return hmac.new(s.ui_secret_key.encode(), cookie_value.encode(), hashlib.sha256).hexdigest()[:32]


def csrf_ok(cookie_value: str | None, provided: str | None, s: Settings) -> bool:
    expected = csrf_token(cookie_value, s)
    return bool(expected) and bool(provided) and hmac.compare_digest(expected, provided)


def role_at_least(user: AppUser | None, minimum: str) -> bool:
    if user is None or not user.is_active:
        return False
    return ROLE_RANK.get(user.role, -1) >= ROLE_RANK[minimum]


async def authenticate(sess: AsyncSession, username: str, password: str) -> AppUser | None:
    user = (await sess.execute(select(AppUser).where(AppUser.username == username))).scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(user.password_hash, password):
        return None
    return user


async def create_user(sess: AsyncSession, username: str, password: str | None, role: str,
                      external_id: str | None = None) -> AppUser:
    if role not in ROLE_RANK:
        raise ConfigInvalidError(f"неизвестная роль {role!r}")
    user = AppUser(username=username, role=role, external_id=external_id,
                   password_hash=hash_password(password) if password else None, is_active=True)
    sess.add(user)
    await sess.flush()
    return user


async def bootstrap_admin(sess: AsyncSession, s: Settings) -> None:
    count = await sess.scalar(select(func.count()).select_from(AppUser))
    if count:
        return
    password = s.ui_bootstrap_password or secrets.token_urlsafe(18)
    user = await create_user(sess, s.ui_bootstrap_user, password, "admin")
    if s.ui_bootstrap_password:
        log.warning("admin_bootstrapped", username=user.username, source="MF_UI_BOOTSTRAP_PASSWORD")
    else:
        log.warning("admin_bootstrapped_generated_password", username=user.username,
                    password=password)
        print(f"\n*** MediaFarm admin: {user.username} / {password} ***\n", flush=True)