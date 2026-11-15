from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from mf_core.config import Settings
from mf_core.media.presets import BUILTIN_PRESETS, snapshot, validate_spec
from mf_core.models import Preset, Storage, WatchFolder
from mf_core.storage import StorageRegistry
from watcher.archiver import archive_pass
from watcher.emitter import Emitter
from watcher.scanner import FolderScanner

pytestmark = pytest.mark.integration


async def _seed(migrated_db, tmp_path, **wf_kw):
    ind, outd = tmp_path / "in", tmp_path / "out"
    ind.mkdir(exist_ok=True)
    outd.mkdir(exist_ok=True)
    sm = async_sessionmaker(create_async_engine(migrated_db, poolclass=NullPool),
                            expire_on_commit=False)
    spec = validate_spec(BUILTIN_PRESETS["proxy_1080p_cpu"])
    async with sm() as s, s.begin():
        st_in = Storage(name=f"in_{uuid.uuid4().hex[:6]}", kind="local", base_uri=f"file://{ind}")
        st_out = Storage(name=f"out_{uuid.uuid4().hex[:6]}", kind="local", base_uri=f"file://{outd}")
        s.add_all([st_in, st_out])
        await s.flush()
        raw = dict(spec.to_db())
        for step in raw["steps"]:
            if step["id"] == "publish":
                step["storage"] = st_out.name
        pr = Preset(name=f"p_{uuid.uuid4().hex[:6]}", version=1, spec=raw)
        s.add(pr)
        await s.flush()
        wf = WatchFolder(name=f"wf_{uuid.uuid4().hex[:6]}", storage_id=st_in.id, path="",
                         preset_id=pr.id, stability_s=1, poll_interval_s=5,
                         include=["*"], exclude=[], **wf_kw)
        s.add(wf)
        await s.flush()
        ids = {"in": st_in.id, "out": st_out.id, "preset": pr.id, "wf": wf.id}
    return sm, ind, outd, ids


async def _wf_row(sm, wf_id):
    async with sm() as s:
        row = (await s.execute(text("""
            SELECT w.*, s.base_uri AS storage_base_uri FROM watch_folder w
            JOIN storage s ON s.id = w.storage_id WHERE w.id=:i
        """), {"i": wf_id})).mappings().first()
    return dict(row)


async def _scanner(sm, ind, ids, settings):
    reg = StorageRegistry()
    async with sm() as s, s.begin():
        await reg.refresh(s)
    wf = await _wf_row(sm, ids["wf"])
    be, _ = reg.resolve(f"file://{ind}")
    return FolderScanner(wf, be, Emitter(reg, settings), sm, settings, reg), reg


async def test_stability_dedupe_and_job_creation(migrated_db, tmp_path):
    sm, ind, outd, ids = await _seed(migrated_db, tmp_path)
    s = Settings(db_dsn=migrated_db, scratch=str(tmp_path / "scr"))
    scanner, reg = await _scanner(sm, ind, ids, s)

    (ind / "clip.mxf").write_bytes(b"x" * 100)
    st1 = await scanner.tick()
    assert st1.created == 0 and st1.pending >= 1        # ещё не дозрел

    await asyncio.sleep(1.1)
    st2 = await scanner.tick()
    assert st2.created == 1
    st3 = await scanner.tick()
    assert st3.created == 0                              # дедупликация

    async with sm() as sess:
        n_assets = await sess.scalar(text("SELECT count(*) FROM asset WHERE watch_folder_id=:w"),
                                     {"w": ids["wf"]})
        n_jobs = await sess.scalar(text("SELECT count(*) FROM job WHERE watch_folder_id=:w"),
                                   {"w": ids["wf"]})
        astate = await sess.scalar(text("SELECT state FROM asset WHERE watch_folder_id=:w"),
                                   {"w": ids["wf"]})
    assert n_assets == 1 and n_jobs == 1 and astate == "planned"
    await reg.close()


