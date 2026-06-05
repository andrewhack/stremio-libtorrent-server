from stremiosrv.transcode.ffmpeg_cmd import build_audio_cmd, build_video_cmd


def test_video_copy():
    cmd = build_video_cmd("http://x/0", {"action": "copy"}, "nvenc-linux")
    assert "-c:v" in cmd and "copy" in cmd
    assert "-force_key_frames:v" in cmd


def test_video_nvenc_transcode_downscale():
    cmd = build_video_cmd("http://x/0", {"action": "transcode", "scale_width": 1920}, "nvenc-linux")
    assert "h264_nvenc" in cmd
    assert any("scale=1920" in p for p in cmd)


def test_video_vaapi_transcode():
    cmd = build_video_cmd("http://x/0", {"action": "transcode", "scale_width": 1920}, "vaapi-renderD128")
    assert "h264_vaapi" in cmd


def test_video_cpu_fallback():
    cmd = build_video_cmd("http://x/0", {"action": "transcode", "scale_width": 1280}, None)
    assert "libx264" in cmd


def test_audio_to_aac_stereo():
    cmd = build_audio_cmd("http://x/0", {"action": "transcode"})
    assert "aac" in cmd and "-ac" in cmd


def test_audio_copy():
    cmd = build_audio_cmd("http://x/0", {"action": "copy"})
    assert "copy" in cmd
