"""试听片段防护：扩展名嗅探与时长探测（2026-10-12 网易 30s 片段落库根治）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from proxy.app import _audio_probe_duration_s, _sniff_audio_ext


def test_sniff_id3_mp3(tmp_path):
    f = tmp_path / "x.flac"  # 后缀骗人：内容 ID3(mp3)
    f.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x23TS SE" + b"\x00" * 20)
    assert _sniff_audio_ext(str(f)) == "mp3"


def test_sniff_flac_mp4_ogg(tmp_path):
    f1 = tmp_path / "a"; f1.write_bytes(b"fLaC\x00" + b"\x00" * 20)
    f2 = tmp_path / "b"; f2.write_bytes(b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 20)
    f3 = tmp_path / "c"; f3.write_bytes(b"OggS\x00" + b"\x00" * 20)
    assert _sniff_audio_ext(str(f1)) == "flac"
    assert _sniff_audio_ext(str(f2)) == "m4a"
    assert _sniff_audio_ext(str(f3)) == "ogg"


def test_sniff_unknown_returns_none(tmp_path):
    f = tmp_path / "junk"
    f.write_bytes(b"\x00\x01\x02\x03" + b"\x00" * 32)
    assert _sniff_audio_ext(str(f)) is None


def test_probe_duration_garbage_none(tmp_path):
    f = tmp_path / "bad.flac"
    f.write_bytes(b"not audio at all" * 4)
    assert _audio_probe_duration_s(str(f)) is None
    assert _audio_probe_duration_s(str(tmp_path / "missing")) is None
