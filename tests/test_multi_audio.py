"""Every audio track in a transcoded stream (1.6.47).

A file with two or more audio tracks is transcoded into one video rendition plus one audio
rendition per track, and the server writes the master playlist that ties them together, as the
stock server does. A file with one audio track keeps the output it had before.
"""
import pathlib

from fastapi.testclient import TestClient

from stremiosrv.app import create_app
from stremiosrv.transcode import converter as converter_mod
from stremiosrv.transcode.converter import (
    Converter,
    audio_master,
    build_hls_cmd,
    first_playlist,
)
from stremiosrv.transcode.fingerprint import MAX_AUDIO_TRACKS, decide
from stremiosrv.transcode.probe import map_probe


def _probe(*audio: dict) -> dict:
    streams = [{"index": 0, "track": "video", "codec": "h264", "width": 1920}]
    for i, a in enumerate(audio, start=1):
        streams.append({"index": i, "track": "audio", "codec": "ac3", "channels": 6, **a})
    return {"streams": streams}


TWO = _probe({"lang": "eng", "title": "English", "default": False},
             {"lang": "bul", "title": "Bulgarian", "default": True})

TRACKS = [
    {"index": 1, "lang": "eng", "title": "English", "default": False},
    {"index": 2, "lang": "bul", "title": "Bulgarian", "default": True},
]
DEC_MULTI = {"video": {"action": "copy"}, "audio": {"action": "transcode", "tracks": TRACKS}}


# --- the probe carries what the master needs -------------------------------------------------


def test_the_probe_carries_each_audio_tracks_title_and_default_flag():
    p = map_probe({"format": {}, "streams": [
        {"index": 1, "codec_type": "audio", "codec_name": "ac3", "channels": 6,
         "tags": {"language": "bul", "title": "Bulgarian"}, "disposition": {"default": 1}},
        {"index": 2, "codec_type": "audio", "codec_name": "aac", "channels": 2,
         "disposition": {"default": 0}},
    ]})
    a, b = p["streams"]
    assert (a["title"], a["default"]) == ("Bulgarian", True)
    assert (b["title"], b["default"]) == (None, False)


# --- the decision ----------------------------------------------------------------------------


def test_two_audio_tracks_are_all_carried_into_the_decision():
    d = decide(TWO, ["h264"], ["ac3"], max_audio_channels=6, max_width=3840)
    assert d["audio"]["tracks"] == TRACKS


def test_with_several_tracks_every_one_is_encoded_even_when_the_first_could_be_copied():
    # One encode settings for all tracks: AAC stereo, which every player takes.
    d = decide(TWO, ["h264"], ["ac3"], max_audio_channels=6, max_width=3840)
    assert d["audio"]["action"] == "transcode"


def test_one_audio_track_keeps_the_decision_it_had():
    one = _probe({"lang": "eng", "title": "English", "default": True})
    d = decide(one, ["h264"], ["ac3"], max_audio_channels=6, max_width=3840)
    assert d["audio"] == {"action": "copy"}


def test_the_number_of_audio_tracks_is_capped():
    many = _probe(*({"lang": "eng"} for _ in range(MAX_AUDIO_TRACKS + 3)))
    d = decide(many, ["h264"], ["aac"], max_audio_channels=2, max_width=3840)
    assert MAX_AUDIO_TRACKS == 8
    assert [t["index"] for t in d["audio"]["tracks"]] == list(range(1, MAX_AUDIO_TRACKS + 1))


def test_a_file_with_no_video_keeps_the_single_track_path():
    audio_only = {"streams": [{"index": 0, "track": "audio", "codec": "aac", "channels": 2},
                              {"index": 1, "track": "audio", "codec": "aac", "channels": 2}]}
    d = decide(audio_only, ["h264"], ["aac"], max_audio_channels=2, max_width=3840)
    assert "tracks" not in d["audio"]


# --- the ffmpeg command ----------------------------------------------------------------------


