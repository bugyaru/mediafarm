from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith(("MF_", "MF_SECRET_")):
            monkeypatch.delenv(k, raising=False)
    from mf_core.config import reset_settings_cache
    from mf_core.secrets import clear_cache
    reset_settings_cache()
    clear_cache()
    yield
    reset_settings_cache()
    clear_cache()


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    if not _docker_available():
        pytest.skip("Docker недоступен — интеграционные тесты пропущены")
    tc = pytest.importorskip("testcontainers.postgres")
    with tc.PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url(driver="asyncpg")


@pytest.fixture(scope="session")
def migrated_db(pg_dsn: str) -> str:
    env = {**os.environ, "MF_DB_DSN": pg_dsn, "MF_LOG_LEVEL": "WARNING"}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT, env=env, check=True, capture_output=True,
    )
    return pg_dsn