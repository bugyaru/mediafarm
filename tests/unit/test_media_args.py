import json
import os
from pathlib import Path

import pytest

from mf_core.errors import ConfigInvalidError
from mf_core.media.args import fit
from mf_core.media.ffmbc import build_imx
from mf_core.media.ffmpeg import build_thumbnail, build_transcode, build_video_filters
from mf_core.media.hwaccel import Hardware, HwDecision
from mf_core.media.probe import MediaInfo
from mf_core.schemas import ScaleSpec, StepSpec

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "ffmpeg_cuda_h264.json"

HW = Hardware(cuda=True, encoders={"h264_nvenc", "hevc_nvenc", "av1_nvenc", "libx264"},
              decoders={"h264_cuvid", "hevc_cuvid"},
              filters={"scale_cuda", "bwdif_cuda", "hwupload_cuda", "scale"},
              hwaccels={"cuda"}, session_limit=0)

UHD = MediaInfo.from_json("src.mp4", {"format": {"duration": "60"}, "streams": [
    {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 3840, "height": 2160,
     "field_order": "progressive", "pix_fmt": "yuv420p", "avg_frame_rate": "25/1"},
    {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2}]})

STEP = StepSpec.model_validate({
    "id": "transcode", "engine": "ffmpeg", "hwaccel": "auto", "deinterlace": "auto",
    "video": {"codec": "h264_nvenc", "cpu_codec": "libx264", "rc": "vbr", "bitrate": "6M",
              "maxrate": "8M", "bufsize": "12M", "preset": "p5", "profile": "high",
              "gop": 50, "bframes": 2},
    "scale": {"height": 1080, "mode": "fit"},
    "audio": [{"codec": "aac", "bitrate": "192k", "channels": 2, "sample_rate": 48000}],
    "container": "mp4", "extra_out": ["-movflags", "+faststart"],
})


def test_fit_modes():
    assert fit(3840, 2160, ScaleSpec(height=1080)) == (1920, 1080, None)
    assert fit(1920, 1080, ScaleSpec(width=1280, height=1280, mode="fit")) == (1280, 720, None)
    w, h, crop = fit(1920, 1080, ScaleSpec(width=1000, height=1000, mode="fill"))
    assert (w, h) == (1778, 1000) and crop == (1000, 1000)
    assert fit(1920, 1080, ScaleSpec(width=641, height=481, mode="exact")) == (642, 482, None)


def test_hwaccel_before_input_and_gpu_filter():
    cmd = build_transcode(STEP, UHD, HW, src="src.mp4", dst="out.mp4")
    assert cmd.index("-hwaccel") < cmd.index("-i")
    assert "-hwaccel_output_format" in cmd and "cuda" in cmd
    vf = cmd[cmd.index("-vf") + 1]
    assert vf == "scale_cuda=1920:1080:format=nv12"
    assert "-nostdin" in cmd


def test_cpu_fallback_has_no_cuda():
    cmd = build_transcode(STEP, UHD, HW, src="src.mp4", dst="out.mp4",
                          decision=HwDecision(False, "forced_cpu"))
    assert "-hwaccel" not in cmd
    assert "libx264" in cmd and "-pix_fmt" in cmd
    assert "scale=1920:1080:flags=lanczos" in cmd[cmd.index("-vf") + 1]


def test_interlaced_uses_bwdif_cuda():
    src = MediaInfo.from_json("s.mxf", {"streams": [
        {"index": 0, "codec_type": "video", "codec_name": "mpeg2video", "width": 720,
         "height": 608, "field_order": "tt"}]})
    dec = HwDecision(True, gpu_index=0, decode_cuda=False, scale_filter="scale_cuda",
                     deint_filter="bwdif_cuda", upload_from_cpu=True)
    chain = build_video_filters(STEP, src, dec)
    assert "bwdif=mode=1" in chain and chain[-1] == "hwupload_cuda"


def test_stream_copy_skips_filters():
    step = STEP.model_copy(update={"video": STEP.video.model_copy(update={"copy": True})})
    cmd = build_transcode(step, UHD, HW, src="s.mp4", dst="o.mp4")
    assert "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "copy"
    assert "-vf" not in cmd and "-hwaccel" not in cmd


def test_network_input_resilience():
    cmd = build_transcode(STEP, UHD, HW, src="https://x/y.m3u8", dst="o.mp4")
    assert "-reconnect" in cmd and "-rw_timeout" in cmd
    assert cmd.index("-reconnect") < cmd.index("-i")


def test_interactive_drops_nostdin():
    cmd = build_transcode(STEP, UHD, HW, src="s.mp4", dst="o.mp4", interactive=True)
    assert "-nostdin" not in cmd


def test_av1_omits_bframes():
    step = STEP.model_copy(update={"video": STEP.video.model_copy(update={"codec": "av1_nvenc"})})
    cmd = build_transcode(step, UHD, HW, src="s.mp4", dst="o.mkv")
    assert "av1_nvenc" in cmd and "-bf" not in cmd


def test_thumbnail():
    cmd = build_thumbnail(STEP, UHD, src="s.mp4", dst="t.jpg", at_s=12.5)
    assert cmd[cmd.index("-ss") + 1] == "12.500" and "-frames:v" in cmd


def test_imx_pal_and_ntsc():
    step = StepSpec.model_validate({"id": "transcode", "engine": "ffmbc", "container": "mxf",
                                    "video": {"codec": "mpeg2video"}})
    cmd = build_imx(step, UHD, src="in.mxf", dst="out.mxf", engine="ffmbc",
                    standard="pal", bitrate_mbps=50)
    assert cmd[0].endswith("ffmbc")
    for token in ("720x608", "25", "yuv422p", "+ildct+ilme", "50M", "pcm_s16le", "mxf"):
        assert token in cmd
    assert "-minrate" in cmd and cmd[cmd.index("-minrate") + 1] == "50M"
    ntsc = build_imx(step, UHD, src="i", dst="o", engine="ffmpeg", standard="ntsc",
                     bitrate_mbps=30)
    assert "720x512" in ntsc and "mxf_d10" in ntsc


def test_imx_rejects_invalid_bitrate():
    step = StepSpec.model_validate({"id": "transcode", "engine": "ffmbc",
                                    "video": {"codec": "mpeg2video"}})
    with pytest.raises(ConfigInvalidError):
        build_imx(step, UHD, src="i", dst="o", bitrate_mbps=25)


def test_golden_cuda_command():
    cmd = build_transcode(STEP, UHD, HW, src="src.mp4", dst="out.mp4")
    if os.getenv("MF_UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(cmd, indent=2, ensure_ascii=False) + "\n")
        pytest.skip("golden обновлён")
    assert cmd == json.loads(GOLDEN.read_text()), (
        "расхождение с golden; если изменение намеренное — MF_UPDATE_GOLDEN=1 pytest"
    )