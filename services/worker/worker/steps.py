from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiofiles
import httpx
from sqlalchemy import text

from mf_core.errors import (
    ConfigInvalidError, HWAccelError, PublishConflictError, ResourceError, SourceInvalidError,
)
from mf_core.media import ytdlp
from mf_core.media.ffmbc import build_ffmbc_generic, build_imx
from mf_core.media.ffmpeg import (
    build_loudnorm_pass1, build_thumbnail, build_transcode, parse_loudnorm,
)
from mf_core.media.probe import detect_interlace, ffprobe
from mf_core.media.progress import run_media
from mf_core.media.verify import verify_output
from mf_core.models import Artifact
from mf_core.schemas import StepSpec
from mf_core.storage.local import LocalBackend
from mf_core.storage.template import build_context, render
from mf_core.telemetry import TRANSFER_BYTES, get_logger
from worker import scratch
from worker.context import JobContext
from worker.egress import check_url

log = get_logger("mf.steps")

DIRECT_EXT = (".m3u8", ".mpd", ".mp4", ".mkv", ".mov", ".ts", ".m4v", ".webm", ".mp3", ".m4a")
DIRECT_SCHEMES = ("rtmp", "rtmps", "rtsp", "srt", "mms", "mmsh")
CONTAINER_EXT = {"mp4": "mp4", "mov": "mov", "mkv": "mkv", "matroska": "mkv", "mxf": "mxf",
                 "mxf_d10": "mxf", "ts": "ts", "mpegts": "ts", "webm": "webm", "mp3": "mp3",
                 "wav": "wav", "flac": "flac"}


def _suffix(uri: str) -> str:
    name = urlsplit(uri).path.rsplit("/", 1)[-1]
    return ("." + name.rsplit(".", 1)[-1]) if "." in name else ".bin"


async def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    async with aiofiles.open(path, "rb") as f:
        while chunk := await f.read(4 * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


async def _http_download(ctx: JobContext, url: str, dst: Path) -> Path:
    part = Path(str(dst) + ctx.s.storage_tmp_suffix)
    offset = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    timeout = httpx.Timeout(30.0, read=120.0, connect=15.0)
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        async with client.stream("GET", url, headers=headers) as r:
            if offset and r.status_code == 200:
                offset = 0                       # сервер проигнорировал Range
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0) + offset
            scratch.ensure_space(ctx.s, ctx.workdir, total)
            mode = "ab" if offset else "wb"
            async with aiofiles.open(part, mode) as f:
                done = offset
                async for chunk in r.aiter_bytes(ctx.s.storage_chunk_mb * 1024 * 1024):
                    ctx.check_cancel()
                    await f.write(chunk)
                    done += len(chunk)
                    ctx.on_progress({"bytes": done, "total": total or None})
                await f.flush()
    if total and part.stat().st_size != total:
        raise SourceInvalidError(f"HTTP: размер не совпал {part.stat().st_size} != {total}")
    part.replace(dst)
    TRANSFER_BYTES.labels("download", "http").inc(part.stat().st_size)
    return dst


