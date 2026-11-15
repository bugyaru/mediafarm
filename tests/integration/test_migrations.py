from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mf_core.models import Asset, Job, Preset, Storage
from mf_core.util import asset_identity

pytestmark = pytest.mark.integration


async def _seed(dsn: str) -> dict[str, uuid.UUID]:
    engine = create_async_engine(dsn)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    ids: dict[str, uuid.UUID] = {}
    async with sm() as s, s.begin():
        st = Storage(name=f"in_{uuid.uuid4().hex[:6]}", kind="smb", base_uri="smb://h/share")
        s.add(st); await s.flush(); ids["storage"] = st.id
        pr = Preset(name=f"p_{uuid.uuid4().hex[:6]}", version=1,
                    spec={"name": "p", "steps": [{"id": "acquire"}]})
        s.add(pr); await s.flush(); ids["preset"] = pr.id
        a = Asset(identity_key=asset_identity(st.id, "smb://h/share/f.mxf", "1:2"),
                  storage_id=st.id, uri="smb://h/share/f.mxf", fingerprint="1:2")
        s.add(a); await s.flush(); ids["asset"] = a.id
        j = Job(asset_id=a.id, preset_id=pr.id, spec={"steps": []},
                idempotency_key=uuid.uuid4().hex)
        s.add(j); await s.flush(); ids["job"] = j.id
    await engine.dispose()
    return ids


def test_schema_and_triggers(migrated_db: str):
    async def scenario():
        ids = await _seed(migrated_db)
        raw = migrated_db.replace("postgresql+asyncpg://", "postgresql://")

        # 1. LISTEN/NOTIFY fan-out
        listener = await asyncpg.connect(raw)
        got: asyncio.Queue = asyncio.Queue()
        await listener.add_listener("job_events", lambda *a: got.put_nowait(a[-1]))
        try:
            engine = create_async_engine(migrated_db)
            sm = async_sessionmaker(engine, expire_on_commit=False)
            async with sm() as s, s.begin():
                await s.execute(
                    text("UPDATE job SET progress = CAST(:p AS jsonb) WHERE id = :i"),
                    {"p": '{"pct": 42.5, "speed": 3.1}', "i": ids["job"]},
                )
            payload = await asyncio.wait_for(got.get(), timeout=5)
            assert '"pct": 42.5' in payload or '"pct":42.5' in payload

            # 2. updated_at триггер
            async with sm() as s:
                ts = await s.scalar(text("SELECT updated_at FROM job WHERE id=:i"), {"i": ids["job"]})
                created = await s.scalar(text("SELECT created_at FROM job WHERE id=:i"), {"i": ids["job"]})
                assert ts >= created

            # 3. дедупликация ассета
            with pytest.raises(IntegrityError):
                async with sm() as s, s.begin():
                    s.add(Asset(identity_key=asset_identity(ids["storage"], "smb://h/share/f.mxf", "1:2"),
                                storage_id=ids["storage"], uri="smb://h/share/f.mxf", fingerprint="1:2"))

            # 4. CHECK на state
            with pytest.raises(IntegrityError):
                async with sm() as s, s.begin():
                    await s.execute(text("UPDATE job SET state='bogus' WHERE id=:i"), {"i": ids["job"]})

            # 5. каскад job -> job_step
            async with sm() as s, s.begin():
                await s.execute(text(
                    "INSERT INTO job_step (job_id, idx, name) VALUES (:j, 0, 'acquire')"),
                    {"j": ids["job"]})
            async with sm() as s, s.begin():
                await s.execute(text("DELETE FROM job WHERE id=:i"), {"i": ids["job"]})
            async with sm() as s:
                assert await s.scalar(text("SELECT count(*) FROM job_step")) == 0
            await engine.dispose()
        finally:
            await listener.close()

    asyncio.run(scenario())


def test_partial_indexes_exist(migrated_db: str):
    async def scenario():
        engine = create_async_engine(migrated_db)
        async with engine.connect() as c:
            names = set((await c.scalars(text(
                "SELECT indexname FROM pg_indexes WHERE tablename='job'"))).all())
        await engine.dispose()
        assert {"ix_job_queue", "ix_job_lease", "ix_job_concurrency"} <= names
    asyncio.run(scenario())