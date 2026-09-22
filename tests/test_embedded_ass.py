"""Embedded ASS for TV players, the pure half: what a file offers, and the ffmpeg that extracts it.

The contract is stremio-video 0.0.98's code (no published stock server answers these routes). The
tests at the bottom run the argv against a real file; they skip where ffmpeg is not installed.
"""
from __future__ import annotations

import json
import re
import subprocess

import pytest
from embedded_ass_fixture import build, needs_ffmpeg

from stremiosrv.subs import embedded_ass as ea


def _sub(index, codec, **tags):
    return {"index": index, "codec_type": "subtitle", "codec_name": codec, "tags": tags}


def _att(index, **tags):
    return {"index": index, "codec_type": "attachment", "codec_name": "ttf", "tags": tags}


def test_only_ass_and_ssa_tracks_are_offered():
    found = ea.discover({"streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        _sub(1, "ass", language="eng", title="Signs"),
        _sub(2, "subrip", language="eng"),
        _sub(3, "SSA", language="bul"),
        _sub(4, "hdmv_pgs_subtitle"),
    ]})
    assert found.answer()["tracks"] == [
        {"number": 1, "codec": "ass", "lang": "eng", "label": "Signs (styled)"},
        {"number": 3, "codec": "ssa", "lang": "bul", "label": "bul (styled)"},
    ]
    assert found.has_track(1) and found.has_track(3)
    assert not found.has_track(2), "a SubRip track stays with the TV's native player"


def test_a_track_with_no_tags_is_und_and_named_by_its_number():
    [track] = ea.discover({"streams": [_sub(7, "ass")]}).answer()["tracks"]
    assert track == {"number": 7, "codec": "ass", "lang": "und", "label": "Track 7 (styled)"}


def test_tag_keys_are_read_whatever_their_case():
    [track] = ea.discover({"streams": [_sub(2, "ass", LANGUAGE="jpn", TITLE="Full")]}).tracks
    assert (track["lang"], track["label"]) == ("jpn", "Full (styled)")


def test_fonts_are_what_the_client_calls_fonts():
    found = ea.discover({"streams": [
        _att(3, filename="a.bin", mimetype="application/x-truetype-font"),  # by type
        _att(4, filename="b.OTF", mimetype="application/vnd.ms-opentype"),  # by extension only
        _att(5, filename="c.woff2"),                                        # by extension only
        _att(6, filename="cover.jpg", mimetype="image/jpeg"),               # neither
        _att(7, filename="d.ttf.txt", mimetype="text/plain"),               # the name must END so
    ]})
    assert found.answer()["fonts"] == [{"id": 3}, {"id": 4}, {"id": 5}]
    # served as its own type when that is a font type, else as bytes
    assert found.fonts == {3: "application/x-truetype-font", 4: "application/octet-stream",
                           5: "application/octet-stream"}


def test_a_mime_type_with_parameters_still_counts():
    found = ea.discover({"streams": [_att(3, mimetype="font/ttf; charset=binary")]})
    assert found.fonts == {3: "font/ttf"}


def test_a_file_with_no_ass_answers_empty_lists():
    assert ea.discover({"streams": [_sub(1, "subrip")]}).answer() == {"tracks": [], "fonts": []}
    assert ea.discover({}).answer() == {"tracks": [], "fonts": []}


def test_streams_without_an_index_are_skipped():
    assert ea.discover({"streams": [{"codec_type": "subtitle", "codec_name": "ass"}, "junk"]}
                       ).tracks == ()


@pytest.mark.parametrize(("frm", "to"), [
    (None, "60000"), ("0", None),                 # missing
    ("-1", "60000"), ("1.5", "60000"), ("x", "1"),  # not whole non-negative milliseconds
    ("60000", "60000"), ("60000", "0"),            # empty, reversed
    ("0", "300001"),                               # longer than MAX_WINDOW_MS
])
def test_windows_that_are_refused(frm, to):
    assert ea.parse_window(frm, to) is None


def test_the_windows_the_client_sends_are_accepted():
    assert ea.parse_window("0", "60000") == (0, 60000)
    assert ea.parse_window("480000", "540000") == (480000, 540000)
    assert ea.parse_window("0", "300000") == (0, 300000)


def test_window_argv_keeps_absolute_times_and_ends_with_to():
    argv = ea.window_argv("http://127.0.0.1:1/r", 3, 480000, 540000)
    assert argv[argv.index("-ss") + 1] == "470.000"  # LEAD_MS before the window
    assert argv[argv.index("-to") + 1] == "540.000"  # an absolute end...
    assert "-t" not in argv                           # ...never a duration, which -copyts breaks
    assert "-copyts" in argv
    assert argv.index("-copyts") < argv.index("-ss") < argv.index("-i")  # an input seek
    assert argv[argv.index("-map") + 1] == "0:3"
    assert argv[-3:] == ["-f", "ass", "pipe:1"]


def test_the_lead_never_seeks_before_zero():
    argv = ea.window_argv("u", 1, 0, 60000)
    assert argv[argv.index("-ss") + 1] == "0.000"


def test_font_dump_names_each_file_by_stream_index(tmp_path):
    argv = ea.font_dump_argv("u", [3, 4], str(tmp_path))
    assert argv[argv.index("-dump_attachment:3") + 1] == str(tmp_path / "3.font")
    assert argv[argv.index("-dump_attachment:4") + 1] == str(tmp_path / "4.font")
    assert argv[argv.index("-i") + 1] == "u"


# --- the argv against the real ffmpeg ---

_DIALOGUE = re.compile(r"^Dialogue:\s*[^,]*,(\d+):(\d\d):(\d\d)\.(\d\d),", re.MULTILINE)


def _starts(text: str) -> list[float]:
    return [int(h) * 3600 + int(m) * 60 + int(s) + int(cs) / 100
            for h, m, s, cs in _DIALOGUE.findall(text)]


@pytest.fixture(scope="module")
def fixture(tmp_path_factory):
    return build(tmp_path_factory.mktemp("ass"))


@needs_ffmpeg
def test_discover_on_a_real_file(fixture):
    out = subprocess.run(ea.probe_argv(str(fixture["path"])), capture_output=True, check=True)
    found = ea.discover(json.loads(out.stdout))
    assert found.answer() == {
        "tracks": [{"number": 1, "codec": "ass", "lang": "eng", "label": "Signs (styled)"}],
        "fonts": [{"id": 3}, {"id": 4}],
    }
    assert found.fonts == {3: "application/x-truetype-font", 4: "application/octet-stream"}


@needs_ffmpeg
def test_a_window_holds_the_header_and_its_events_at_absolute_times(fixture):
    out = subprocess.run(ea.window_argv(str(fixture["path"]), 1, 30000, 60000),
                         capture_output=True, check=True)
    text = out.stdout.decode("utf-8")
    for section in ("[Script Info]", "[V4+ Styles]", "Style: Default,Fixture Sans", "[Events]"):
        assert section in text
    starts = _starts(text)
    # every event starting from LEAD_MS before the window up to its end, with the file's own times
    assert set(starts) == {float(t) for t in range(20, 60, 2)}


@needs_ffmpeg
def test_the_font_dump_writes_each_font_byte_for_byte(fixture, tmp_path):
    subprocess.run(ea.font_dump_argv(str(fixture["path"]), [3, 4], str(tmp_path)),
                   capture_output=True, check=True)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["3.font", "4.font"]
    for i in (3, 4):
        assert (tmp_path / f"{i}.font").read_bytes() == fixture["attachments"][i]
