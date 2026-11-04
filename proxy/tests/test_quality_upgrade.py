"""quality_upgrade 单测：音质分档、候选归类（精确/疑似）、SQL 过滤。

只测纯逻辑，不碰官方库、不下载——替换链路的文件操作靠真机试运行验证。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quality_upgrade as qu  # noqa: E402


# ---------------------------------------------------------------- 分档 --

def test_lossless_tiers():
    assert qu._tier("flac", 0, 16) == qu.TIER_LOSSLESS
    assert qu._tier("flac", 0, 24) == qu.TIER_LOSSLESS_HIRES
    assert qu._tier("wav", 0, 32) == qu.TIER_LOSSLESS_HIRES
    # 无损一定优于任何有损
    assert qu._tier("flac", 0, 16) < qu._tier("mp3", 320000)


def test_lossy_tiers():
    assert qu._tier("mp3", 320000) == qu.TIER_HIGH
    assert qu._tier("mp3", 192000) == qu.TIER_MID
    assert qu._tier("mp3", 128000) == qu.TIER_LOW
    assert qu._tier("m4a", 62208) == qu.TIER_LOW


def test_fmt_bitrate_display():
    assert qu._fmt_bitrate("mp3", 320000) == "320kbps"
    assert qu._fmt_bitrate("mp3", 128000) == "128kbps"
    # db 里个别 mp3 记了异常高码率，展示钳到 320k
    assert qu._fmt_bitrate("mp3", 905000) == "320kbps"
    assert qu._fmt_bitrate("flac", 900000, 16, 44100) == "44.1kHz/16bit"
    assert qu._fmt_bitrate("flac", 0) == "无损"
    assert qu._fmt_bitrate("mp3", 0) == "未知"


def test_tier_label():
    assert qu._tier_label("flac", 0, 24) == "Hi-Res"
    assert qu._tier_label("flac", 0, 16) == "无损"


# ---------------------------------------------------------------- SQL --

def test_filter_clause():
    assert "mp3" in qu._filter_clause("lossy")
    assert "flac" in qu._filter_clause("lossless")
    assert "bitrate" in qu._filter_clause("below320")
    assert "bit_depth" in qu._filter_clause("hires")
    assert qu._filter_clause("all") == ""


# ---------------------------------------------------------------- 候选 --

CUR = {"guid": "g1", "title": "单车恋人", "artist": "后弦", "tier": qu.TIER_LOW,
       "durationMs": 249135}


def _item(**kw):
    base = {"id": "kuwo:1", "source": "kuwo", "title": "单车恋人", "artist": "后弦",
            "ext": "flac", "file_size": 48000000, "duration_s": 249.0, "album": "",
            "cover_url": ""}
    base.update(kw)
    return base


def test_shape_exact_better():
    c = qu._shape_candidate(_item(), CUR)
    assert c["kind"] == "exact"
    assert c["better"] is True
    assert c["durationDiffMs"] < 1000


def test_shape_exact_not_better_when_same_tier():
    cur = dict(CUR, tier=qu.TIER_HIGH)
    c = qu._shape_candidate(_item(ext="mp3", file_size=9000000), cur)
    assert c["kind"] == "exact"
    assert c["better"] is False


def test_shape_suspect_when_artist_differs():
    c = qu._shape_candidate(_item(artist="江易泽"), CUR)
    assert c is not None
    assert c["kind"] == "suspect"
    assert c["reason"] == "歌手不符"
    assert c["better"] is False


def test_shape_suspect_when_version_suffix():
    c = qu._shape_candidate(_item(title="单车恋人 (Live)"), CUR)
    assert c is not None
    assert c["kind"] == "suspect"
    assert c["reason"] == "版本后缀不同"


def test_shape_reject_unrelated():
    assert qu._shape_candidate(_item(title="完全不同的歌"), CUR) is None


def test_shape_reject_without_id():
    assert qu._shape_candidate(_item(id=""), CUR) is None


def test_base_title_strips_bracket_tail():
    assert qu._base_title("嗜好 (回忆版)") == qu._base_title("嗜好")
    assert qu._base_title("单车恋人【伴奏】") == qu._base_title("单车恋人")


def test_duration_suspicious_flag():
    c = qu._shape_candidate(_item(duration_s=340.0), CUR)
    assert c["durationSuspicious"] is True
    assert c["durationDiffMs"] > 3000
