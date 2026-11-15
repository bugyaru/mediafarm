"""Полный цикл на локальных хранилищах и реальном ffmpeg."""
from __future__ import annotations

import asyncio
import shutil
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from mf_core import db, queue as q
from mf_core.config import Settings
from mf_core.media.presets import BUILTIN_PRESETS, apply_overrides, snapshot, validate_spec
from mf_core.models import Asset, Storage
from mf_core.util import asset_identity
from worker.main import Worker

pytestmark = pytest.mark.integration
requires_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="нет ffmpeg")


async def _make_src(path):
    p = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440",
        "-t", "4", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(path))
    assert await p.wait() == 0


@requires_ffmpeg
async def test_local_transcode_publish(migrated_db, tmp_path):
    ind, outd, scr = tmp_path / "in", tmp_path / "out", tmp_path / "scratch"
    for d in (ind, outd, scr):
        d.mkdir()
    src = ind / "clip.mp4"
    await _make_src(src)

    sm = async_sessionmaker(create_async_engine(migrated_db, poolclass=NullPool),
                            expire_on_commit=False)
    spec = validate_spec(apply_overrides(
        BUILTIN_PRESETS["proxy_1080p_cpu"],
        {"transcode": {"scale": {"height": 180}, "video": {"cq": 30, "preset": "p1"}},
         "publish": {"storage": "out_local", "path": "{yyyy}/{asset.stem}_180p.mp4"},
         "thumbnail": {"at": ["25%"]}},
    ))
    async with sm() as s, s.begin():
        st_in = Storage(name=f"in_{uuid.uuid4().hex[:6]}", kind="local", base_uri=f"file://{ind}")
        st_out = Storage(name="out_local", kind="local", base_uri=f"file://{outd}")
        s.add_all([st_in, st_out])
        await s.flush()
        a = Asset(identity_key=asset_identity(st_in.id, str(src), "1:1"), storage_id=st_in.id,
                  uri=f"file://{ind}/clip.mp4", fingerprint="1:1", size=src.stat().st_size)
        s.add(a)
        await s.flush()
        jid, _ = await q.enqueue(s, asset_id=a.id, spec=snapshot(spec),
                                 params={"input_uri": a.uri})

    w = Worker(Settings(role="worker", db_dsn=migrated_db, worker_id="e2e",
                        cpu_slots=1, scratch=str(scr), metrics_port=0,
                        idle_poll_s=0.05, heartbeat_s=1, progress_throttle_s=0.2))
    task = asyncio.create_task(w.run())
    try:
        async def wait_done():
            while True:
                async with sm() as s:
                    st = await s.scalar(text("SELECT state FROM job WHERE id=:i"), {"i": jid})
                if st in ("succeeded", "failed"):
                    return st
                await asyncio.sleep(0.2)
        assert await asyncio.wait_for(wait_done(), 120) == "succeeded"
    finally:
        w.request_stop()
        await asyncio.wait_for(task, 30)
        await db.dispose()

    produced = list(outd.rglob("*.mp4"))
    assert produced, "выходной файл не опубликован"
    assert list(outd.rglob("*_thumb_00.jpg"))
    async with sm() as s:
        roles = (await s.execute(text("SELECT role FROM artifact WHERE job_id=:j"),
                                 {"j": jid})).scalars().all()
        astate = await s.scalar(text("SELECT state FROM asset WHERE uri LIKE '%clip.mp4'"))
    assert "output" in roles and "thumbnail" in roles and astate == "done"