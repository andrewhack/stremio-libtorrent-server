from stremiosrv.transcode.converter import build_hls_cmd

DEC_TRANSCODE = {"video": {"action": "transcode", "scale_width": 1920},
                 "audio": {"action": "transcode"}}
DEC_COPY = {"video": {"action": "copy"}, "audio": {"action": "copy"}}


def test_nvenc_hls():
    cmd = build_hls_cmd("http://x/0", DEC_TRANSCODE, "nvenc-linux", "/tmp/j")
    assert "h264_nvenc" in cmd
    assert "-hwaccel" in cmd and "cuda" in cmd
    assert any("scale=1920" in p for p in cmd)
    assert "aac" in cmd
    assert "hls" in cmd and "/tmp/j/index.m3u8" in cmd


def test_vaapi_hls():
    cmd = build_hls_cmd("http://x/0", DEC_TRANSCODE, "vaapi-renderD128", "/tmp/j")
    assert "h264_vaapi" in cmd
    assert "vaapi" in cmd
    # h264_vaapi is 8-bit only and the decoder hands it whatever the source was, so the filter has
    # to convert: a 10-bit source otherwise fails at init, with no fallback.
    assert "scale_vaapi=w=1920:h=-2:format=nv12" in cmd


def test_vaapi_hls_converts_the_pixel_format_with_nothing_to_scale():
    """The other branch. It emitted no -vf at all, so there was nowhere for the conversion to go."""
    cmd = build_hls_cmd("http://x/0", {"video": {"action": "transcode"}}, "vaapi-x", "/tmp/j")
    assert "-vf" in cmd
    assert "scale_vaapi=format=nv12" in cmd


def test_cpu_hls():
    cmd = build_hls_cmd("http://x/0", DEC_TRANSCODE, None, "/tmp/j")
    assert "libx264" in cmd


def test_copy_hls_has_no_decode_accel():
    cmd = build_hls_cmd("http://x/0", DEC_COPY, "nvenc-linux", "/tmp/j")
    assert "copy" in cmd
    assert "-hwaccel" not in cmd


def test_no_audio_stream():
    cmd = build_hls_cmd("http://x/0", {"video": {"action": "copy"}}, None, "/tmp/j")
    assert "0:a:0?" not in cmd
