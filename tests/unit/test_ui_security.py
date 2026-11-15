import uuid

import pytest

from mf_core.config import Settings
from mf_core.errors import ConfigInvalidError
from mf_core.models import AppUser
from ui.security import (
    csrf_ok, csrf_token, hash_password, make_session_cookie, read_session_cookie,
    role_at_least, verify_password,
)


def test_password_hash_and_policy():
    h = hash_password("correct-horse-battery")
    assert verify_password(h, "correct-horse-battery")
    assert not verify_password(h, "wrong")
    assert not verify_password(None, "x")
    with pytest.raises(ConfigInvalidError):
        hash_password("short")


def test_session_cookie_roundtrip_and_expiry():
    s = Settings(ui_secret_key="x" * 32)
    user = AppUser(id=uuid.uuid4(), username="op", role="operator", is_active=True)
    cookie = make_session_cookie(user, s)
    data = read_session_cookie(cookie, s)
    assert data["uid"] == str(user.id) and data["r"] == "operator"
    assert read_session_cookie(cookie + "tamper", s) is None
    assert read_session_cookie(None, s) is None
    short = Settings(ui_secret_key="x" * 32, ui_session_ttl_s=-1)
    assert read_session_cookie(cookie, short) is None


def test_csrf_bound_to_cookie_and_secret():
    s = Settings(ui_secret_key="y" * 32)
    cookie = "abc"
    token = csrf_token(cookie, s)
    assert csrf_ok(cookie, token, s)
    assert not csrf_ok(cookie, "bad", s)
    assert not csrf_ok("other-cookie", token, s)
    assert not csrf_ok(None, token, s)
    other = Settings(ui_secret_key="z" * 32)
    assert not csrf_ok(cookie, token, other)


def test_role_hierarchy():
    admin = AppUser(username="a", role="admin", is_active=True)
    viewer = AppUser(username="v", role="viewer", is_active=True)
    disabled = AppUser(username="d", role="admin", is_active=False)
    assert role_at_least(admin, "operator") and role_at_least(admin, "admin")
    assert not role_at_least(viewer, "operator")
    assert not role_at_least(disabled, "viewer")
    assert not role_at_least(None, "viewer")