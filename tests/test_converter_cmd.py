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
    assert any("scale_vaapi=w=1920:h=-2:format=nv12" in p for p in cmd)


def test_vaapi_hls_converts_the_pixel_format_with_nothing_to_scale():
    """The other branch. It emitted no -vf at all, so there was nowhere for the conversion to go."""
    cmd = build_hls_cmd("http://x/0", {"video": {"action": "transcode"}}, "vaapi-x", "/tmp/j")
    assert "-vf" in cmd
    assert any("scale_vaapi=format=nv12" in p for p in cmd)


# A VAAPI decoder that cannot read the source (10-bit HEVC on older Intel, AV1 before its hardware
# decodes it) hands ffmpeg software frames, and a chain that starts at scale_vaapi cannot take
# them: the transcode failed outright. `format=nv12|vaapi,hwupload` passes hardware frames straight
# through and uploads software ones, so the encode stays on the GPU either way. hwupload needs a
# named device, so the render node is opened explicitly and shared by decoder and filters.
def test_vaapi_hls_uploads_frames_the_hardware_could_not_decode():
    cmd = build_hls_cmd("http://x/0", DEC_TRANSCODE, "vaapi-renderD128", "/tmp/j")
    assert cmd[cmd.index("-vf") + 1] == (
        "format=nv12|vaapi,hwupload,scale_vaapi=w=1920:h=-2:format=nv12")
    assert cmd[cmd.index("-init_hw_device") + 1] == "vaapi=va:/dev/dri/renderD128"
    assert cmd[cmd.index("-hwaccel_device") + 1] == "va"
    assert cmd[cmd.index("-filter_hw_device") + 1] == "va"
    assert cmd.index("-init_hw_device") < cmd.index("-hwaccel") < cmd.index("-i")


def test_vaapi_hls_uploads_with_nothing_to_scale():
    cmd = build_hls_cmd("http://x/0", {"video": {"action": "transcode"}}, "vaapi-renderD128",
                        "/tmp/j")
    assert cmd[cmd.index("-vf") + 1] == "format=nv12|vaapi,hwupload,scale_vaapi=format=nv12"


def test_cpu_hls():
    cmd = build_hls_cmd("http://x/0", DEC_TRANSCODE, None, "/tmp/j")
    assert "libx264" in cmd


# libx264 keeps a 10-bit source 10-bit unless told otherwise, and H.264 High 10 plays only in
# software decoders: browsers decode it on the CPU at best, TV and phone hardware not at all.
# The NVENC branch already normalises to yuv420p; the CPU branch has to as well.
def test_cpu_hls_outputs_8_bit_when_scaling():
    cmd = build_hls_cmd("http://x/0", DEC_TRANSCODE, None, "/tmp/j")
    assert cmd[cmd.index("-vf") + 1] == "scale=1920:-2:flags=lanczos,format=yuv420p"


def test_cpu_hls_outputs_8_bit_with_nothing_to_scale():
    cmd = build_hls_cmd("http://x/0", {"video": {"action": "transcode"}}, None, "/tmp/j")
    assert cmd[cmd.index("-vf") + 1] == "format=yuv420p"


def test_copy_hls_has_no_decode_accel():
    cmd = build_hls_cmd("http://x/0", DEC_COPY, "nvenc-linux", "/tmp/j")
    assert "copy" in cmd
    assert "-hwaccel" not in cmd


def test_no_audio_stream():
    cmd = build_hls_cmd("http://x/0", {"video": {"action": "copy"}}, None, "/tmp/j")
    assert "0:a:0?" not in cmd


# The byte route ends a response early when a torrent piece misses its timeout, and ffmpeg took a
# body shorter than its Content-Length for the end of the input: it exited 0 and marked the playlist
# finished part-way through the film. Reconnecting asks again from the byte it stopped at.
def test_build_hls_cmd_reconnects_an_http_input_that_ends_short():
    argv = build_hls_cmd("http://127.0.0.1:11470/" + "a" * 40 + "/0", DEC_COPY, None, "/tmp/j")
    r = argv.index("-reconnect")
    assert argv[r + 1] == "1"
    assert argv[argv.index("-reconnect_delay_max") + 1] == "30"
    assert r < argv.index("-i")


def test_build_hls_cmd_leaves_reconnect_off_for_a_local_file():
    # ffmpeg answers "Option reconnect not found." and exits 1 for a file input.
    argv = build_hls_cmd("/tmp/src.mp4", DEC_COPY, None, "/tmp/j")
    assert "-reconnect" not in argv


def test_build_hls_cmd_has_a_protocol_whitelist_before_input():
    """ffmpeg must not be free to follow whatever scheme a redirect throws at it -- only the
    handful this server actually serves media over (Minor 8's protocol whitelist), and it has to
    precede -i to guard the input it names."""
    argv = build_hls_cmd("http://127.0.0.1:1/x", DEC_COPY, None, "/tmp/j")
    assert "-protocol_whitelist" in argv
    i = argv.index("-protocol_whitelist")
    assert argv[i + 1] == "file,crypto,data,http,tcp,tls,https"
    assert i < argv.index("-i")