def test_each_audio_track_gets_its_own_rendition():
    argv = build_hls_cmd("http://x/0", DEC_MULTI, None, "/tmp/j")
    i = argv.index("-i")
    maps = [argv[k + 1] for k in range(i, len(argv)) if argv[k] == "-map"]
    assert maps == ["0:v:0", "0:1", "0:2"]
    assert argv[argv.index("-var_stream_map") + 1] == (
        "v:0,name:video0 a:0,name:audio0 a:1,name:audio1")
    assert argv[-1] == "/tmp/j/%v.m3u8"
    assert argv[argv.index("-hls_segment_filename") + 1] == "/tmp/j/%v_%d.m4s"
    assert argv[argv.index("-hls_fmp4_init_filename") + 1] == "%v_init.mp4"


def test_every_audio_rendition_is_aac_stereo():
    argv = build_hls_cmd("http://x/0", DEC_MULTI, None, "/tmp/j")
    assert argv[argv.index("-c:a") + 1] == "aac"
    assert argv[argv.index("-ac") + 1] == "2"


def test_the_server_writes_the_master_not_ffmpeg():
    # ffmpeg's own master names every track "audio_N" and cannot mark the file's default track.
    argv = build_hls_cmd("http://x/0", DEC_MULTI, None, "/tmp/j")
    assert "-master_pl_name" not in argv


def test_rendition_names_never_carry_tag_text():
    # var_stream_map's name: becomes the file name; a title there would put tag text into paths.
    tracks = [{"index": 1, "lang": "eng", "title": "../x y", "default": False},
              {"index": 2, "lang": "bul", "title": "a,b:c", "default": False}]
    argv = build_hls_cmd("http://x/0", {"video": {"action": "copy"},
                                        "audio": {"action": "transcode", "tracks": tracks}},
                         None, "/tmp/j")
    assert argv[argv.index("-var_stream_map") + 1] == (
        "v:0,name:video0 a:0,name:audio0 a:1,name:audio1")


def test_one_audio_track_keeps_the_exact_command_it_had():
    argv = build_hls_cmd("http://x/0", {"video": {"action": "copy"}, "audio": {"action": "copy"}},
                         None, "/tmp/j")
    assert argv == [
        "ffmpeg", "-hide_banner", "-y", "-protocol_whitelist", "file,crypto,data,http,tcp,tls,https",
        "-reconnect", "1", "-reconnect_delay_max", "30",
        "-i", "http://x/0", "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "copy", "-c:a", "copy",
        "-f", "hls", "-hls_time", "4", "-hls_playlist_type", "event",
        "-hls_segment_type", "fmp4", "-hls_flags", "independent_segments",
        "-hls_fmp4_init_filename", "init.mp4",
        "-hls_segment_filename", "/tmp/j/seg%d.m4s",
        "-master_pl_name", "master.m3u8", "/tmp/j/index.m3u8",
    ]


# --- the master playlist ---------------------------------------------------------------------


def _media_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("#EXT-X-MEDIA:")]


def test_the_master_lists_each_track_and_the_video():
    text = audio_master(TRACKS)
    assert text.startswith("#EXTM3U\n")
    assert _media_lines(text) == [
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="English",LANGUAGE="eng",'
        'AUTOSELECT=YES,DEFAULT=NO,URI="audio0.m3u8"',
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Bulgarian",LANGUAGE="bul",'
        'AUTOSELECT=YES,DEFAULT=YES,URI="audio1.m3u8"',
    ]
    lines = text.splitlines()
    inf = next(i for i, line in enumerate(lines) if line.startswith("#EXT-X-STREAM-INF:"))
    assert 'AUDIO="audio"' in lines[inf] and "BANDWIDTH=" in lines[inf]
    assert lines[inf + 1] == "video0.m3u8"


def test_the_first_track_is_default_when_the_file_marks_none():
    tracks = [dict(t, default=False) for t in TRACKS]
    defaults = ["DEFAULT=YES" in line for line in _media_lines(audio_master(tracks))]
    assert defaults == [True, False]


def test_only_one_track_is_default_when_the_file_marks_several():
    tracks = [dict(t, default=True) for t in TRACKS]
    defaults = ["DEFAULT=YES" in line for line in _media_lines(audio_master(tracks))]
    assert defaults == [True, False]


