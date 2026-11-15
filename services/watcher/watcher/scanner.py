from __future__ import annotations

import posixpath
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from mf_core import parsers, queue as q
from mf_core.config import Settings
from mf_core.errors import ConfigInvalidError, SourceInvalidError
from mf_core.media.presets import apply_overrides, load_preset, validate_spec
from mf_core.models import Preset
from mf_core.parsers import ParseContext, SourceSpec
from mf_core.schemas import PresetSpec
from mf_core.storage.base import Backend, Entry
from mf_core.telemetry import get_logger
from watcher import metrics as M
from watcher.emitter import Emitter
from watcher.stability import StabilityTracker, match_globs

log = get_logger("mf.scanner")


@dataclass(slots=True)
class ScanStats:
    scanned: int = 0
    pending: int = 0
    created: int = 0
    duplicates: int = 0
    errors: int = 0
    truncated: bool = False
    backpressure: bool = False
    elapsed_s: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


class FolderScanner:
    def __init__(self, wf: dict, backend: Backend, emitter: Emitter, sf, settings: Settings,
                 registry) -> None:
        self.wf, self.be, self.emitter = wf, backend, emitter
        self.sf, self.s, self.registry = sf, settings, registry
        self.tracker = StabilityTracker(wf["stability_s"], settings.watcher_stability_ttl_s)
        self._parsed: dict[str, str] = {}      # uri -> fingerprint, чтобы не парсить заново
        self.label = wf["name"]

    # ------------------------------ public ------------------------------
    async def tick(self) -> ScanStats:
        st = ScanStats()
        started = time.monotonic()
        wf = self.wf
        limit = int((wf.get("job_template") or {}).get("max_backlog") or self.s.watcher_max_backlog)
        async with self.sf() as sess:
            backlog = await q.backlog(sess, wf["id"])
        M.BACKLOG.labels(self.label).set(backlog)
        if backlog >= limit:
            st.backpressure = True
            M.SCANS.labels(self.label, "backpressure").inc()
            log.info("scan_backpressure", folder=self.label, backlog=backlog, limit=limit)
            return st

        try:
            preset, spec = await self._resolve_preset()
        except ConfigInvalidError as e:
            await self._save_error({"class": "config_invalid", "message": str(e)})
            M.SCANS.labels(self.label, "config_error").inc()
            st.errors += 1
            return st

        parent_cache: dict[str, Any] = {}
        count = 0
        try:
            async for entry in self.be.list(wf["path"], wf["recursive"]):
                count += 1
                if count > self.s.watcher_max_entries:
                    st.truncated = True
                    break
                if entry.is_dir or not match_globs(entry.name, entry.path,
                                                   wf["include"], wf["exclude"]):
                    continue
                st.scanned += 1
                try:
                    if wf["ready_marker"] and not await self._marker_ready(entry):
                        st.pending += 1
                        continue
                    if not self.tracker.is_stable(entry.uri, entry.size, entry.mtime):
                        st.pending += 1
                        continue
                    await self._handle_entry(entry, preset, spec, st, parent_cache)
                except SourceInvalidError:
                    st.errors += 1
                except Exception as e:  # noqa: BLE001
                    st.errors += 1
                    log.warning("scan_entry_failed", folder=self.label, uri=entry.uri,
                                error=str(e))
        except Exception as e:  # noqa: BLE001
            await self._save_error({"class": "storage", "message": str(e)})
            M.SCANS.labels(self.label, "storage_error").inc()
            st.errors += 1

        self.tracker.prune()
        M.PENDING.labels(self.label).set(self.tracker.pending())
        st.pending = self.tracker.pending()
        st.elapsed_s = round(time.monotonic() - started, 3)
        M.SCAN_DURATION.labels(self.label).observe(st.elapsed_s)
        M.SCANS.labels(self.label, "ok" if not st.errors else "with_errors").inc()
        await self._save_scan(st)
        return st

    # ----------------------------- internals ----------------------------
    async def _resolve_preset(self) -> tuple[Preset, PresetSpec]:
        tpl = self.wf.get("job_template") or {}
        async with self.sf() as sess:
            preset = await load_preset(sess, preset_id=self.wf.get("preset_id"),
                                       name=tpl.get("preset_name"))
            raw = apply_overrides(dict(preset.spec), tpl.get("overrides"))
            return preset, validate_spec(raw)

    async def _marker_ready(self, entry: Entry) -> bool:
        tpl = self.wf["ready_marker"]
        stem = entry.name.rsplit(".", 1)[0] if "." in entry.name else entry.name
        marker = tpl.format(name=entry.name, stem=stem, path=entry.path)
        rel = posixpath.join(posixpath.dirname(entry.path), marker)
        return await self.be.exists(rel)

    async def _handle_entry(self, entry: Entry, preset: Preset, spec: PresetSpec,
                            st: ScanStats, parent_cache: dict) -> None:
        wf = self.wf
        if not wf["parser_id"]:
            src = SourceSpec(uri=entry.uri, kind="file", meta={})
            await self._emit(src, preset, spec, st,
                             storage_id=wf["storage_id"], fingerprint=entry.fingerprint,
                             size=entry.size, mtime=entry.mtime)
            self.tracker.forget(entry.uri)
            return

        fp = entry.fingerprint
        if self._parsed.get(entry.uri) == fp:
            return
        parser = parsers.get(wf["parser_id"])
        ctx = ParseContext(backend=self.be, entry=entry, options=wf["parser_opts"] or {},
                           base_uri=self.be.row.base_uri)
        try:
            sources = await parser.parse(ctx)
        except Exception as e:  # noqa: BLE001
            M.PARSE_ERRORS.labels(self.label, wf["parser_id"]).inc()
            st.errors += 1
            log.warning("parse_failed", folder=self.label, parser=wf["parser_id"],
                        uri=entry.uri, error=str(e))
            self._parsed[entry.uri] = fp        # не долбиться в битый файл каждый скан
            return

        parent_id = await self._manifest_asset(entry, parent_cache)
        for src in sources:
            try:
                ready, meta = await self._child_ready(src)
                if not ready:
                    st.pending += 1
                    continue
                await self._emit(src, preset, spec, st, parent_asset_id=parent_id, **meta)
            except Exception as e:  # noqa: BLE001
                st.errors += 1
                log.warning("child_emit_failed", folder=self.label, uri=src.uri, error=str(e))
        self._parsed[entry.uri] = fp
        self.tracker.forget(entry.uri)

    async def _manifest_asset(self, entry: Entry, cache: dict) -> Any:
        if entry.uri in cache:
            return cache[entry.uri]
        async with self.sf() as sess, sess.begin():
            pid = await self.emitter.register_manifest(
                sess, self.wf, uri=entry.uri, storage_id=self.wf["storage_id"],
                fingerprint=entry.fingerprint, size=entry.size, mtime=entry.mtime,
                meta={"parser": self.wf["parser_id"]},
            )
        cache[entry.uri] = pid
        return pid

    async def _child_ready(self, src: SourceSpec) -> tuple[bool, dict]:
        if src.kind != "file":
            return True, {"fingerprint": src.identity or "url"}
        try:
            be, rel = self.registry.resolve(src.uri)
        except ConfigInvalidError:
            return True, {"fingerprint": src.identity or "url"}
        try:
            e = await be.stat(rel)
        except Exception:  # noqa: BLE001
            return False, {}
        if not self.tracker.is_stable(src.uri, e.size, e.mtime):
            return False, {}
        self.tracker.forget(src.uri)
        return True, {"storage_id": be.row.id, "fingerprint": e.fingerprint,
                      "size": e.size, "mtime": e.mtime}

    async def _emit(self, src: SourceSpec, preset: Preset, spec: PresetSpec, st: ScanStats,
                    **kw) -> None:
        async with self.sf() as sess, sess.begin():
            res = await self.emitter.emit(sess, self.wf, src, preset, spec, **kw)
        if res.created:
            st.created += 1
            M.ASSETS_CREATED.labels(self.label).inc()
            M.JOBS_CREATED.labels(self.label).inc()
            log.info("job_created", folder=self.label, uri=src.uri, job_id=str(res.job_id))
        else:
            st.duplicates += 1

    async def _save_scan(self, st: ScanStats) -> None:
        async with self.sf() as sess, sess.begin():
            await sess.execute(text("""
                UPDATE watch_folder SET last_scan_at = now(),
                    last_error = '{}'::jsonb, updated_at = now() WHERE id = :i
            """), {"i": self.wf["id"]})

    async def _save_error(self, err: dict) -> None:
        import json
        async with self.sf() as sess, sess.begin():
            await sess.execute(text("""
                UPDATE watch_folder SET last_scan_at = now(),
                    last_error = CAST(:e AS jsonb), updated_at = now() WHERE id = :i
            """), {"e": json.dumps(err, ensure_ascii=False), "i": self.wf["id"]})