import asyncio
import uuid

import pytest

from mf_core.errors import CancelledByUser, PublishConflictError, StorageAuthError
from mf_core.storage.base import StorageRow
from mf_core.storage.local import LocalBackend


def make(tmp_path, **kw) -> LocalBackend:
    row = StorageRow(id=uuid.uuid4(), name="t", kind="local",
                     base_uri=f"file://{tmp_path}", **kw)
    return LocalBackend(row)


async def test_list_stat_recursive(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a/x.mp4").write_bytes(b"x" * 10)
    (tmp_path / "y.mxf").write_bytes(b"y" * 20)
    be = make(tmp_path)
    flat = [e.path async for e in be.list()]
    deep = sorted(e.path async for e in be.list(recursive=True))
    assert flat == ["y.mxf"]
    assert deep == ["a/x.mp4", "y.mxf"]
    st = await be.stat("a/x.mp4")
    assert st.size == 10 and st.fingerprint.startswith("10:")


async def test_upload_is_atomic_and_no_tmp_left(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"payload" * 1000)
    be = make(tmp_path / "out" if (tmp_path / "out").mkdir() is None else tmp_path / "out")
    entry = await be.upload(src, "pub/file.bin")
    assert entry.size == src.stat().st_size
    assert [e.path async for e in be.list(recursive=True)] == ["pub/file.bin"]
    assert await be.checksum("pub/file.bin") == __import__("hashlib").sha256(src.read_bytes()).hexdigest()


async def test_upload_conflict(tmp_path):
    src = tmp_path / "s.bin"; src.write_bytes(b"1")
    be = make(tmp_path)
    await be.upload(src, "f.bin")
    with pytest.raises(PublishConflictError):
        await be.upload(src, "f.bin")
    assert (await be.upload(src, "f.bin", overwrite=True)).size == 1


async def test_download_resume(tmp_path):
    root = tmp_path / "store"; root.mkdir()
    payload = b"A" * 5000 + b"B" * 5000
    (root / "f.bin").write_bytes(payload)
    be = make(root)
    dst = tmp_path / "dl" / "f.bin"
    dst.parent.mkdir()
    # имитируем обрыв: половина уже в .part
    part = tmp_path / "dl" / "f.bin.part"
    part.write_bytes(payload[:5000])

    seen = []
    res = await be.download("f.bin", dst, on_progress=lambda p: seen.append(p))
    assert dst.read_bytes() == payload
    assert res.resumed_from == 5000
    assert not part.exists()
    assert seen and seen[-1]["pct"] == 100.0


async def test_download_restart_when_source_shrunk(tmp_path):
    root = tmp_path / "s"; root.mkdir()
    (root / "f.bin").write_bytes(b"new")
    be = make(root)
    dst = tmp_path / "d.bin"
    (tmp_path / "d.bin.part").write_bytes(b"old-longer-content")
    await be.download("f.bin", dst)
    assert dst.read_bytes() == b"new"


async def test_download_cancel(tmp_path):
    root = tmp_path / "s"; root.mkdir()
    (root / "f.bin").write_bytes(b"x" * 100000)
    be = make(root)
    ev = asyncio.Event(); ev.set()
    with pytest.raises(CancelledByUser):
        await be.download("f.bin", tmp_path / "o.bin", cancel=ev)


async def test_path_traversal_blocked(tmp_path):
    be = make(tmp_path)
    with pytest.raises(StorageAuthError):
        await be.stat("../../etc/passwd")


async def test_read_only_guard(tmp_path):
    be = make(tmp_path, read_only=True)
    src = tmp_path / "s.bin"; src.write_bytes(b"1")
    with pytest.raises(Exception, match="read_only"):
        await be.upload(src, "x.bin")