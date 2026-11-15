"""SMB/FTP проверяются против реальных тестовых серверов.

Запуск:
  MF_TEST_SMB_URI="smb://fs01/mf-test" \
  MF_SECRET_TST_SMB_USERNAME=... MF_SECRET_TST_SMB_PASSWORD=... \
  MF_TEST_FTP_URI="ftp://ftp01/mf-test" ... pytest -m integration

проброса диапазона портов, случайный маппинг testcontainers его ломает).
"""
from __future__ import annotations

import os
import uuid

import pytest

from mf_core.storage.base import StorageRow
from mf_core.storage.ftp import FtpBackend
from mf_core.storage.smb import SmbBackend

pytestmark = pytest.mark.integration


def _row(kind: str, uri: str, secret_name: str) -> StorageRow:
    return StorageRow(id=uuid.uuid4(), name=f"t_{kind}", kind=kind, base_uri=uri,
                      secret_ref=f"env:{secret_name}")


async def _roundtrip(be, tmp_path):
    from mf_core.secrets import SecretResolver  # noqa: F401
    payload = b"S" * 2_000_000
    src = tmp_path / "src.bin"; src.write_bytes(payload)
    key = f"mf-test/{uuid.uuid4().hex}.bin"
    entry = await be.upload(src, key)
    assert entry.size == len(payload)
    dst = tmp_path / "dl.bin"
    (tmp_path / "dl.bin.part").write_bytes(payload[:500_000])
    res = await be.download(key, dst)
    assert dst.read_bytes() == payload and res.resumed_from == 500_000
    listed = [e.path async for e in be.list("mf-test", recursive=True)]
    assert key in listed
    await be.delete(key)
    assert not await be.exists(key)


async def test_smb(tmp_path):
    uri = os.getenv("MF_TEST_SMB_URI")
    if not uri:
        pytest.skip("MF_TEST_SMB_URI не задан")
    await _roundtrip(SmbBackend(_row("smb", uri, "tst_smb"),
                                _creds("MF_SECRET_TST_SMB_")), tmp_path)


async def test_ftp(tmp_path):
    uri = os.getenv("MF_TEST_FTP_URI")
    if not uri:
        pytest.skip("MF_TEST_FTP_URI не задан")
    await _roundtrip(FtpBackend(_row("ftp", uri, "tst_ftp"),
                                _creds("MF_SECRET_TST_FTP_")), tmp_path)


def _creds(prefix: str) -> dict[str, str]:
    return {
        "username": os.environ.get(prefix + "USERNAME", ""),
        "password": os.environ.get(prefix + "PASSWORD", ""),
    }