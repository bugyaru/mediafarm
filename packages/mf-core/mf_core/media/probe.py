from __future__ import annotations

import asyncio
import json
import os
import re
import signal
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from mf_core.config import Settings, get_settings
from mf_core.errors import ConfigInvalidError, TransientNetworkError, classify_ffmpeg

NETWORK_SCHEMES = ("http", "https", "rtmp", "rtmps", "hls", "ftp", "sftp", "tcp", "srt")
LogCB = Callable[[str, str], None]

def _ratio(v: str | None) -> float | None:
    if not v or v in ("N/A", "0/0", "0:0"):
        return None
    try:
        if "/" in v:
            num, den = v.split("/", 1)
            d = float(den)
            return round(float(num) / d, 6) if d else None
        return float(v)
    except (ValueError, ZeroDivisionError):
        return None

def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

def _float(v: Any) -> float | None:
    try:
        f = float(v)
        return None if f != f else f  # NaN
    except (TypeError, ValueError):
        return None

@dataclass(frozen=True, slots=True)
class VideoStream:
    index: int
    codec: str
    width: int
    height: int
    profile: str | None = None
    pix_fmt: str | None = None
    fps: float | None = None
    bitrate: int | None = None
    duration: float | None = None
    field_order: str | None = None
    interlaced: bool = False
    color_space: str | None = None
    color_transfer: str | None = None
    color_primaries: str | None = None
    hdr: bool = False
    rotation: int = 0
    sar: str | None = None
    dar: str | None = None
    nb_frames: int | None = None
    attached_pic: bool = False

    @property
    def resolution(self) -> tuple[int, int]:
        return self.width, self.height

@dataclass(frozen=True, slots=True)
class AudioStream:
    index: int
    codec: str
    channels: int | None = None
    channel_layout: str | None = None
    sample_rate: int | None = None
    bitrate: int | None = None
    duration: float | None = None
    language: str | None = None
    default: bool = False

@dataclass(frozen=True, slots=True)
class MediaInfo:
    uri: str
    format_name: str = ""
    duration: float | None = None
    size: int | None = None
    bitrate: int | None = None
    video: VideoStream | None = None
    audio: tuple[AudioStream, ...] = ()
    subtitle_indexes: tuple[int, ...] = ()
    tags: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def has_video(self) -> bool:
        return self.video is not None and not self.video.attached_pic

    @property
    def has_audio(self) -> bool:
        return bool(self.audio)

    @property
    def video_codec(self) -> str | None:
        return self.video.codec if self.video else None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)
        return d

    @classmethod
    def from_json(cls, uri: str, data: dict[str, Any]) -> MediaInfo:
        fmt = data.get("format") or {}
        streams = data.get("streams") or []
        video: VideoStream | None = None
        audio: list[AudioStream] = []
        subs: list[int] = []

        for st in streams:
            kind = st.get("codec_type")
            idx = int(st.get("index", 0))
            disp = st.get("disposition") or {}
            tags = {k.lower(): str(v) for k, v in (st.get("tags") or {}).items()}
            if kind == "video" and video is None:
                fo = (st.get("field_order") or "").lower()
                rotation = 0
                for sd in st.get("side_data_list") or []:
                    if "rotation" in sd:
                        rotation = int(_float(sd["rotation"]) or 0)
                rotation = rotation or int(_float(tags.get("rotate")) or 0)
                transfer = (st.get("color_transfer") or "").lower()
                video = VideoStream(
                    index=idx, codec=st.get("codec_name") or "",
                    width=int(st.get("width") or 0), height=int(st.get("height") or 0),
                    profile=st.get("profile"), pix_fmt=st.get("pix_fmt"),
                    fps=_ratio(st.get("avg_frame_rate")) or _ratio(st.get("r_frame_rate")),
                    bitrate=_int(st.get("bit_rate")), duration=_float(st.get("duration")),
                    field_order=fo or None,
                    interlaced=fo in ("tt", "bb", "tb", "bt"),
                    color_space=st.get("color_space"), color_transfer=transfer or None,
                    color_primaries=st.get("color_primaries"),
                    hdr=transfer in ("smpte2084", "arib-std-b67"),
                    rotation=rotation % 360,
                    sar=st.get("sample_aspect_ratio"), dar=st.get("display_aspect_ratio"),
                    nb_frames=_int(st.get("nb_frames")),
                    attached_pic=bool(disp.get("attached_pic")),
                )
            elif kind == "audio":
                audio.append(AudioStream(
                    index=idx, codec=st.get("codec_name") or "",
                    channels=_int(st.get("channels")),
                    channel_layout=st.get("channel_layout"),
                    sample_rate=_int(st.get("sample_rate")),
                    bitrate=_int(st.get("bit_rate")), duration=_float(st.get("duration")),
                    language=tags.get("language"), default=bool(disp.get("default")),
                ))
            elif kind == "subtitle":
                subs.append(idx)

        return cls(
            uri=uri, format_name=fmt.get("format_name") or "",
            duration=_float(fmt.get("duration")), size=_int(fmt.get("size")),
            bitrate=_int(fmt.get("bit_rate")), video=video, audio=tuple(audio),
            subtitle_indexes=tuple(subs),
            tags={k.lower(): str(v) for k, v in (fmt.get("tags") or {}).items()},
            raw=data,
        )

