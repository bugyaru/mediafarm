import pytest
from pydantic import ValidationError

from mf_core.schemas import JobCreate, PresetSpec, ScheduleIn

FFMBC_SPEC = {
    "name": "imx_d10", "version": 1,
    "requires_gpu": True, "selector": {"gpu": "nvidia"},
    "steps": [
        {"id": "acquire", "mode": "staged"},
        {"id": "transcode", "engine": "ffmbc",
         "video": {"codec": "mpeg2video", "bitrate": "30M"}, "container": "mxf"},
        {"id": "publish", "storage": "out_smb", "path": "{asset.stem}.mxf"},
    ],
}

CUDA_SPEC = {
    "name": "proxy_1080p", "requires_gpu": True,
    "steps": [
        {"id": "acquire"}, {"id": "probe", "engine": "ffmpeg"},
        {"id": "transcode", "engine": "ffmpeg",
         "video": {"codec": "h264_nvenc"}, "scale": {"height": 1080}},
        {"id": "publish", "storage": "out_s3", "path": "{asset.stem}.mp4"},
    ],
}


def test_ffmbc_preset_drops_gpu_requirement():
    spec = PresetSpec.model_validate(FFMBC_SPEC)
    assert spec.requires_gpu is False
    assert "gpu" not in spec.selector


def test_cuda_preset_gets_selector():
    spec = PresetSpec.model_validate(CUDA_SPEC)
    assert spec.requires_gpu and spec.selector["gpu"] == "nvidia"


def test_publish_requires_storage_and_path():
    bad = {"name": "x", "steps": [{"id": "acquire"}, {"id": "publish"}]}
    with pytest.raises(ValidationError):
        PresetSpec.model_validate(bad)


def test_duplicate_step_ids_rejected():
    with pytest.raises(ValidationError):
        PresetSpec.model_validate({"name": "x", "steps": [{"id": "acquire"}, {"id": "acquire"}]})


def test_scale_requires_dimension():
    with pytest.raises(ValidationError):
        PresetSpec.model_validate(
            {"name": "x", "steps": [{"id": "transcode", "video": {}, "scale": {"mode": "fit"}}]}
        )


def test_job_create_needs_source_and_preset():
    with pytest.raises(ValidationError):
        JobCreate(preset_name="p")
    with pytest.raises(ValidationError):
        JobCreate(url="https://x/y")
    assert JobCreate(url="https://x/y", preset_name="p").kind == "transcode"


def test_cron_validation():
    assert ScheduleIn(name="s", cron="*/15 * * * *", job_template={}).cron
    with pytest.raises(ValidationError):
        ScheduleIn(name="s", cron="every day", job_template={})