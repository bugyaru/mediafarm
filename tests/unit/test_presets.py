import pytest

from mf_core.errors import ConfigInvalidError
from mf_core.media.hwaccel import Hardware
from mf_core.media.presets import (
    BUILTIN_PRESETS, apply_overrides, snapshot, spec_from_job, validate_against_hardware,
    validate_spec,
)


def test_all_builtins_valid():
    for name, raw in BUILTIN_PRESETS.items():
        spec = validate_spec(raw)
        assert spec.name == name and spec.steps


def test_ffmbc_builtin_has_no_gpu():
    spec = validate_spec(BUILTIN_PRESETS["imx50_pal_ffmbc"])
    assert spec.requires_gpu is False and "gpu" not in spec.selector


def test_nvenc_builtin_requires_gpu_selector():
    spec = validate_spec(BUILTIN_PRESETS["proxy_1080p_nvenc"])
    assert spec.requires_gpu and spec.selector["gpu"] == "nvidia"


def test_overrides_merge_deep():
    raw = BUILTIN_PRESETS["proxy_1080p_nvenc"]
    out = apply_overrides(raw, {"transcode": {"video": {"bitrate": "12M"}}})
    step = next(s for s in out["steps"] if s["id"] == "transcode")
    assert step["video"]["bitrate"] == "12M"
    assert step["video"]["codec"] == "h264_nvenc"      # остальное сохранено
    assert raw["steps"][2]["video"]["bitrate"] == "6M"  # исходник не мутирован


def test_snapshot_roundtrip():
    spec = validate_spec(BUILTIN_PRESETS["av1_l40"])
    restored = spec_from_job(snapshot(spec))
    assert restored.name == spec.name and len(restored.steps) == len(spec.steps)
    with pytest.raises(ConfigInvalidError):
        spec_from_job({})


def test_hardware_warnings():
    cpu = Hardware(cuda=False, encoders={"libx264"}, decoders=set())
    warns = validate_against_hardware(validate_spec(BUILTIN_PRESETS["proxy_1080p_nvenc"]), cpu)
    assert any("fallback CPU" in w for w in warns)
    assert any("требует GPU" in w for w in warns)
    ffmbc = validate_against_hardware(validate_spec(BUILTIN_PRESETS["imx50_pal_ffmbc"]), cpu)
    assert any("ffmbc" in w for w in ffmbc)