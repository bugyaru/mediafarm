"""Реальный ffmpeg на синтетике lavfi. GPU-тесты — только на ноде с L40 (-m gpu)."""
from __future__ import annotations

import shutil

import pytest

from mf_core.config import get_settings
from mf_core.media.ffmpeg import build_transcode
from mf_core.media.hwaccel import Hardware, detect_hardware
from mf_core.media.probe import ffprobe
from mf_core.media.progress import run_media
from mf_core.media.verify import verify_output
from mf_core.schemas import StepSpec

pytestmark = pytest.mark.integration

requires_ffmpeg = pytest.mark.skipif(
    not shutil.which("ffmpeg"), reason="ffmpeg не установлен"
)


async def _make_src(path, *, seconds=6, size="1280x720", rate=25):
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    ]
    p = await __import__("asyncio").create_subprocess_exec(*cmd)
    assert await p.wait() == 0


CPU_STEP = StepSpec.model_validate({
    "id": "transcode", "engine": "ffmpeg", "hwaccel": "cpu", "container": "mp4",
    "video": {"codec": "libx264", "cpu_codec": "libx264", "rc": "cq", "cq": 28,
              "preset": "p1", "gop": 50},
    "scale": {"height": 360, "mode": "fit"},
    "audio": [{"codec": "aac", "bitrate": "96k", "channels": 2}],
    "extra_out": ["-movflags", "+faststart"],
})


@requires_ffmpeg
async def test_cpu_transcode_and_verify(tmp_path):
    src, dst = tmp_path / "src.mp4", tmp_path / "out.mp4"
    await _make_src(src)
    info = await ffprobe(str(src))
    assert info.has_video and info.has_audio and info.duration >= 5

    cmd = build_transcode(CPU_STEP, info, Hardware(cuda=False), src=str(src), dst=str(dst))
    seen = []
    res = await run_media(cmd, duration_s=info.duration,
                          on_progress=lambda p: seen.append(p), progress_interval_s=0.0)
    assert res.returncode == 0 and dst.exists()
    assert seen and max(p["pct"] or 0 for p in seen) >= 99

    report = await verify_output(str(dst), checks=["has_video", "has_audio",
                                                   "duration_delta<=1.0", "height==360"],
                                 src_info=info, local_path=str(dst))
    assert report["metrics"]["height"] == 360


@requires_ffmpeg
async def test_interlace_detection_runs(tmp_path):
    from mf_core.media.probe import detect_interlace
    src = tmp_path / "i.mp4"
    await _make_src(src, seconds=3)
    res = await detect_interlace(str(src), frames=50)
    assert res["interlaced"] is False and res["confidence"] > 0.5


@requires_ffmpeg
@pytest.mark.gpu
async def test_l40_nvenc_transcode(tmp_path):
    s = get_settings()
    hw = await detect_hardware(s)
    if not hw.cuda:
        pytest.skip("CUDA/NVENC недоступны")
    assert "h264_nvenc" in hw.nvenc

    src, dst = tmp_path / "src.mp4", tmp_path / "nvenc.mp4"
    await _make_src(src, seconds=8, size="1920x1080")
    info = await ffprobe(str(src))
    step = StepSpec.model_validate({
        "id": "transcode", "engine": "ffmpeg", "hwaccel": "auto", "container": "mp4",
        "video": {"codec": "h264_nvenc", "rc": "vbr", "bitrate": "4M", "preset": "p5",
                  "gop": 50},
        "scale": {"height": 720, "mode": "fit"},
        "audio": [{"codec": "aac", "bitrate": "128k"}],
    })
    dec = hw.decide(step, info)
    assert dec.use_cuda, dec.reason
    cmd = build_transcode(step, info, hw, src=str(src), dst=str(dst), decision=dec)
    res = await run_media(cmd, duration_s=info.duration, codec="h264_nvenc")
    assert res.returncode == 0
    out = await ffprobe(str(dst))
    assert out.video.codec == "h264" and out.video.height == 720


@requires_ffmpeg
@pytest.mark.gpu
async def test_av1_nvenc_on_ada(tmp_path):
    hw = await detect_hardware()
    if "av1_nvenc" not in hw.nvenc:
        pytest.skip("av1_nvenc недоступен (нужна Ada/L40 и свежий ffmpeg)")
    src, dst = tmp_path / "s.mp4", tmp_path / "a.mkv"
    await _make_src(src, seconds=4)
    info = await ffprobe(str(src))
    step = StepSpec.model_validate({
        "id": "transcode", "engine": "ffmpeg", "hwaccel": "cuda", "container": "mkv",
        "video": {"codec": "av1_nvenc", "rc": "vbr", "bitrate": "2M", "preset": "p5"},
        "audio": [{"codec": "libopus", "bitrate": "96k"}],
    })
    cmd = build_transcode(step, info, hw, src=str(src), dst=str(dst))
    assert (await run_media(cmd, duration_s=info.duration, codec="av1_nvenc")).returncode == 0
    assert (await ffprobe(str(dst))).video.codec == "av1"