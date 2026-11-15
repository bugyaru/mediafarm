"""WS-мост: UPDATE job в БД → pg_notify → готовый HTML-фрагмент строки."""
from __future__ import annotations

import uuid

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient

from mf_core import queue as q
from mf_core.config import Settings, reset_settings_cache

pytestmark = pytest.mark.integration


def test_ws_streams_job_row(migrated_db, monkeypatch):
    monkeypatch.setenv("MF_DB_DSN", migrated_db)
    monkeypatch.setenv("MF_UI_SECRET_KEY", "w" * 32)
    monkeypatch.setenv("MF_UI_BOOTSTRAP_PASSWORD", "admin-password-123")
    reset_settings_cache()

    import asyncio

    from mf_core import db

    async def seed():
        sm = async_sessionmaker(create_async_engine(migrated_db, poolclass=NullPool),
                                expire_on_commit=False)
        async with sm() as s, s.begin():
            jid, _ = await q.enqueue(s, spec={"name": "t", "steps": [{"id": "acquire"}]})
        await db.dispose()
        return jid

    jid = asyncio.run(seed())
    from ui.app import create_app

    with TestClient(create_app()) as client:
        r = client.post("/login", data={"username": "admin", "password": "admin-password-123"},
                        follow_redirects=False)
        assert r.status_code == 303
        with client.websocket_connect("/ws/jobs") as ws:
            sync_dsn = Settings(db_dsn=migrated_db).sync_dsn
            with psycopg.connect(sync_dsn) as conn, conn.cursor() as cur:
                cur.execute("UPDATE job SET progress = %s::jsonb WHERE id = %s",
                            ('{"pct": 42.5, "step": "transcode"}', str(jid)))
                conn.commit()
            html = ws.receive_text()
            assert f'id="job-{jid}"' in html
            assert "hx-swap-oob" in html and "42.5" in html
    reset_settings_cache()