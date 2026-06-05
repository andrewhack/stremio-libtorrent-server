import struct

from stremiosrv.subs.opensub import opensubtitles_hash


def test_zero_file_hash_equals_filesize(tmp_path):
    p = tmp_path / "zero.bin"
    p.write_bytes(b"\x00" * (2 * 65536))  # 128 KiB of zeros
    # head sum 0 + tail sum 0 + filesize(131072=0x20000)
    assert opensubtitles_hash(str(p)) == "0000000000020000"


def test_leading_int64_adds_to_hash(tmp_path):
    data = bytearray(b"\x00" * (2 * 65536))
    data[0:8] = struct.pack("<Q", 1)  # first int64 = 1
    p = tmp_path / "one.bin"
    p.write_bytes(bytes(data))
    # filesize 131072 + 1 (head) + 0 (tail) = 131073 = 0x20001
    assert opensubtitles_hash(str(p)) == "0000000000020001"


def test_hash_is_16_hex_chars(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"\xab" * (2 * 65536 + 123))
    h = opensubtitles_hash(str(p))
    assert len(h) == 16
    int(h, 16)  # parses as hex
