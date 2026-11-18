from __future__ import annotations

import copy
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mf_core.errors import ConfigInvalidError
from mf_core.media.hwaccel import Hardware
from mf_core.models import Preset
from mf_core.schemas import PresetSpec

BUILTIN_PRESETS: dict[str, dict[str, Any]] = {
    "proxy_1080p_nvenc": {
        "name": "proxy_1080p_nvenc", "version": 1, "requires_gpu": True,
        "gpu_sessions": 1, "cpu_cost": 2,
        "description": "H.264 1080p на NVENC (L40), fallback на CPU отдельным пресетом",
        "steps": [
            {"id": "acquire", "mode": "staged", "resume": True},
            {"id": "probe", "engine": "ffmpeg"},
            {"id": "transcode", "engine": "ffmpeg", "hwaccel": "auto", "fallback_to_cpu": True,
             "deinterlace": "auto",
             "video": {"codec": "h264_nvenc", "cpu_codec": "libx264", "rc": "vbr",
                       "bitrate": "6M", "maxrate": "8M", "bufsize": "12M",
                       "preset": "p5", "profile": "high", "gop": 50, "bframes": 2},
             "scale": {"height": 1080, "mode": "fit"},
             "audio": [{"codec": "aac", "bitrate": "192k", "channels": 2}],
             "container": "mp4", "extra_out": ["-movflags", "+faststart"]},
            {"id": "thumbnail", "engine": "ffmpeg", "at": ["10%", "50%"]},
            {"id": "verify", "checks": ["has_video", "has_audio", "duration_delta<=1.0",
                                        "size>1024"]},
            {"id": "publish", "storage": "out", "path": "{yyyy}/{mm}/{asset.stem}_1080p.mp4",
             "on_conflict": "skip"},
        ],
    },
    "proxy_1080p_cpu": {
        "name": "proxy_1080p_cpu", "version": 1, "requires_gpu": False, "cpu_cost": 4,
        "steps": [
            {"id": "acquire", "mode": "staged"},
            {"id": "probe", "engine": "ffmpeg"},
            {"id": "transcode", "engine": "ffmpeg", "hwaccel": "cpu",
             "video": {"codec": "libx264", "cpu_codec": "libx264", "rc": "cq", "cq": 23,
                       "preset": "p4", "profile": "high", "gop": 50},
             "scale": {"height": 1080, "mode": "fit"},
             "audio": [{"codec": "aac", "bitrate": "192k", "channels": 2}],
             "container": "mp4", "extra_out": ["-movflags", "+faststart"]},
            {"id": "verify", "checks": ["has_video", "duration_delta<=1.0"]},
            {"id": "publish", "storage": "out", "path": "{yyyy}/{mm}/{asset.stem}_cpu.mp4"},
        ],
    },
    "av1_l40": {
        "name": "av1_l40", "version": 1, "requires_gpu": True, "gpu_sessions": 1,
        "description": "AV1 NVENC — Ada/L40, 3 энкодера",
        "steps": [
            {"id": "acquire", "mode": "staged"},
            {"id": "probe", "engine": "ffmpeg"},
            {"id": "transcode", "engine": "ffmpeg", "hwaccel": "cuda",
             "video": {"codec": "av1_nvenc", "rc": "vbr", "bitrate": "3M",
                       "preset": "p5", "gop": 60},
             "scale": {"height": 1080, "mode": "fit"},
             "audio": [{"codec": "libopus", "bitrate": "128k", "channels": 2}],
             "container": "mkv"},
            {"id": "verify", "checks": ["has_video", "duration_delta<=1.5"]},
            {"id": "publish", "storage": "out", "path": "{yyyy}/{mm}/{asset.stem}_av1.mkv"},
        ],
    },
    "imx50_pal_ffmbc": {
        "name": "imx50_pal_ffmbc", "version": 1, "requires_gpu": False, "cpu_cost": 2,
        "description": "IMX 50 Mbit/s PAL MXF OP1a (ffmbc). GPU не используется",
        "selector": {"gpu": "nvidia"},
        "steps": [
            {"id": "acquire", "mode": "staged"},
            {"id": "probe", "engine": "ffmpeg"},
            {"id": "transcode", "engine": "ffmbc", "hwaccel": "cpu", "container": "mxf",
             "profile": "imx50", "standard": "pal", "bitrate_mbps": 50, "audio_channels": 2,
             "video": {"codec": "mpeg2video", "pix_fmt": "yuv422p", "bitrate": "50M"}},
            {"id": "verify", "checks": ["has_video", "has_audio", "height==608"]},
            {"id": "publish", "storage": "out", "path": "{yyyy}/{mm}/{asset.stem}_IMX50.mxf"},
        ],
    },
    "loudnorm_archive": {
        "name": "loudnorm_archive", "version": 1, "requires_gpu": True,
        "steps": [
            {"id": "acquire", "mode": "staged"},
            {"id": "probe", "engine": "ffmpeg"},
            {"id": "transcode", "engine": "ffmpeg", "hwaccel": "auto",
             "video": {"codec": "hevc_nvenc", "cpu_codec": "libx265", "rc": "vbr",
                       "bitrate": "8M", "preset": "p5", "gop": 50},
             "audio": [{"codec": "aac", "bitrate": "192k", "channels": 2,
                        "loudnorm": "ebu_r128"}],
             "container": "mkv"},
            {"id": "verify", "checks": ["has_video", "has_audio"]},
            {"id": "publish", "storage": "out", "path": "{yyyy}/{mm}/{asset.stem}_norm.mkv"},
        ],
    },
    "stream_capture": {
        "name": "stream_capture", "version": 1, "requires_gpu": False, "cpu_cost": 1,
        "description": "Захват web-стрима через yt-dlp с ограничением длительности",
        "steps": [
            {"id": "acquire", "engine": "ytdlp", "mode": "staged", "live": True,
             "max_duration_s": 600},
            {"id": "probe", "engine": "ffmpeg"},
            {"id": "verify", "checks": ["size>1024"]},
            {"id": "publish", "storage": "out",
             "path": "{yyyy}/{mm}/{dd}/{asset.stem}_{ts}.mkv", "on_conflict": "version"},
        ],
    },
    "fetch_only": {
        "name": "fetch_only", "version": 1, "requires_gpu": False, "cpu_cost": 1,
        "steps": [
            {"id": "acquire", "mode": "staged", "resume": True},
            {"id": "probe", "engine": "ffmpeg"},
            {"id": "publish", "storage": "out", "path": "incoming/{asset.name}",
             "on_conflict": "skip"},
        ],
    },
}