def test_a_track_is_named_by_its_language_without_a_title_then_by_its_number():
    tracks = [{"index": 1, "lang": "eng", "title": None, "default": True},
              {"index": 2, "lang": None, "title": "", "default": False}]
    lines = _media_lines(audio_master(tracks))
    assert 'NAME="eng",LANGUAGE="eng"' in lines[0]
    assert 'NAME="Audio 2"' in lines[1] and "LANGUAGE=" not in lines[1]


def test_tag_text_cannot_break_out_of_a_quoted_attribute():
    # An HLS quoted string has no escape: a double quote or a line break in a title would end the
    # attribute or the tag. They are replaced, and a language that is not a language tag is left out.
    tracks = [{"index": 1, "lang": 'en",URI="x', "title": 'Say "hi"\n#EXT-X-ENDLIST',
               "default": True},
              {"index": 2, "lang": "bul", "title": "Bulgarian", "default": False}]
    text = audio_master(tracks)
    first = _media_lines(text)[0]
    assert "\n#EXT-X-ENDLIST" not in text
    assert first.count('"') == 6  # GROUP-ID, NAME and URI, each quoted once: no LANGUAGE
    assert "LANGUAGE=" not in first


def test_tracks_with_the_same_name_are_told_apart():
    tracks = [{"index": 1, "lang": "eng", "title": "Stereo", "default": True},
              {"index": 2, "lang": "eng", "title": "Stereo", "default": False}]
    lines = _media_lines(audio_master(tracks))
    assert 'NAME="Stereo"' in lines[0] and 'NAME="Stereo (2)"' in lines[1]


# --- the job and the route -------------------------------------------------------------------


def test_the_playlist_that_proves_a_job_started():
    assert first_playlist(DEC_MULTI) == "video0.m3u8"
    assert first_playlist({"video": {"action": "copy"}, "audio": {"action": "copy"}}) == (
        "master.m3u8")


class _Proc:
    def poll(self):
        return None


def test_a_multi_track_job_has_its_master_written_by_the_server(tmp_path, monkeypatch):
    monkeypatch.setattr(converter_mod.subprocess, "Popen", lambda *a, **k: _Proc())
    conv = Converter(str(tmp_path), None)
    d = conv.ensure_job("job1", "http://x/0", DEC_MULTI)
    assert (d / "master.m3u8").read_text() == audio_master(TRACKS)


def test_a_single_track_job_leaves_the_master_to_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(converter_mod.subprocess, "Popen", lambda *a, **k: _Proc())
    conv = Converter(str(tmp_path), None)
    d = conv.ensure_job("job1", "http://x/0", {"video": {"action": "copy"},
                                               "audio": {"action": "copy"}})
    assert not (d / "master.m3u8").exists()


def test_the_master_route_waits_for_ffmpegs_first_playlist(tmp_path, monkeypatch):
    """The server's own master exists the moment the job starts, so waiting for it would prove
    nothing: the route waits for the video playlist ffmpeg writes, and still answers 504 when that
    never comes."""
    from stremiosrv.api import hls

    waited: list[pathlib.Path] = []

    def fake_wait(path, timeout):
        waited.append(path)
        return False

    class Conv:
        def ensure_job(self, job_id, media, dec, key=None):
            (tmp_path / "master.m3u8").write_text(audio_master(dec["audio"]["tracks"]))
            return tmp_path

    monkeypatch.setattr(hls, "probe_media", lambda media: {
        "format": {"name": "matroska"}, "streams": TWO["streams"], "samples": {}})
    monkeypatch.setattr(hls, "resolve_media_input", lambda request, url: url)
    monkeypatch.setattr(hls, "_wait_file", fake_wait)
    app = create_app()
    app.state.converter = Conv()
    r = TestClient(app).get("/hlsv2/job1/master.m3u8",
                            params={"mediaURL": "http://x/y", "audioCodecs": "aac"})
    assert waited == [tmp_path / "video0.m3u8"]
    assert r.status_code == 504