def probe_input_args(uri: str, s: Settings | None = None) -> list[str]:
    s = s or get_settings()
    if "://" not in uri:
        return []
    scheme = uri.split("://", 1)[0].lower()
    if scheme not in NETWORK_SCHEMES:
        return []
    return [
        "-rw_timeout", str(15_000_000),
        "-analyzeduration", str(s.probe_analyze_s * 1_000_000),
        "-probesize", str(s.probe_analyze_s * 1_000_000),
    ]

def _dump_log(on_log: LogCB | None, stream: str, payload: bytes | str) -> None:
    """Отдаёт сырой вывод инструмента в полный лог задачи."""
    if on_log is None:
        return
    text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload
    for line in text.splitlines():
        clean = line.rstrip("\r\n")
        try:
            on_log(stream, clean)
        except Exception:  # noqa: BLE001
            return

async def ffprobe(
        uri: str, *, settings: Settings | None = None, extra: list[str] | None = None,
        timeout_s: int | None = None, on_log: LogCB | None = None,
) -> MediaInfo:
    s = settings or get_settings()
    cmd = [s.ffprobe_path, "-hide_banner", "-loglevel", f"level+{s.media_loglevel}",
           "-print_format", "json", "-show_format", "-show_streams"]
    cmd += probe_input_args(uri, s) + (extra or []) + [uri]
    _dump_log(on_log, "meta", "$ " + " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s or s.probe_timeout_s)
    except TimeoutError:
        _kill(proc)
        _dump_log(on_log, "stderr", f"ffprobe timeout {timeout_s or s.probe_timeout_s}s: {uri}")
        raise TransientNetworkError(f"ffprobe timeout {uri}", uri=uri) from None
    _dump_log(on_log, "stderr", err or b"")
    if proc.returncode != 0:
        raise classify_ffmpeg(proc.returncode or 1, err.decode("utf-8", "replace").splitlines())
    try:
        data = json.loads(out or b"{}")
    except json.JSONDecodeError as e:
        raise ConfigInvalidError(f"ffprobe вернул не-JSON для {uri}: {e}") from e
    return MediaInfo.from_json(uri, data)

def _kill(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()

_IDET = re.compile(
    r"Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*"
    r"Progressive:\s*(\d+)\s*Undetermined:\s*(\d+)"
)

async def detect_interlace(
        uri: str, *, frames: int = 250, settings: Settings | None = None,
        on_log: LogCB | None = None,
) -> dict[str, Any]:
    """idet — дороже, чем field_order из контейнера, но ловит telecine/неверные флаги."""
    s = settings or get_settings()
    cmd = [s.ffmpeg_path, "-hide_banner", "-nostdin", "-v", "info",
           *probe_input_args(uri, s), "-i", uri,
           "-vf", "idet", "-frames:v", str(frames), "-an", "-f", "null", "-"]
    _dump_log(on_log, "meta", "$ " + " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), s.probe_timeout_s + frames / 10)
    except TimeoutError:
        _kill(proc)
        _dump_log(on_log, "stderr", f"idet timeout: {uri}")
        raise TransientNetworkError(f"idet timeout {uri}") from None
    _dump_log(on_log, "stderr", err or b"")
    matches = _IDET.findall(err.decode("utf-8", "replace"))
    if not matches:
        return {"interlaced": None, "field_order": None, "confidence": 0.0}
    tff, bff, prog, und = (int(x) for x in matches[-1])
    total = max(1, tff + bff + prog)
    inter = tff + bff
    return {
        "tff": tff, "bff": bff, "progressive": prog, "undetermined": und,
        "interlaced": inter > prog,
        "field_order": "tt" if tff >= bff else "bb",
        "confidence": round(max(inter, prog) / total, 3),
    }