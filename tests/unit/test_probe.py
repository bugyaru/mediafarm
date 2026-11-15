from mf_core.media.probe import MediaInfo, _ratio

HDR = {
    "format": {"format_name": "mov,mp4", "duration": "60.0", "size": "1000", "bit_rate": "5000000"},
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160,
         "pix_fmt": "yuv420p10le", "avg_frame_rate": "30000/1001", "field_order": "progressive",
         "color_transfer": "smpte2084", "color_primaries": "bt2020",
         "side_data_list": [{"rotation": -90}], "disposition": {"attached_pic": 0}},
        {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 6,
         "sample_rate": "48000", "tags": {"language": "rus"}, "disposition": {"default": 1}},
        {"index": 2, "codec_type": "subtitle", "codec_name": "subrip"},
    ],
}

INTERLACED = {
    "format": {"format_name": "mxf", "duration": "10.0"},
    "streams": [{"index": 0, "codec_type": "video", "codec_name": "mpeg2video",
                 "width": 720, "height": 608, "field_order": "tt",
                 "avg_frame_rate": "25/1", "pix_fmt": "yuv422p"}],
}


def test_ratio_parsing():
    assert _ratio("30000/1001") == 29.97003
    assert _ratio("0/0") is None and _ratio("N/A") is None and _ratio(None) is None


def test_hdr_rotation_audio():
    info = MediaInfo.from_json("f.mp4", HDR)
    assert info.has_video and info.has_audio and info.duration == 60.0
    v = info.video
    assert v.hdr and v.rotation == 270 and v.fps == 29.97003 and not v.interlaced
    assert info.audio[0].channels == 6 and info.audio[0].language == "rus"
    assert info.subtitle_indexes == (2,)
    assert "raw" not in info.to_dict()


def test_interlaced_detection_from_field_order():
    info = MediaInfo.from_json("f.mxf", INTERLACED)
    assert info.video.interlaced and info.video.field_order == "tt"
    assert info.video.resolution == (720, 608)


def test_attached_pic_is_not_video():
    data = {"format": {}, "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "mjpeg", "width": 100, "height": 100,
         "disposition": {"attached_pic": 1}}]}
    assert MediaInfo.from_json("a.mp3", data).has_video is False