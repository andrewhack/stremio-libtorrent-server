from stremiosrv.transcode.fingerprint import decide

PROBE = {"streams": [
    {"track": "video", "codec": "hevc", "width": 3840, "height": 2160},
    {"track": "audio", "codec": "eac3", "channels": 6},
]}


def test_video_copy_when_supported_and_within_width():
    d = decide(PROBE, ["h264", "hevc"], ["aac"], max_audio_channels=2, max_width=3840)
    assert d["video"]["action"] == "copy"
    assert d["audio"]["action"] == "transcode"  # eac3 not in [aac]


def test_video_transcode_when_codec_unsupported():
    d = decide(PROBE, ["h264"], ["aac", "eac3"], max_audio_channels=6, max_width=3840)
    assert d["video"]["action"] == "transcode"
    assert d["audio"]["action"] == "copy"


def test_video_transcode_when_over_maxwidth():
    d = decide(PROBE, ["hevc"], ["aac"], max_audio_channels=2, max_width=1920)
    assert d["video"]["action"] == "transcode"
    assert d["video"]["scale_width"] == 1920
