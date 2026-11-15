from __future__ import annotations

import asyncio
import time
import uuid

import httpx
import pytest

from mf_core.storage.base import StorageRow
from mf_core.storage.s3 import S3Backend

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def minio_endpoint():
    tc = pytest.importorskip("testcontainers.core.container")
    with tc.DockerContainer("minio/minio:latest") \
            .with_env("MINIO_ROOT_USER", "mfadmin") \
            .with_env("MINIO_ROOT_PASSWORD", "mfadmin123") \
            .with_command("server /data --address :9000") \
            .with_exposed_ports(9000) as c:
        url = f"http://{c.get_container_host_ip()}:{c.get_exposed_port(9000)}"
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if httpx.get(f"{url}/minio/health/ready", timeout=2).status_code == 200:
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1)
        else:
            pytest.skip("MinIO не поднялся")
        yield url


async def _backend(minio_endpoint, direct=True) -> S3Backend:
    row = StorageRow(
        id=uuid.uuid4(), name="s3t", kind="s3", base_uri="s3://mf-test",
        options={"endpoint_url": minio_endpoint, "region": "us-east-1",
                 "direct_publish": direct},
    )
    be = S3Backend(row, {"access_key": "mfadmin", "secret_key": "mfadmin123"})
    fs = await be.fs()
    if not await fs._exists("mf-test"):
        await fs._mkdir("mf-test")
    return be


async def test_s3_roundtrip(minio_endpoint, tmp_path):
    be = await _backend(minio_endpoint)
    src = tmp_path / "big.bin"
    src.write_bytes(b"Z" * (12 * 1024 * 1024))     # > 8 MiB → multipart
    entry = await be.upload(src, "out/big.bin")
    assert entry.size == src.stat().st_size
    dst = tmp_path / "back.bin"
    res = await be.download("out/big.bin", dst)
    assert dst.read_bytes() == src.read_bytes() and res.size == entry.size
    names = [e.path async for e in be.list(recursive=True)]
    assert "out/big.bin" in names
    await be.delete("out/big.bin")
    assert not await be.exists("out/big.bin")
    await be.close()


async def test_s3_resume(minio_endpoint, tmp_path):
    be = await _backend(minio_endpoint)
    payload = b"Q" * 3_000_000
    src = tmp_path / "r.bin"; src.write_bytes(payload)
    await be.upload(src, "r.bin", overwrite=True)
    dst = tmp_path / "dl.bin"
    (tmp_path / "dl.bin.part").write_bytes(payload[:1_000_000])
    res = await be.download("r.bin", dst)
    assert res.resumed_from == 1_000_000
    assert dst.read_bytes() == payload
    await be.close()


async def test_s3_tmp_publish_mode(minio_endpoint, tmp_path):
    be = await _backend(minio_endpoint, direct=False)
    src = tmp_path / "t.bin"; src.write_bytes(b"hello")
    await be.upload(src, "tmpmode/t.bin")
    names = [e.path async for e in be.list("tmpmode", recursive=True)]
    assert names == ["tmpmode/t.bin"]              # tmp-ключ удалён
    await be.close()