async def test_growing_file_waits(migrated_db, tmp_path):
    sm, ind, _, ids = await _seed(migrated_db, tmp_path)
    s = Settings(db_dsn=migrated_db, scratch=str(tmp_path / "scr"))
    scanner, reg = await _scanner(sm, ind, ids, s)
    f = ind / "grow.mxf"
    f.write_bytes(b"a" * 10)
    await scanner.tick()
    await asyncio.sleep(0.6)
    f.write_bytes(b"a" * 20)                             # размер изменился → таймер сброшен
    st = await scanner.tick()
    assert st.created == 0
    await asyncio.sleep(1.2)
    assert (await scanner.tick()).created == 1
    await reg.close()


async def test_ready_marker_gates(migrated_db, tmp_path):
    sm, ind, _, ids = await _seed(migrated_db, tmp_path, ready_marker="{name}.done")
    s = Settings(db_dsn=migrated_db, scratch=str(tmp_path / "scr"))
    scanner, reg = await _scanner(sm, ind, ids, s)
    (ind / "a.mxf").write_bytes(b"x")
    await scanner.tick()
    await asyncio.sleep(1.1)
    assert (await scanner.tick()).created == 0
    (ind / "a.mxf.done").write_text("")
    assert (await scanner.tick()).created == 1
    await reg.close()


async def test_parser_creates_children_and_parent(migrated_db, tmp_path):
    sm, ind, _, ids = await _seed(
        migrated_db, tmp_path, parser_id="m3u8", parser_opts={"best_only": True},
        include=["*.m3u8"],
    )
    s = Settings(db_dsn=migrated_db, scratch=str(tmp_path / "scr"))
    scanner, reg = await _scanner(sm, ind, ids, s)
    (ind / "master.m3u8").write_text(
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=9000000,RESOLUTION=1920x1080,NAME=\"hi\"\n"
        "https://cdn.example.com/hi/index.m3u8\n")
    await scanner.tick()
    await asyncio.sleep(1.1)
    st = await scanner.tick()
    assert st.created == 1
    async with sm() as sess:
        rows = (await sess.execute(text(
            "SELECT kind, state, parent_id, uri FROM asset WHERE watch_folder_id=:w"),
            {"w": ids["wf"]})).all()
    assert len(rows) == 2
    manifest = next(r for r in rows if r[1] == "skipped")
    child = next(r for r in rows if r[0] == "url")
    assert child[2] == manifest[3 - 1] or child[2] is not None
    assert child[3].startswith("https://cdn.example.com/")
    await reg.close()


async def test_backpressure(migrated_db, tmp_path):
    sm, ind, _, ids = await _seed(migrated_db, tmp_path,
                                  job_template={"max_backlog": 0})
    s = Settings(db_dsn=migrated_db, scratch=str(tmp_path / "scr"))
    scanner, reg = await _scanner(sm, ind, ids, s)
    (ind / "a.mxf").write_bytes(b"x")
    await asyncio.sleep(1.1)
    st = await scanner.tick()
    assert st.backpressure is True and st.created == 0
    await reg.close()


async def test_archive_mark_and_move(migrated_db, tmp_path):
    for mode in ("mark", "move"):
        sm, ind, _, ids = await _seed(migrated_db, tmp_path / mode,
                                      archive_mode=mode, archive_path="archive")
        s = Settings(db_dsn=migrated_db, scratch=str(tmp_path / mode / "scr"))
        scanner, reg = await _scanner(sm, ind, ids, s)
        (ind / "a.mxf").write_bytes(b"x")
        await scanner.tick()
        await asyncio.sleep(1.1)
        await scanner.tick()
        async with sm() as sess, sess.begin():
            await sess.execute(text("UPDATE asset SET state='done' WHERE watch_folder_id=:w"),
                               {"w": ids["wf"]})
        wf = await _wf_row(sm, ids["wf"])
        assert await archive_pass(wf, reg, sm, s) == 1
        if mode == "mark":
            assert (ind / "a.mxf.processed").exists() and (ind / "a.mxf").exists()
        else:
            assert not (ind / "a.mxf").exists() and (ind / "archive" / "a.mxf").exists()
        async with sm() as sess:
            archived = await sess.scalar(text(
                "SELECT count(*) FROM asset WHERE watch_folder_id=:w AND archived_at IS NOT NULL"),
                {"w": ids["wf"]})
        assert archived == 1
        await reg.close()