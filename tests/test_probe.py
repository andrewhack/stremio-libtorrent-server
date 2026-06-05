from stremiosrv.transcode.probe import map_probe

FFPROBE = {
    "format": {"format_name": "matroska,webm", "duration": "120.5"},
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160,
         "r_frame_rate": "24000/1001", "has_b_frames": 2,
         "color_transfer": "smpte2084", "color_primaries": "bt2020"},
        {"index": 1, "codec_type": "audio", "codec_name": "eac3", "channels": 6},
    ],
}


def test_map_video():
    v = map_probe(FFPROBE)["streams"][0]
    assert v["track"] == "video" and v["codec"] == "hevc"
    assert v["width"] == 3840 and v["isHdr"] is True
    assert v["hasBFrames"] is True
    assert abs(v["frameRate"] - 23.976) < 0.01


def test_map_audio_and_format():
    p = map_probe(FFPROBE)
    assert p["streams"][1]["channels"] == 6
    assert p["format"]["name"] == "matroska,webm"
    assert p["format"]["duration"] == 120.5


def test_dovi_via_codec_tag():
    j = {"format": {}, "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "hevc", "codec_tag_string": "dvhe"}]}
    assert map_probe(j)["streams"][0]["isDoVi"] is True


def test_no_hdr_for_sdr():
    j = {"format": {}, "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264",
         "color_transfer": "bt709", "color_primaries": "bt709"}]}
    s = map_probe(j)["streams"][0]
    assert s["isHdr"] is False and s["isDoVi"] is False
