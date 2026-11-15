"""IMX / D-10 MXF OP1a. ffmbc не использует GPU — только CPU-воркеры.

Стандарты: PAL 720x608@25, NTSC 720x512@29.97; битрейты 30/40/50 Мбит/с, I-frame only.
Современный ffmpeg пишет то же через muxer mxf_d10 (engine="ffmpeg").
"""
from __future__ import annotations

from mf_core.config import Settings, get_settings
from mf_core.errors import ConfigInvalidError
from mf_core.media.args import input_resilience
from mf_core.media.probe import MediaInfo
from mf_core.schemas import StepSpec

STANDARDS = {
    "pal": {"size": "720x608", "rate": "25", "bufsize": "2000k"},
    "ntsc": {"size": "720x512", "rate": "30000/1001", "bufsize": "1668k"},
}
VALID_BITRATES = (30, 40, 50)


def build_imx(
        step: StepSpec, info: MediaInfo, *, src: str, dst: str, engine: str = "ffmbc",
        standard: str = "pal", bitrate_mbps: int = 50, audio_channels: int = 2,
        settings: Settings | None = None,
) -> list[str]:
    s = settings or get_settings()
    if engine == "ffmbc" and not s.ffmbc_enabled:
        raise ConfigInvalidError("ffmbc выключен (MF_FFMBC_ENABLED=false)")
    if engine not in ("ffmbc", "ffmpeg"):
        raise ConfigInvalidError(f"IMX: неподдерживаемый engine {engine!r}")
    if bitrate_mbps not in VALID_BITRATES:
        raise ConfigInvalidError(f"D-10: битрейт {bitrate_mbps} не из {VALID_BITRATES}")
    if standard not in STANDARDS:
        raise ConfigInvalidError(f"неизвестный стандарт {standard!r}")
    if audio_channels not in (2, 4, 8):
        raise ConfigInvalidError("D-10: допустимо 2/4/8 аудиоканалов")

    std = STANDARDS[standard]
    binary = s.ffmbc_path if engine == "ffmbc" else s.ffmpeg_path
    muxer = "mxf" if engine == "ffmbc" else "mxf_d10"
    br = f"{bitrate_mbps}M"

    cmd = [binary, "-hide_banner", "-nostdin", "-v", "level+warning",
           "-progress", "pipe:1", "-nostats", "-y"]
    cmd += input_resilience(src, s) + list(step.extra_in) + ["-i", src]
    cmd += [
        "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "mpeg2video", "-pix_fmt", "yuv422p", "-profile:v", "0",
        "-intra", "-flags", "+ildct+ilme", "-top", "1",
        "-s", std["size"], "-r", std["rate"], "-aspect", "4:3",
        "-b:v", br, "-minrate", br, "-maxrate", br, "-bufsize", std["bufsize"],
        "-g", "1", "-qmin", "1", "-qmax", "12", "-lmin", "0", "-lmax", "0",
        "-c:a", "pcm_s16le", "-ar", "48000", "-ac", str(audio_channels),
        "-f", muxer,
    ]
    cmd += list(step.extra_out) + [dst]
    return cmd


def build_ffmbc_generic(
        step: StepSpec, info: MediaInfo, *, src: str, dst: str, settings: Settings | None = None,
) -> list[str]:
    """Не-IMX пресеты на ffmbc (legacy broadcast). GPU не используется никогда."""
    s = settings or get_settings()
    if not s.ffmbc_enabled:
        raise ConfigInvalidError("ffmbc выключен")
    v = step.video
    cmd = [s.ffmbc_path, "-hide_banner", "-nostdin", "-v", "level+warning",
           "-progress", "pipe:1", "-nostats", "-y"]
    cmd += input_resilience(src, s) + list(step.extra_in) + ["-i", src, "-map", "0:v:0"]
    if v and v.copy:
        cmd += ["-c:v", "copy"]
    else:
        cmd += ["-c:v", (v.codec if v else "mpeg2video"), "-pix_fmt",
                (v.pix_fmt if v else "yuv422p")]
        if v and v.bitrate:
            cmd += ["-b:v", v.bitrate]
        if v and v.gop:
            cmd += ["-g", str(v.gop)]
    from mf_core.media.args import audio_args, container_args
    cmd += audio_args(step.audio) + container_args(step.container, dst)
    cmd += list(step.extra_out) + [dst]
    return cmd