# --------------------------------- acquire ---------------------------------
async def acquire(step: StepSpec, ctx: JobContext) -> dict[str, Any]:
    params = ctx.job.params
    url = params.get("url")
    src = url or params.get("input_uri") or (ctx.asset or {}).get("uri")
    if not src:
        raise ConfigInvalidError("не задан источник (url / input_uri / asset)")

    if url:
        check_url(url, ctx.s)
        scheme = urlsplit(url).scheme.lower()
        direct = scheme in DIRECT_SCHEMES or urlsplit(url).path.lower().endswith(DIRECT_EXT)
        use_yt = step.engine == "ytdlp" or (step.engine is None and not direct)
        if use_yt:
            extra = step.model_extra or {}
            res = await ytdlp.download(
                url, outdir=ctx.workdir, on_progress=ctx.on_progress, cancel=ctx.cancel,
                settings=ctx.s, resume=step.resume, live=bool(extra.get("live")),
                live_from_start=bool(extra.get("live_from_start")),
                max_duration_s=extra.get("max_duration_s"),
                cookies_file=extra.get("cookies_file"), archive_file=extra.get("archive_file"),
                format_selector=extra.get("format"), playlist=bool(extra.get("playlist")),
            )
            ctx.input, ctx.input_local, ctx.yt_info = res.path, True, res.info
            TRANSFER_BYTES.labels("download", "ytdlp").inc(Path(res.path).stat().st_size)
            return {"input": res.path, "local": True, "source": "ytdlp",
                    "title": res.info.get("title"), "size": Path(res.path).stat().st_size}
        if step.mode == "stream":
            ctx.input, ctx.input_local = url, False
            return {"input": url, "local": False, "source": "http-stream"}
        dst = ctx.workdir / ("input" + _suffix(url))
        await _http_download(ctx, url, dst)
        ctx.input, ctx.input_local = str(dst), True
        return {"input": str(dst), "local": True, "source": "http",
                "size": dst.stat().st_size}

    be, rel = ctx.registry.resolve(src)
    entry = await be.stat(rel)
    if isinstance(be, LocalBackend):
        local = Path(be.root) / rel
        ctx.input, ctx.input_local = str(local), True
        return {"input": str(local), "local": True, "source": be.kind, "size": entry.size}

    scratch.ensure_space(ctx.s, ctx.workdir, entry.size * 2)
    dst = ctx.workdir / ("input" + _suffix(entry.name))
    res = await be.download(rel, dst, resume=step.resume,
                            on_progress=ctx.on_progress, cancel=ctx.cancel)
    TRANSFER_BYTES.labels("download", be.kind).inc(res.size)
    ctx.input, ctx.input_local = str(dst), True
    return {"input": str(dst), "local": True, "source": be.kind,
            "size": res.size, "resumed_from": res.resumed_from}


# ---------------------------------- probe ----------------------------------
async def probe(step: StepSpec, ctx: JobContext) -> dict[str, Any]:
    if not ctx.input:
        raise ConfigInvalidError("probe: нет входа (шаг acquire не выполнен)")
    info = await ffprobe(ctx.input, settings=ctx.s)
    if not info.has_video and not info.has_audio:
        raise SourceInvalidError("в источнике нет медиапотоков", uri=ctx.input)

    transcode = next((s for s in ctx.spec.steps if s.id == "transcode"), None)
    need_idet = (
            transcode is not None and transcode.deinterlace == "auto" and info.video is not None
            and info.video.field_order in (None, "unknown")
            and (ctx.job.params.get("idet", True))
    )
    idet: dict[str, Any] = {}
    if need_idet:
        idet = await detect_interlace(ctx.input, frames=150, settings=ctx.s)
        if idet.get("interlaced") is not None and idet.get("confidence", 0) > 0.6:
            v = dataclasses.replace(info.video, interlaced=bool(idet["interlaced"]),
                                    field_order=idet.get("field_order"))
            info = dataclasses.replace(info, video=v)
    ctx.info = info

    if ctx.asset:
        async with ctx.sf() as s, s.begin():
            await s.execute(
                text("UPDATE asset SET probe = CAST(:p AS jsonb), updated_at = now() WHERE id = :i"),
                {"p": json.dumps(info.to_dict(), ensure_ascii=False), "i": ctx.asset["id"]},
            )
    return {"probe": info.to_dict(), "idet": idet}