def validate_spec(raw: dict[str, Any]) -> PresetSpec:
    return PresetSpec.from_db(raw)


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def apply_overrides(spec: dict, overrides: dict | None) -> dict:
    """Оверрайды шагов: {"transcode": {"video": {"bitrate": "10M"}}}."""
    if not overrides:
        return spec
    out = copy.deepcopy(spec)
    steps = {s["id"]: s for s in out.get("steps", [])}
    for step_id, patch in overrides.items():
        if step_id in steps:
            steps[step_id] = deep_merge(steps[step_id], patch)
        else:
            out.setdefault("meta_overrides", {})[step_id] = patch
    return out


def resolve_spec(preset: Preset, overrides: dict | None = None) -> PresetSpec:
    return validate_spec(apply_overrides(dict(preset.spec), overrides))


def snapshot(spec: PresetSpec) -> dict[str, Any]:
    """Снапшот в job.spec — ретрай не изменит результат при правке пресета."""
    return spec.to_db()


def spec_from_job(spec_json: dict | None) -> PresetSpec:
    if not spec_json or not spec_json.get("steps"):
        raise ConfigInvalidError("в задаче нет снапшота пресета (job.spec пуст)")
    return validate_spec(spec_json)


async def load_preset(
        session: AsyncSession, *, preset_id: uuid.UUID | None = None,
        name: str | None = None, version: int | None = None,
) -> Preset:
    if preset_id is not None:
        obj = await session.get(Preset, preset_id)
        if obj is None or not obj.enabled:
            raise ConfigInvalidError(f"пресет {preset_id} не найден/выключен")
        return obj
    if not name:
        raise ConfigInvalidError("нужен preset_id или name")
    if version is not None:
        sql = """
            SELECT * FROM preset WHERE name = :n AND enabled AND version = :v
            ORDER BY version DESC LIMIT 1
        """
        params = {"n": name, "v": version}
    else:
        sql = """
            SELECT * FROM preset WHERE name = :n AND enabled
            ORDER BY version DESC LIMIT 1
        """
        params = {"n": name}
    row = (await session.execute(text(sql), params)).mappings().first()
    if row is None:
        raise ConfigInvalidError(f"пресет {name!r} (v={version}) не найден")
    return await session.get(Preset, row["id"])  # type: ignore[return-value]


async def seed_presets(session: AsyncSession) -> int:
    created = 0
    for name, raw in BUILTIN_PRESETS.items():
        spec = validate_spec(raw)
        res = await session.execute(text("""
            INSERT INTO preset (name, version, description, spec, enabled)
            VALUES (:n, :v, :d, CAST(:s AS jsonb), true)
            ON CONFLICT (name, version) DO NOTHING
        """), {"n": name, "v": spec.version, "d": spec.description,
               "s": __import__("json").dumps(snapshot(spec))})
        created += res.rowcount or 0
    return created


def validate_against_hardware(spec: PresetSpec, hw: Hardware) -> list[str]:
    """Мягкая валидация для UI: предупреждения, не блокирующие сохранение."""
    warns: list[str] = []
    for step in spec.steps:
        if step.engine == "ffmbc":
            warns.append("ffmbc: только CPU, GPU не используется")
        if step.id == "transcode" and step.video and not step.video.copy:
            target = step.video.codec
            if target.endswith("_nvenc") and not hw.can_encode(target):
                warns.append(f"кодек {target} недоступен на этом воркере → fallback CPU")
            if step.hwaccel == "cuda" and not hw.cuda:
                warns.append("hwaccel=cuda, но CUDA недоступна")
        if step.scale and hw.cuda and not hw.cuda_filters:
            warns.append("нет CUDA-фильтров: масштабирование будет на CPU")
    if spec.requires_gpu and not hw.cuda:
        warns.append("пресет требует GPU — задача не будет взята CPU-воркером")
    return warns