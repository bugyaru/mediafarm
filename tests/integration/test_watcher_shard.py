from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from mf_core.config import Settings
from mf_core.queue import advisory_lock
from watcher.main import folder_lock_key

pytestmark = pytest.mark.integration


async def test_folder_lock_is_exclusive(migrated_db):
    s = Settings(db_dsn=migrated_db)
    fid = uuid.uuid4()
    key = folder_lock_key(s, fid)
    e1 = create_async_engine(migrated_db, poolclass=NullPool)
    e2 = create_async_engine(migrated_db, poolclass=NullPool)
    try:
        async with advisory_lock(e1, key) as first:
            assert first is True
            async with advisory_lock(e2, key) as second:
                assert second is False        # вторую реплику к папке не пускаем
        async with advisory_lock(e2, key) as third:
            assert third is True              # после освобождения — пускаем
    finally:
        await e1.dispose()
        await e2.dispose()


def test_lock_keys_are_stable_and_distinct():
    s = Settings()
    a, b = uuid.uuid4(), uuid.uuid4()
    assert folder_lock_key(s, a) == folder_lock_key(s, a)
    assert folder_lock_key(s, a) != folder_lock_key(s, b)
    assert folder_lock_key(s, a) >= s.watcher_lock_base