# -------------------------------- transcode --------------------------------
async def transcode(step: StepSpec, ctx: JobContext) -> dict[str, Any]:
    if ctx.info is None or ctx.input is None:
        raise ConfigInvalidError("transcode: нужен шаг probe")
    ctx.check_cancel()
    ext = CONTAINER_EXT.get((step.container or "mkv").lower(), "mkv")
    out = ctx.workdir / f"output.{ext}"
    out.unlink(missing_ok=True)
    scratch.ensure_space(ctx.s, ctx.workdir, (out.stat().st_size if out.exists() else 0)
                         + int((ctx.info.size or 0) * 1.5) + 64 * 1024**2)

    engine = step.engine or "ffmpeg"
    if engine == "ffmbc":
        extra = step.model_extra or {}
        if extra.get("profile", "").startswith("imx") or extra.get("standard"):
            cmd = build_imx(step, ctx.info, src=ctx.input, dst=str(out), engine="ffmbc",
                            standard=extra.get("standard", "pal"),
                            bitrate_mbps=int(extra.get("bitrate_mbps", 50)),
                            audio_channels=int(extra.get("audio_channels", 2)), settings=ctx.s)
        else:
            cmd = build_ffmbc_generic(step, ctx.info, src=ctx.input, dst=str(out), settings=ctx.s)
        res = await run_media(cmd, duration_s=ctx.info.duration, on_progress=ctx.on_progress,
                              cancel=ctx.cancel, settings=ctx.s, engine="ffmbc",
                              codec="mpeg2video", log_file=str(ctx.workdir / "transcode.log"))
        ctx.metrics.update({"engine": "ffmbc", "hw": "cpu", "speed": res.avg_speed})
        return {"path": str(out), "size": out.stat().st_size, "engine": "ffmbc",
                "hw": False, "speed": res.avg_speed}

    # ffmpeg: двухпроходный loudnorm при необходимости
    measured: dict[str, Any] | None = None
    if any(a.loudnorm == "ebu_r128" for a in step.audio):
        p1 = build_loudnorm_pass1(ctx.info, src=ctx.input, settings=ctx.s)
        ln_log = ctx.workdir / "loudnorm.log"
        r1 = await run_media(p1, duration_s=ctx.info.duration, cancel=ctx.cancel,
                             settings=ctx.s, engine="ffmpeg", codec="loudnorm",
                             log_file=str(ln_log))
        measured = parse_loudnorm(ln_log.read_text(encoding="utf-8"))
        ctx.metrics["loudnorm"] = measured
        _ = r1

    dec = ctx.hw.decide(step, ctx.info)
    if step.hwaccel == "cuda" and not dec.use_cuda and not step.fallback_to_cpu:
        raise HWAccelError(f"CUDA недоступна: {dec.reason}", reason=dec.reason)

    cmd = build_transcode(step, ctx.info, ctx.hw, src=ctx.input, dst=str(out),
                          decision=dec, loudnorm=measured, interactive=True, settings=ctx.s)
    codec = (step.video.codec if dec.use_cuda else step.video.cpu_codec) if step.video else "copy"
    res = await run_media(cmd, duration_s=ctx.info.duration, on_progress=ctx.on_progress,
                          cancel=ctx.cancel, stdin_q=True, settings=ctx.s,
                          engine="ffmpeg", codec=codec,
                          log_file=str(ctx.workdir / "transcode.log"))
    if not out.exists() or out.stat().st_size == 0:
        raise SourceInvalidError("ffmpeg не создал результат", path=str(out))
    ctx.metrics.update({"engine": "ffmpeg", "hw": "cuda" if dec.use_cuda else "cpu",
                        "hw_reason": dec.reason, "codec": codec, "speed": res.avg_speed})
    return {"path": str(out), "size": out.stat().st_size, "engine": "ffmpeg",
            "hw": dec.use_cuda, "codec": codec, "speed": res.avg_speed}


# -------------------------------- thumbnail --------------------------------
async def thumbnail(step: StepSpec, ctx: JobContext) -> dict[str, Any]:
    if ctx.info is None or ctx.input is None:
        return {"skipped": "no input"}
    duration = ctx.info.duration or 0
    paths: list[str] = []
    marks = step.at or ["10%"]
    for i, mark in enumerate(marks):
        ctx.check_cancel()
        at_s = duration * float(mark.rstrip("%")) / 100 if mark.endswith("%") else float(mark)
        dst = ctx.workdir / f"thumb_{i:02d}.jpg"
        cmd = build_thumbnail(step, ctx.info, src=ctx.input, dst=str(dst),
                              at_s=min(at_s, max(0.0, duration - 0.1)), settings=ctx.s)
        await run_media(cmd, settings=ctx.s, engine="ffmpeg", codec="mjpeg",
                        stall_timeout_s=60)
        if dst.exists():
            paths.append(str(dst))
    return {"paths": paths}


# ---------------------------------- verify ---------------------------------
async def verify(step: StepSpec, ctx: JobContext) -> dict[str, Any]:
    out = (ctx.outputs.get("transcode") or ctx.outputs.get("capture")
           or ctx.outputs.get("acquire") or {})
    path = out.get("path") or out.get("input")
    if not path:
        raise ConfigInvalidError("verify: нечего проверять")
    return await verify_output(path, checks=step.checks, src_info=ctx.info, local_path=path)


