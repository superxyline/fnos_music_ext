"""enhance_search（QQ/酷狗源接入）单测：mock 平台函数，不触网。"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import match_core  # noqa: E402
import enhance_search.platforms as enhance_registry  # noqa: E402

QQ_SONG = {
    "id": "97773", "title": "晴天", "artist": "周杰伦", "album": "叶惠美",
    "duration": 269000, "date": "", "trackNumber": "", "discNumber": "",
    "picUrl": "https://y.gtimg.cn/music/photo_new/T002R800x800M000abc.jpg",
    "fields": {}, "internal": {"qq_id": "97773", "songmid": "0039MnYb0qxYhV",
                               "albummid": "abc"},
}

KG_SONG = {
    "id": "5148138", "title": "晴天", "artist": "周杰伦", "album": "叶惠美",
    "duration": 269000, "date": "", "trackNumber": "", "discNumber": "",
    "picUrl": "https://imge.kugou.com/stdmusic/480/abc.jpg",
    "fields": {}, "internal": {"hash": "abc123", "filename": "周杰伦 - 晴天"},
}

QRC_LINES = [
    [0, 5000, "故事的小黄花"],
    [5000, 9000, "从出生那年就飘着"],
]


@pytest.fixture()
def clear_source_env(monkeypatch):
    for key in ("FNMUSIC_QQ_ENABLED", "FNMUSIC_KUGOU_ENABLED",
                "FNMUSIC_MUSICDL_ENABLED", "FNMUSIC_NETEASE_ENABLED",
                "FNMUSIC_LX_ENABLED"):
        monkeypatch.delenv(key, raising=False)


def test_enabled_sources_default_qq_kugou_first(clear_source_env):
    assert match_core._enabled_sources() == ["qq", "kugou"]


def test_enabled_sources_env_toggle(clear_source_env, monkeypatch):
    monkeypatch.setenv("FNMUSIC_QQ_ENABLED", "false")
    monkeypatch.setenv("FNMUSIC_LX_ENABLED", "true")
    assert match_core._enabled_sources() == ["kugou", "lx"]


def test_structured_to_lrc():
    text = match_core._structured_to_lrc(QRC_LINES)
    assert text == "[00:00.00]故事的小黄花\n[00:05.00]从出生那年就飘着"


def test_structured_to_lrc_word_payload():
    lines = [[1000, 3000, [[1000, 1500, "你"], [1500, 3000, "好"]]]]
    assert match_core._structured_to_lrc(lines) == "[00:01.00]你好"


def test_search_qq_maps_candidate(monkeypatch):
    monkeypatch.setattr(enhance_registry.SOURCE_REGISTRY["qq"]["impl"],
                        "search_songs", lambda kw, p=1, ps=20, timeout=None: [QQ_SONG])
    out = asyncio.run(match_core._search_qq(None, "晴天 周杰伦"))
    assert len(out) == 1
    cand = out[0]
    assert cand["source"] == "qq" and cand["id"] == "97773"
    assert cand["title"] == "晴天" and cand["artist"] == "周杰伦"
    assert cand["cover_url"].startswith("https://y.gtimg.cn/")
    assert cand["internal"]["songmid"] == "0039MnYb0qxYhV"


def test_search_kugou_maps_candidate(monkeypatch):
    monkeypatch.setattr(enhance_registry.SOURCE_REGISTRY["kugou"]["impl"],
                        "search_songs", lambda kw, p=1, ps=20, timeout=None: [KG_SONG])
    out = asyncio.run(match_core._search_kugou(None, "晴天 周杰伦"))
    assert out[0]["source"] == "kugou"
    assert out[0]["internal"]["hash"] == "abc123"


def test_fetch_detail_qq(monkeypatch):
    monkeypatch.setattr(enhance_registry.SOURCE_REGISTRY["qq"]["impl"],
                        "get_lyrics",
                        lambda song, timeout=None: {"original": QRC_LINES})
    cand = match_core._map_enhance_song(QQ_SONG, "qq")
    detail = asyncio.run(match_core.fetch_candidate_detail(None, cand))
    assert detail["lyric"] == "[00:00.00]故事的小黄花\n[00:05.00]从出生那年就飘着"
    assert detail["cover_url"].startswith("https://y.gtimg.cn/")


def test_fetch_detail_kugou(monkeypatch):
    monkeypatch.setattr(enhance_registry.SOURCE_REGISTRY["kugou"]["impl"],
                        "get_lyrics",
                        lambda song, timeout=None: {"original": QRC_LINES})
    cand = match_core._map_enhance_song(KG_SONG, "kugou")
    detail = asyncio.run(match_core.fetch_candidate_detail(None, cand))
    assert detail["lyric"].startswith("[00:00.00]")
    assert detail["cover_url"].startswith("https://imge.kugou.com/")


def test_fetch_detail_qq_lyric_failure_keeps_cover(monkeypatch):
    def boom(song, timeout=None):
        raise RuntimeError("qrc down")

    monkeypatch.setattr(enhance_registry.SOURCE_REGISTRY["qq"]["impl"],
                        "get_lyrics", boom)
    cand = match_core._map_enhance_song(QQ_SONG, "qq")
    detail = asyncio.run(match_core.fetch_candidate_detail(None, cand))
    assert detail["lyric"] == ""
    assert detail["cover_url"].startswith("https://y.gtimg.cn/")
