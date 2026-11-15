from mf_core.errors import (
    ConfigInvalidError, HWAccelError, ResourceError, SourceInvalidError,
    TransientNetworkError, classify, classify_ffmpeg, exp_backoff,
)


def test_hwaccel_detected_and_falls_back():
    e = classify_ffmpeg(1, ["[h264_nvenc] Cannot load libnvidia-encode.so.1"])
    assert isinstance(e, HWAccelError) and e.fallback_cpu and e.retryable


def test_cpu_oom_is_resource_not_hwaccel():
    e = classify_ffmpeg(1, ["av_interleaved_write_frame(): out of memory"])
    assert isinstance(e, ResourceError)


def test_network_and_source():
    assert isinstance(classify_ffmpeg(1, ["Connection reset by peer"]), TransientNetworkError)
    assert isinstance(classify_ffmpeg(1, ["moov atom not found"]), SourceInvalidError)
    assert isinstance(classify_ffmpeg(1, ["No such filter: 'scale_cuda'"]), ConfigInvalidError)


def test_classify_oserror():
    import errno
    assert classify(OSError(errno.ENOSPC, "no space")).retryable
    assert not classify(PermissionError("denied")).retryable
    assert isinstance(classify(TimeoutError("t")), TransientNetworkError)


def test_backoff_bounds_and_jitter():
    for attempt in range(1, 12):
        v = exp_backoff(attempt)
        assert 0 < v <= 720  # cap 600 + 20% джиттера
    assert exp_backoff(10) <= 720


def test_as_dict_shape():
    d = HWAccelError("boom", rc=1).as_dict()
    assert d["class"] == "hwaccel" and d["retryable"] is True and d["details"]["rc"] == 1
    