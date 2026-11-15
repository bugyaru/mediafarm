from __future__ import annotations

import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from mf_core import db
from mf_core.config import Settings, reset_settings_cache

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(migrated_db, monkeypatch):
    monkeypatch.setenv("MF_DB_DSN", migrated_db)
    monkeypatch.setenv("MF_ROLE", "ui")
    monkeypatch.setenv("MF_UI_SECRET_KEY", "t" * 32)
    monkeypatch.setenv("MF_UI_BOOTSTRAP_PASSWORD", "admin-password-123")
    monkeypatch.setenv("MF_UI_INTERNAL_TOKEN", "internal-test-token")
    reset_settings_cache()
    await db.dispose()
    from ui.app import create_app
    app = create_app()
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    await db.dispose()
    reset_settings_cache()


async def _login(c: AsyncClient, username="admin", password="admin-password-123") -> str:
    r = await c.post("/login", data={"username": username, "password": password},
                     follow_redirects=False)
    assert r.status_code == 303, r.text
    cookie = r.cookies.get("mf_session")
    assert cookie
    from mf_core.config import get_settings
    from ui.security import csrf_token
    return csrf_token(cookie, get_settings())


async def test_login_required_and_rbac(client):
    r = await client.get("/api/v1/jobs")
    assert r.status_code == 401
    token = await _login(client)
    r = await client.get("/api/v1/jobs")
    assert r.status_code == 200
    # CSRF обязателен для мутаций при cookie-аутентификации
    r = await client.post("/api/v1/presets/seed")
    assert r.status_code == 403
    r = await client.post("/api/v1/presets/seed", headers={"X-CSRF-Token": token})
    assert r.status_code == 200 and r.json()["created"] >= 5


async def test_storage_crud_and_job_lifecycle(client, tmp_path):
    token = await _login(client)
    h = {"X-CSRF-Token": token}
    ind, outd = tmp_path / "in", tmp_path / "out"
    ind.mkdir(); outd.mkdir()

    r = await client.post("/api/v1/storages", headers=h, json={
        "name": f"in_{uuid.uuid4().hex[:6]}", "kind": "local", "base_uri": f"file://{ind}"})
    assert r.status_code == 201
    sid = r.json()["id"]
    r = await client.post(f"/api/v1/storages/{sid}/test", headers=h)
    assert r.json()["ok"] is True

    r = await client.get("/api/v1/presets")
    preset_name = r.json()[0]["name"]

    r = await client.post("/api/v1/jobs", headers=h, json={
        "url": "https://example.com/video.mp4", "preset_name": preset_name, "kind": "fetch"})
    assert r.status_code == 201, r.text
    jid = r.json()["id"]
    # идемпотентность: повтор не создаёт дубль
    r2 = await client.post("/api/v1/jobs", headers=h, json={
        "url": "https://example.com/video.mp4", "preset_name": preset_name, "kind": "fetch"})
    assert r2.json()["id"] == jid and r2.json()["created"] is False

    r = await client.get(f"/api/v1/jobs/{jid}")
    assert r.json()["job"]["state"] == "queued"
    r = await client.post(f"/api/v1/jobs/{jid}/cancel", headers=h)
    assert r.json()["state"] == "cancelled"
    r = await client.post(f"/api/v1/jobs/{jid}/retry", headers=h)
    assert r.json()["state"] == "queued"
    r = await client.patch(f"/api/v1/jobs/{jid}", headers=h, json={"priority": 500})
    assert r.json()["priority"] == 500

    r = await client.get("/api/v1/stats")
    assert "queue" in r.json() and "workers" in r.json()
    r = await client.get("/api/v1/audit")
    assert any(a["action"] == "job_create" for a in r.json())


async def test_preset_validation_and_cron_preview(client):
    token = await _login(client)
    h = {"X-CSRF-Token": token}
    r = await client.post("/api/v1/presets/validate", headers=h, json={
        "name": "bad", "steps": [{"id": "publish"}]})
    assert r.json()["ok"] is False
    r = await client.post("/api/v1/schedules/preview", headers=h,
                          json={"cron": "*/15 * * * *", "count": 3})
    assert len(r.json()["next"]) == 3
    r = await client.post("/api/v1/schedules/preview", headers=h, json={"cron": "nope"})
    assert r.status_code == 400


async def test_internal_api_token_guard(client):
    r = await client.get("/internal/v1/storages")
    assert r.status_code == 401
    r = await client.get("/internal/v1/storages",
                         headers={"Authorization": "Bearer internal-test-token"})
    assert r.status_code == 200
    assert all("password" not in str(s).lower() for s in r.json())