from __future__ import annotations

from mf_core.config import Settings, get_settings
from mf_core.errors import ConfigInvalidError
from mf_core.media.probe import NETWORK_SCHEMES
from mf_core.schemas import AudioSpec, ScaleSpec, VideoSpec

X264_PRESET_MAP = {
    "p1": "veryfast", "p2": "faster", "p3": "fast", "p4": "medium",
    "p5": "medium", "p6": "slow", "p7": "veryslow",
}


def even(n: float) -> int:
    return max(2, int(round(n / 2)) * 2)


def fit(src_w: int, src_h: int, spec: ScaleSpec) -> tuple[int, int, tuple[int, int] | None]:
    """→ (w, h, crop|None). mode=fit — вписать; fill — покрыть с кропом; exact — как задано."""
    if not src_w or not src_h:
        raise ConfigInvalidError("не удалось определить размеры источника")
    tw, th = spec.width, spec.height
    if spec.mode == "exact":
        return even(tw or src_w), even(th or src_h), None
    if not spec.keep_aspect:
        return even(tw or src_w), even(th or src_h), None
    if tw and th:
        r_fit = min(tw / src_w, th / src_h)
        r_fill = max(tw / src_w, th / src_h)
        r = r_fill if spec.mode == "fill" else r_fit
        w, h = even(src_w * r), even(src_h * r)
        crop = (even(tw), even(th)) if spec.mode == "fill" and (w > tw or h > th) else None
        return w, h, crop
    if th:
        return even(src_w * th / src_h), even(th), None
    return even(tw), even(src_h * tw / src_w), None  # type: ignore[arg-type]


def input_resilience(uri: str, s: Settings | None = None) -> list[str]:
    s = s or get_settings()
    if "://" not in uri:
        return []
    scheme = uri.split("://", 1)[0].lower()
    if scheme in ("http", "https", "hls"):
        return [
            "-reconnect", "1", "-reconnect_streaming", "1", "-reconnect_delay_max", "5",
            "-reconnect_on_network_error", "1", "-rw_timeout", "15000000",
            "-analyzeduration", str(s.probe_analyze_s * 1_000_000),
            "-probesize", str(s.probe_analyze_s * 1_000_000),
        ]
    if scheme in ("rtsp", "rtspS".lower()):
        return ["-rtsp_transport", "tcp", "-timeout", "15000000"]
    if scheme in ("rtmp", "rtmps", "tcp", "ftp", "sftp"):
        return ["-rw_timeout", "15000000"]
    if scheme == "srt":
        return ["-latency", "200000", "-rw_timeout", "15000000"]
    if scheme in NETWORK_SCHEMES:
        return ["-rw_timeout", "15000000"]
    return []


def rate_args(v: VideoSpec, cuda: bool) -> list[str]:
    out: list[str] = []
    if v.rc == "constqp":
        out += ["-rc", "constqp", "-qp", str(v.cq or 23)] if cuda else ["-qp", str(v.cq or 23)]
        return out
    if v.rc == "cq":
        if cuda:
            out += ["-rc", "vbr", "-cq", str(v.cq or 23), "-b:v", "0"]
        else:
            out += ["-crf", str(v.cq or 23)]
        return out
    if cuda:
        out += ["-rc", "vbr" if v.rc == "vbr" else "cbr"]
    if v.bitrate:
        out += ["-b:v", v.bitrate]
    if v.maxrate:
        out += ["-maxrate", v.maxrate]
    if v.bufsize:
        out += ["-bufsize", v.bufsize]
    if v.cq is not None and cuda and v.rc == "vbr":
        out += ["-cq", str(v.cq)]
    return out


def video_args(v: VideoSpec, *, cuda: bool, gpu_index: int = 0, threads: int = 0) -> list[str]:
    if cuda:
        codec = v.codec if v.codec.endswith("_nvenc") else "h264_nvenc"
        out = ["-c:v", codec, "-preset", v.preset or "p5", "-tune", "hq",
               "-gpu", str(gpu_index)]
        out += rate_args(v, cuda=True)
        if v.profile:
            out += ["-profile:v", v.profile]
        if v.gop:
            out += ["-g", str(v.gop)]
        if v.bframes is not None and codec != "av1_nvenc":
            out += ["-bf", str(v.bframes)]
        return out

    codec = v.cpu_codec
    out = ["-c:v", codec]
    if codec in ("libx264", "libx265"):
        out += ["-preset", X264_PRESET_MAP.get(v.preset or "p4", "medium")]
    out += ["-pix_fmt", v.pix_fmt]
    out += rate_args(v, cuda=False)
    if v.profile:
        out += ["-profile:v", v.profile]
    if v.gop:
        out += ["-g", str(v.gop)]
    if v.bframes is not None:
        out += ["-bf", str(v.bframes)]
    if threads:
        out += ["-threads", str(threads)]
    return out


def loudnorm_filter(measured: dict | None = None, target_i: float = -23.0) -> str:
    base = f"loudnorm=I={target_i}:LRA=7:TP=-2"
    if not measured:
        return base + ":print_format=json"
    return (
        f"{base}:measured_I={measured['input_i']}:measured_LRA={measured['input_lra']}"
        f":measured_TP={measured['input_tp']}:measured_thresh={measured['input_thresh']}"
        f":offset={measured.get('target_offset', 0)}:linear=true:print_format=summary"
    )


def audio_args(specs: list[AudioSpec], measured: dict | None = None) -> list[str]:
    if not specs:
        return ["-map", "0:a?", "-c:a", "copy"]
    out: list[str] = []
    for i, a in enumerate(specs):
        out += ["-map", a.map]
        if a.copy:
            out += [f"-c:a:{i}", "copy"]
            continue
        out += [f"-c:a:{i}", a.codec, f"-b:a:{i}", a.bitrate]
        if a.channels:
            out += [f"-ac:a:{i}", str(a.channels)]
        if a.sample_rate:
            out += [f"-ar:a:{i}", str(a.sample_rate)]
        if a.loudnorm == "ebu_r128" and measured is not None:
            out += [f"-filter:a:{i}", loudnorm_filter(measured)]
    return out


def container_args(container: str | None, dst: str) -> list[str]:
    if not container:
        return []
    c = container.lower()
    fmt = {"mp4": "mp4", "mov": "mov", "mkv": "matroska", "mxf": "mxf",
           "mxf_d10": "mxf_d10", "ts": "mpegts", "webm": "webm", "mp3": "mp3",
           "wav": "wav", "flac": "flac", "hls": "hls"}.get(c, c)
    return ["-f", fmt]