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


# --- HDR. A transcode wrote an HDR source out as 8-bit SDR without converting its light levels, so
# PQ-coded pictures came out grey and flat. When a transcode is already chosen for an HDR source, the
# decision now asks for tone mapping to SDR; a stream that is copied is left as HDR.

HDR = {"streams": [
    {"track": "video", "codec": "hevc", "width": 3840, "height": 2160, "isHdr": True},
    {"track": "audio", "codec": "aac", "channels": 2},
]}


def test_an_hdr_source_that_is_transcoded_is_tone_mapped():
    d = decide(HDR, ["h264"], ["aac"], max_audio_channels=2, max_width=3840)
    assert d["video"] == {"action": "transcode", "scale_width": 3840, "tonemap": True}


def test_an_hdr_source_played_as_it_is_stays_hdr():
    d = decide(HDR, ["h264", "hevc"], ["aac"], max_audio_channels=2, max_width=3840)
    assert d["video"] == {"action": "copy"}


def test_an_sdr_source_is_transcoded_as_before():
    d = decide(PROBE, ["h264"], ["aac"], max_audio_channels=2, max_width=3840)
    assert "tonemap" not in d["video"]
