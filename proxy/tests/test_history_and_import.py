"""history_playlist（最近常听）与 playlist_import（歌单导入）纯逻辑测试。

不依赖事件循环与官方库，Windows 本机可跑（proxy 服务本体跑在 NAS）。
"""
import importlib.util
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PROXY = _HERE.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _PROXY / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


hist = _load("history_playlist")
pimp = _load("playlist_import")


# ------------------------------------------------------ history_playlist --

def test_merge_rows_dedupes_by_title_artist_and_prefers_official():
    rows = [
        {"guid": "online:kuwo:1", "title": "晴天", "artist": "周杰伦", "album": "",
         "duration_ms": 0, "play_count": 1, "playedAt": 200, "official": False},
        {"guid": "aabbccdd0000000000000000000000ff", "title": "晴天", "artist": "周杰伦",
         "album": "叶惠美", "duration_ms": 269000, "play_count": 5, "playedAt": 100,
         "official": True},
        {"guid": "online:migu:2", "title": "七里香", "artist": "周杰伦", "album": "",
         "duration_ms": 0, "play_count": 9, "playedAt": 300, "official": False},
    ]
    merged = hist._merge_rows(rows, 10)
    # 同曲合并后取官方形态、播放次数取大（5）
    sunny = next(r for r in merged if r["title"] == "晴天")
    assert sunny["official"] is True
    assert sunny["guid"] == "aabbccdd0000000000000000000000ff"
    assert sunny["play_count"] == 5
    # 排序按次数 desc：七里香(9) 在 晴天(5) 前
    assert [r["title"] for r in merged] == ["七里香", "晴天"]


def test_merge_rows_respects_limit():
    rows = [{"guid": f"online:kuwo:{i}", "title": f"T{i}", "artist": "A", "album": "",
             "duration_ms": 0, "play_count": i, "playedAt": i, "official": False}
            for i in range(50)]
    assert len(hist._merge_rows(rows, 30)) == 30


def test_playlist_size_env(monkeypatch):
    monkeypatch.setenv("FNMUSIC_HISTORY_PLAYLIST_SIZE", "abc")
    assert hist.playlist_size() == 30
    monkeypatch.setenv("FNMUSIC_HISTORY_PLAYLIST_SIZE", "120")
    assert hist.playlist_size() == 100  # 上限钳制
    monkeypatch.setenv("FNMUSIC_HISTORY_PLAYLIST_SIZE", "10")
    assert hist.playlist_size() == 10


def test_card_for_disguises_online_cover(monkeypatch):
    monkeypatch.setattr(hist, "_cover_track", {"u1": "online:kuwo:123"}, raising=False)
    monkeypatch.setattr(hist, "_tracks_cache", {"u1": ([{"guid": "online:kuwo:123"}], 9e12)},
                        raising=False)
    card = hist.card_for("u1", fake_fn=lambda g: "f" * 32)
    assert card["guid"] == hist.PLAYLIST_GUID
    assert card["coverId"].startswith("track_")
    assert card["isDaily"] is True


# ------------------------------------------------------ playlist_import --

def test_parse_input_urls():
    assert pimp.parse_input("https://music.163.com/playlist?id=2884035&userid=1") == ("netease", "2884035")
    assert pimp.parse_input("https://music.163.com/playlist/2884035") == ("netease", "2884035")
    assert pimp.parse_input("https://y.qq.com/n/ryqq/playlist/7358512163") == ("qq", "7358512163")
    assert pimp.parse_input("2884035", platform="netease") == ("netease", "2884035")


def test_parse_input_errors():
    import pytest
    with pytest.raises(ValueError):
        pimp.parse_input("随便一段话")
    with pytest.raises(ValueError):
        pimp.parse_input("")


def test_pick_candidate_prefers_quality_and_title():
    items = [
        {"id": "bilibili:1", "title": "晴 天", "artist": "周杰伦", "ext": "m4a",
         "file_size": 0, "duration_s": 260, "download_url": "http://x/1"},
        {"id": "kuwo:2", "title": "晴天", "artist": "周杰伦/杨瑞代", "ext": "flac",
         "file_size": 30_000_000, "duration_s": 260, "download_url": "http://x/2"},
        {"id": "kuwo:3", "title": "晴天（Live）", "artist": "周杰伦", "ext": "flac",
         "file_size": 30_000_000, "duration_s": 260, "download_url": "http://x/3"},
        {"id": "kuwo:4", "title": "晴天", "artist": "群星", "ext": "flac",
         "file_size": 30_000_000, "duration_s": 260, "download_url": "http://x/4"},
    ]
    best = pimp._pick_candidate(items, "晴天", "周杰伦")
    assert best["id"] == "kuwo:2"  # 标题归一相等 + 歌手重叠 + 无损优先


def test_quality_tier_threshold():
    assert pimp._quality_tier("flac", 0, 0) == 0
    assert pimp._quality_tier("m4a", 0, 0) == 2  # 无大小无时长=未知
    assert pimp._quality_tier("mp3", 12_800_000, 320) == 1
    assert pimp._quality_tier("mp3", 5_120_000, 320) == 2
