"""publicplaylists 酷狗榜单 provider 纯逻辑测试（Windows 本机可跑）。

不触网：只测榜单清单一致性、selection 校验、_kg_pick 同名优选。
"""
import importlib.util
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PROXY = _HERE.parent

spec = importlib.util.spec_from_file_location("publicplaylists_kg", _PROXY / "publicplaylists.py")
pubpl = importlib.util.module_from_spec(spec)
sys.modules["publicplaylists_kg"] = pubpl
spec.loader.exec_module(pubpl)


def test_kg_charts_consistent():
    """榜单清单：id 唯一、rankid 可转 int、name/cover 齐全。"""
    ids = [str(c["id"]) for c in pubpl.KG_CHARTS]
    assert len(ids) == len(set(ids)) and len(ids) >= 15
    for c in pubpl.KG_CHARTS:
        int(c["rankid"])
        assert c["name"] and str(c.get("cover") or "").startswith("https://")
    assert pubpl._KG_CHART_BY_ID["8888"]["name"] == "TOP500"


def test_parse_selection_accepts_kg_chart_only():
    """provider=kugou 只收内置清单内的 chart 条目，非法 kind/未知 id 丢弃。"""
    import json

    rows = json.dumps([
        {"provider": "kugou", "source": "kg", "kind": "chart", "id": "8888"},
        {"provider": "kugou", "source": "kg", "kind": "playlist", "id": "8888"},  # kind 不符
        {"provider": "kugou", "source": "kg", "kind": "chart", "id": "9999999"},  # 不在清单
        {"provider": "musicbox", "source": "netease", "kind": "chart", "id": "3778678"},
    ])
    out = pubpl.parse_selection(rows, {})
    assert [e["provider"] for e in out] == ["kugou", "musicbox"]
    assert out[0]["id"] == "8888"


def test_kg_pick_prefers_lossless_and_matching_artist():
    items = [
        {"id": "bilibili:1", "title": "晴 天", "artist": "群星", "ext": "m4a",
         "file_size": 0, "duration_s": 260, "download_url": "http://x/1"},
        {"id": "kuwo:2", "title": "晴天", "artist": "周杰伦/杨瑞代", "ext": "flac",
         "file_size": 30_000_000, "duration_s": 260, "download_url": "http://x/2"},
        {"id": "kuwo:3", "title": "晴天（Live）", "artist": "周杰伦", "ext": "flac",
         "file_size": 30_000_000, "duration_s": 260, "download_url": "http://x/3"},
        {"id": "migu:4", "title": "晴天", "artist": "周杰伦", "ext": "mp3",
         "file_size": 5_120_000, "duration_s": 260, "download_url": "http://x/4"},
    ]
    best = pubpl._kg_pick(items, "晴天", "周杰伦")
    assert best and best["id"] == "kuwo:2"  # 无损优先 + 歌手重叠


def test_kg_pick_rejects_mismatch():
    assert pubpl._kg_pick([
        {"id": "kuwo:1", "title": "晴天", "artist": "群星", "ext": "flac",
         "file_size": 1, "duration_s": 1, "download_url": "http://x"},
    ], "晴天", "周杰伦") is None  # 歌手完全不含
