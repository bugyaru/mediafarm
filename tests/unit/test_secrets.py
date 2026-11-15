import pytest

from mf_core.errors import ConfigInvalidError
from mf_core.secrets import SecretResolver
from mf_core.util import asset_identity, job_idempotency_key, mask, parse_size


def test_env_secret(monkeypatch):
    monkeypatch.setenv("MF_SECRET_IN_SMB_USERNAME", "svc")
    monkeypatch.setenv("MF_SECRET_IN_SMB_PASSWORD", "p@ss")
    r = SecretResolver()
    assert r.resolve("env:in_smb") == {"username": "svc", "password": "p@ss"}
    assert len(r.fingerprint("env:in_smb")) == 16
    assert r.resolve(None) == {}


def test_missing_secret_raises(monkeypatch):
    with pytest.raises(ConfigInvalidError):
        SecretResolver().resolve("env:nope")
    with pytest.raises(ConfigInvalidError):
        SecretResolver().resolve("vault:whatever")


def test_file_secret(tmp_path):
    (tmp_path / "username").write_text("u\n")
    (tmp_path / "password").write_text("p\n")
    (tmp_path / "..data").mkdir()
    data = SecretResolver().resolve(f"file:{tmp_path}")
    assert data == {"username": "u", "password": "p"}


def test_mask_hides_creds():
    out = mask("smb://user:secret@host/share", ["secret"])
    assert "secret" not in out and "***" in out


def test_identity_stability():
    a = asset_identity("sid", "smb://h/s/f.mxf", "100:200")
    assert a == asset_identity("sid", "smb://h/s/f.mxf", "100:200")
    assert a != asset_identity("sid", "smb://h/s/f.mxf", "100:201")
    assert job_idempotency_key(a, "p", 1, "out") != job_idempotency_key(a, "p", 2, "out")


def test_parse_size():
    assert parse_size("6M") == 6_000_000
    assert parse_size("192k") == 192_000
    assert parse_size(None) is None