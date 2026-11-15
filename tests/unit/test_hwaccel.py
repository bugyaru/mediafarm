from mf_core.media.hwaccel import Hardware, parse_codecs, parse_filters, parse_hwaccels
from mf_core.media.probe import MediaInfo
from mf_core.schemas import StepSpec

ENCODERS = """
Encoders:
 V..... = Video
 ------
 V..... libx264              libx264 H.264
 V..... h264_nvenc           NVIDIA NVENC H.264
 V..... hevc_nvenc           NVIDIA NVENC hevc
 V..... av1_nvenc            NVIDIA NVENC av1
 A..... aac                  AAC
"""
FILTERS = """
Filters:
 ... scale              V->V       Scale the input video size
 ... scale_cuda         V->V       GPU based video scaler
 ... bwdif_cuda         V->V       Deinterlace CUDA
 ... hwupload_cuda      V->V       Upload to CUDA
"""
HWACCELS = "Hardware acceleration methods:\ncuda\nvaapi\n"

L40 = Hardware(
    cuda=True, encoders=parse_codecs(ENCODERS), decoders={"h264_cuvid", "hevc_cuvid"},
    filters=parse_filters(FILTERS), hwaccels=parse_hwaccels(HWACCELS),
    gpus=[], session_limit=0, min_free_mb=2000,
)

SRC = MediaInfo.from_json("s.mp4", {"format": {"duration": "60"}, "streams": [
    {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 3840, "height": 2160,
     "field_order": "progressive", "pix_fmt": "yuv420p"}]})


def _step(**kw):
    base = {"id": "transcode", "engine": "ffmpeg",
            "video": {"codec": "h264_nvenc", "cpu_codec": "libx264"}}
    base.update(kw)
    return StepSpec.model_validate(base)


def test_parsers():
    assert {"h264_nvenc", "libx264", "aac"} <= parse_codecs(ENCODERS)
    assert "scale_cuda" in parse_filters(FILTERS)
    assert parse_hwaccels(HWACCELS) == {"cuda", "vaapi"}


def test_l40_uses_cuda_and_scale_cuda():
    d = L40.decide(_step(), SRC)
    assert d.use_cuda and d.decode_cuda and d.scale_filter == "scale_cuda"
    assert d.deint_filter == "bwdif_cuda"


def test_npp_preferred_when_present():
    hw = Hardware(cuda=True, encoders=L40.encoders, decoders=L40.decoders,
                  filters=L40.filters | {"scale_npp"}, hwaccels={"cuda"})
    assert hw.scale_filter() == "scale_npp"


def test_fallback_reasons():
    assert L40.decide(_step(hwaccel="cpu"), SRC).reason == "forced_cpu"
    assert L40.decide(_step(video={"codec": "vp9_nvenc"}), SRC).reason.startswith("no_encoder")
    cpu_hw = Hardware(cuda=False)
    assert cpu_hw.decide(_step(), SRC).reason == "no_cuda"
    rot = MediaInfo.from_json("r.mp4", {"streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920,
         "height": 1080, "side_data_list": [{"rotation": 90}]}]})
    assert L40.decide(_step(), rot).reason == "rotation"


def test_unknown_decoder_uploads_from_cpu():
    src = MediaInfo.from_json("v.mov", {"streams": [
        {"index": 0, "codec_type": "video", "codec_name": "prores", "width": 1920,
         "height": 1080}]})
    d = L40.decide(_step(), src)
    assert d.use_cuda and not d.decode_cuda and d.upload_from_cpu


def test_labels_and_slots():
    from mf_core.media.hwaccel import GpuInfo
    hw = Hardware(cuda=True, encoders=L40.encoders, decoders=L40.decoders,
                  hwaccels={"cuda"},
                  gpus=[GpuInfo(0, "NVIDIA L40", 46068, 45000)])
    assert hw.labels()["gpu"] == "nvidia" and "av1_nvenc" in hw.labels()["nvenc"]
    assert hw.suggest_gpu_slots() == 8         # 46 GB / 1.5 GB → упор в потолок 8
    hw.session_limit = 3
    assert hw.suggest_gpu_slots() == 3