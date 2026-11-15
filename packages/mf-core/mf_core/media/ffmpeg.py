from __future__ import annotations

import json
import re
from typing import Any

from mf_core.config import Settings, get_settings
from mf_core.errors import ConfigInvalidError
from mf_core.media.args import (
    audio_args, container_args, fit, input_resilience, loudnorm_filter, video_args,
)
from mf_core.media.hwaccel import Hardware, HwDecision
from mf_core.media.probe import MediaInfo
from mf_core.schemas import StepSpec, VideoSpec

_GLOBAL = ["-hide_banner", "-loglevel", "level+warning", "-progress", "pipe:1", "-nostats", "-y"]


def build_video_filters(
        step: StepSpec, info: MediaInfo, dec: HwDecision,
) -> list[str]:
    v = info.video
    if v is None:
        return []
    chain: list[str] = []
    cpu_part: list[str] = []

    need_deint = step.deinterlace == "force" or (step.deinterlace == "auto" and v.interlaced)
    if need_deint:
        if dec.use_cuda and dec.deint_filter.endswith("_cuda") and dec.decode_cuda:
            chain.append(f"{dec.deint_filter}=mode=1")
        else:
            cpu_part.append("bwdif=mode=1")

    if step.scale:
        w, h, crop = fit(v.width, v.height, step.scale)
        if (w, h) != (v.width, v.height):
            if dec.use_cuda and dec.decode_cuda and step.scale.hw != "cpu":
                fmt = "p010le" if (v.pix_fmt or "").startswith(("p010", "yuv420p10")) else "nv12"
                if dec.scale_filter == "scale_npp":
                    chain.append(f"scale_npp={w}:{h}:format={fmt}")
                else:
                    chain.append(f"scale_cuda={w}:{h}:format={fmt}")
            else:
                cpu_part.append(f"scale={w}:{h}:flags=lanczos")
        if crop:
            cpu_part.append(f"crop={crop[0]}:{crop[1]}")

    if cpu_part:
        if dec.use_cuda and dec.decode_cuda:
            chain += ["hwdownload", "format=nv12", *cpu_part, "hwupload_cuda"]
        else:
            chain += cpu_part
            if dec.use_cuda:                      # CPU-декод → GPU-энкод
                chain.append("hwupload_cuda")
    if not dec.use_cuda:
        chain.append(f"format={(step.video or VideoSpec()).pix_fmt}")
    return chain


def build_transcode(
        step: StepSpec, info: MediaInfo, hw: Hardware, *, src: str, dst: str,
        decision: HwDecision | None = None, loudnorm: dict | None = None,
        interactive: bool = False, settings: Settings | None = None,
) -> list[str]:
    """interactive=True → без -nostdin, чтобы run_media мог мягко остановить по 'q'."""
    s = settings or get_settings()
    dec = decision or hw.decide(step, info)
    video = step.video or VideoSpec()

    cmd = [s.ffmpeg_path, *_GLOBAL]
    if not interactive:
        cmd.append("-nostdin")
    if dec.use_cuda and dec.decode_cuda:
        cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
                "-hwaccel_device", str(dec.gpu_index)]
    cmd += input_resilience(src, s) + list(step.extra_in) + ["-i", src]

    if video.copy:
        cmd += ["-map", "0:v:0", "-c:v", "copy"]
    else:
        cmd += ["-map", "0:v:0"]
        vf = build_video_filters(step, info, dec)
        if vf:
            cmd += ["-vf", ",".join(vf)]
        cmd += video_args(video, cuda=dec.use_cuda, gpu_index=dec.gpu_index,
                          threads=s.media_threads)

    cmd += audio_args(step.audio, loudnorm)
    cmd += ["-map_metadata", "0", "-map_chapters", "0"]
    cmd += container_args(step.container, dst) + list(step.extra_out) + [dst]
    return cmd


def build_loudnorm_pass1(
        info: MediaInfo, *, src: str, settings: Settings | None = None,
) -> list[str]:
    s = settings or get_settings()
    return [
        s.ffmpeg_path, "-hide_banner", "-nostdin", "-v", "info",
        *input_resilience(src, s), "-i", src, "-map", "0:a:0",
        "-af", loudnorm_filter(None), "-f", "null", "-",
    ]


_LOUDNORM_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)


def parse_loudnorm(stderr: str) -> dict[str, Any]:
    m = _LOUDNORM_JSON.findall(stderr)
    if not m:
        raise ConfigInvalidError("loudnorm pass1 не вернул измерений")
    return json.loads(m[-1])


def build_thumbnail(
        step: StepSpec, info: MediaInfo, *, src: str, dst: str, at_s: float,
        width: int = 640, settings: Settings | None = None,
) -> list[str]:
    s = settings or get_settings()
    h = -2
    if info.video and info.video.width:
        h = max(2, int(round(width * info.video.height / info.video.width / 2)) * 2)
    return [
        s.ffmpeg_path, "-hide_banner", "-nostdin", "-v", "error", "-y",
        *input_resilience(src, s), "-ss", f"{max(0.0, at_s):.3f}", "-i", src,
        "-map", "0:v:0", "-frames:v", "1",
        "-vf", f"scale={width}:{h}:flags=lanczos", "-q:v", "3", dst,
    ]


def build_capture(
        *, src: str, dst: str, duration_s: int | None = None, copy: bool = True,
        segment_s: int | None = None, settings: Settings | None = None,
) -> list[str]:
    """Захват live-потока: stream copy по умолчанию (без перекодирования)."""
    s = settings or get_settings()
    cmd = [s.ffmpeg_path, *_GLOBAL, "-nostdin", *input_resilience(src, s), "-i", src]
    cmd += ["-map", "0", "-c", "copy"] if copy else ["-map", "0:v:0", "-map", "0:a?"]
    if duration_s:
        cmd += ["-t", str(duration_s)]
    if segment_s:
        cmd += ["-f", "segment", "-segment_time", str(segment_s),
                "-reset_timestamps", "1", "-strftime", "1"]
    cmd += [dst]
    return cmd