# ---------------------------------- publish --------------------------------
async def publish(step: StepSpec, ctx: JobContext) -> dict[str, Any]:
    out = (ctx.outputs.get("transcode") or ctx.outputs.get("capture")
           or ctx.outputs.get("acquire") or {})
    src_path = out.get("path") or out.get("input")
    if not src_path or not Path(src_path).is_file():
        raise ConfigInvalidError("publish: локальный результат не найден")

    override = ctx.job.params.get("output") or {}
    storage_name = override.get("storage") or step.storage
    template = override.get("path") or step.path
    if not storage_name or not template:
        raise ConfigInvalidError("publish: не заданы storage/path")

    be = ctx.registry.by_name(storage_name)
    row = ctx.registry.row_by_name(storage_name)
    asset = ctx.asset or {}
    tctx = build_context(asset_uri=asset.get("uri") or ctx.input or "",
                         asset_meta=asset.get("meta") or ctx.yt_info,
                         preset_name=ctx.spec.name, job_id=str(ctx.job.id))
    rel = render(template, tctx)

    mode = step.on_conflict
    if mode == "version":
        base, dot, ext = rel.rpartition(".")
        n = 2
        while await be.exists(rel):
            rel = f"{base}_v{n}{dot}{ext}" if dot else f"{rel}_v{n}"
            n += 1
    elif mode == "skip" and await be.exists(rel):
        await ctx.event("publish_skipped", rel, data={"uri": be.uri(rel)})
        return {"skipped": True, "uri": be.uri(rel), "artifacts": []}

    want_checksum = bool((step.model_extra or {}).get("checksum"))
    checksum = await _sha256(Path(src_path)) if want_checksum else None
    entry = await be.upload(src_path, rel, overwrite=(mode == "overwrite"),
                            on_progress=ctx.on_progress, cancel=ctx.cancel, checksum=checksum)
    TRANSFER_BYTES.labels("upload", be.kind).inc(entry.size)

    records = [{"storage_id": row.id, "uri": entry.uri, "role": "output",
                "size": entry.size, "checksum": checksum, "probe": {}}]

    for i, tp in enumerate((ctx.outputs.get("thumbnail") or {}).get("paths", [])):
        base = rel.rsplit(".", 1)[0]
        trel = f"{base}_thumb_{i:02d}.jpg"
        te = await be.upload(tp, trel, overwrite=True, cancel=ctx.cancel)
        records.append({"storage_id": row.id, "uri": te.uri, "role": "thumbnail",
                        "size": te.size, "checksum": None, "probe": {}})

    async with ctx.sf() as s, s.begin():
        for rec in records:
            s.add(Artifact(job_id=ctx.job.id, **rec))
        if ctx.asset:
            await s.execute(text("UPDATE asset SET state='done', updated_at=now() WHERE id=:i"),
                            {"i": ctx.asset["id"]})
    ctx.artifacts.extend(records)
    return {"uri": entry.uri, "size": entry.size, "artifacts": len(records)}


# ---------------------------------- notify ---------------------------------
async def notify(step: StepSpec, ctx: JobContext) -> dict[str, Any]:
    url = (step.model_extra or {}).get("url") or ctx.job.params.get("notify_url")
    if not url:
        return {"skipped": "no url"}
    payload = {"job_id": str(ctx.job.id), "state": "succeeded",
               "artifacts": [a["uri"] for a in ctx.artifacts], "metrics": ctx.metrics}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
    return {"status": r.status_code}

async def capture(step: StepSpec, ctx: JobContext) -> dict[str, Any]:
    from mf_core.media.ffmpeg import build_capture
    src = ctx.input or ctx.job.params.get("url")
    if not src:
        raise ConfigInvalidError("capture: нет источника")
    extra = step.model_extra or {}
    out = ctx.workdir / f"capture.{CONTAINER_EXT.get((step.container or 'mkv'), 'mkv')}"
    cmd = build_capture(src=src, dst=str(out), duration_s=extra.get("duration_s"),
                        copy=bool(extra.get("copy", True)),
                        segment_s=extra.get("segment_s"), settings=ctx.s)
    res = await run_media(cmd, duration_s=extra.get("duration_s"), on_progress=ctx.on_progress,
                          cancel=ctx.cancel, settings=ctx.s, engine="ffmpeg", codec="copy")
    ctx.metrics.update({"engine": "ffmpeg", "hw": "cpu", "speed": res.avg_speed})
    return {"path": str(out), "size": out.stat().st_size, "engine": "ffmpeg", "hw": False}

HANDLERS = {
    "acquire": acquire, "probe": probe, "transcode": transcode, "thumbnail": thumbnail,
    "verify": verify, "publish": publish, "notify": notify,
}