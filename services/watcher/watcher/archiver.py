"""Архивация исходников после успешной конвертации: mark (.processed) или move."""
from __future__ import annotations

import posixpath

from sqlalchemy import text

from mf_core.config import Settings
from mf_core.errors import ConfigInvalidError
from mf_core.storage import StorageRegistry
from mf_core.telemetry import get_logger
from watcher import metrics as M

log = get_logger("mf.archive")

SELECT_DONE = text("""
SELECT id, uri, storage_id FROM asset
WHERE watch_folder_id = :wf AND state = 'done' AND archived_at IS NULL
  AND storage_id IS NOT NULL AND kind = 'file'
ORDER BY updated_at LIMIT :limit
""")


async def archive_pass(wf: dict, registry: StorageRegistry, sf, settings: Settings,
                       limit: int = 50) -> int:
    mode = wf.get("archive_mode") or "none"
    if mode == "none":
        return 0
    async with sf() as sess:
        rows = (await sess.execute(SELECT_DONE, {"wf": wf["id"], "limit": limit})).mappings().all()
    if not rows:
        return 0

    done = 0
    for row in rows:
        try:
            be, rel = registry.resolve(row["uri"])
        except ConfigInvalidError as e:
            log.warning("archive_resolve_failed", uri=row["uri"], error=str(e))
            continue
        if be.row.read_only:
            log.warning("archive_skipped_readonly", folder=wf["name"], storage=be.row.name)
            continue
        try:
            if mode == "mark":
                await be.write_bytes(rel + ".processed", b"", overwrite=True)
            elif mode == "move":
                target = _target_path(wf, rel)
                parent = posixpath.dirname(target)
                if parent:
                    await be.mkdirs(parent)
                await be.move(rel, target)
            else:
                return 0
        except Exception as e:  # noqa: BLE001
            log.warning("archive_failed", uri=row["uri"], mode=mode, error=str(e))
            continue
        async with sf() as sess, sess.begin():
            await sess.execute(text("""
                UPDATE asset SET archived_at = now(),
                    meta = meta || jsonb_build_object('archived', true, 'archive_mode', :m),
                    updated_at = now()
                WHERE id = :i AND archived_at IS NULL
            """), {"i": row["id"], "m": mode})
        done += 1
        M.ARCHIVED.labels(wf["name"], mode).inc()
    if done:
        log.info("archived", folder=wf["name"], mode=mode, count=done)
    return done


def _target_path(wf: dict, rel: str) -> str:
    base = (wf.get("archive_path") or "archive").strip("/")
    sub = rel
    root = (wf.get("path") or "").strip("/")
    if root and sub.startswith(root + "/"):
        sub = sub[len(root) + 1:]
    return posixpath.join(base, sub)