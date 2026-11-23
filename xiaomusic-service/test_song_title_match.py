"""小爱点歌「只匹配歌曲名」回归测试（2026-11-23 用户反馈）。

现象：点「陶喆的就是爱你」→ 先响小米试听，几秒后播成张杰《这就是爱》。
根因：官方 /search/track 是歌名+歌手+歌词全文匹配，歌词撞词的本地条目
（《这就是爱》歌词含「这就是爱」）被旧逻辑按 difflib 相似度选中——两个歌名
相似度正好 0.75，且当时没有任何歌名校验。现改为歌名级确定性匹配：
  只认歌名 → 本地优先 → 本地没有才走在线源 → 都对不上就 TTS 没找到。
手机 App / 网页端搜索走官方接口，不经过本模块，不受影响。
"""
import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_MOD = None


def _bridge():
    """按文件路径加载桥接 app 模块（避免 import app 命中 webui-service/app.py）。"""
    global _MOD
    if _MOD is None:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        spec = importlib.util.spec_from_file_location("fnmusic_bridge_song_match", HERE / "app.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MOD = mod
    return _MOD


@pytest.fixture
def m(monkeypatch):
    """每个用例拿到干净状态：无引擎、无客户端、空收藏。"""
    mod = _bridge()
    monkeypatch.setattr(mod, "XM", None)
    monkeypatch.setattr(mod.STATE, "fn", None)
    monkeypatch.setattr(mod.STATE, "favorites", [])
    return mod


def _track(guid, title, artist):
    return {"guid": guid, "title": title, "artists": [{"name": artist}]}


def _fake_fn(tracks_by_kw):
    """假飞牛客户端：按关键词返回预设搜索结果，记录调用参数。"""

    class _Fn:
        token = "test-token"

        def __init__(self):
            self.calls = []

        async def login(self, force=False):
            return self.token

        async def search_track(self, keyword, page_size=20):
            self.calls.append((keyword, page_size))
            return list(tracks_by_kw.get(keyword, []))

    return _Fn()


# ---------------- 歌名判定 ----------------


def test_title_hit_accepts_artist_prefix(m):
    """「歌手 + 的 + 歌名」口令必须命中歌名本身。"""
    assert m._title_hit("陶喆的就是爱你", "就是爱你")
    assert m._title_hit("周杰伦的晴天", "晴天")
    assert m._title_hit("陶喆 就是爱你", "就是爱你")
    assert m._title_hit("就是爱你", "就是爱你")


def test_title_hit_rejects_lyric_neighbor(m):
    """歌词撞词的邻近歌名不得命中（用户反馈的核心回归）。"""
    assert not m._title_hit("陶喆的就是爱你", "这就是爱")
    assert not m._title_hit("就是爱你", "这就是爱")
    assert not m._title_hit("周杰伦的晴天", "晴天娃娃")
    # 注：口令「晴天」本身包含在「晴天娃娃」里，属于可接受的宽松命中；
    # 它不会抢在完全相等的《晴天》前面（由 _match_favorite 的排序保证）。


def test_title_hit_version_suffix(m):
    """版本后缀（Live/伴奏等）不影响命中。"""
    assert m._title_hit("就是爱你", "就是爱你 (Live)")
    assert m._title_hit("就是爱你", "就是爱你（伴奏）")


def test_title_hit_strips_source_tag(m):
    """本地曲库标题带音源前缀（真机实测「[kuwo]就是爱你」），必须剥掉再比。"""
    assert m._norm_title("[kuwo]就是爱你") == "就是爱你"
    assert m._norm_title("[kuwo]就是爱你 (Live)") == "就是爱你"
    assert m._title_hit("陶喆的就是爱你", "[kuwo]就是爱你")
    assert m._title_hit("陶喆的就是爱你", "[kuwo]就是爱你 (Live)")
    # 剥掉来源标签后，歌词撞词条目依然不命中
    assert not m._title_hit("陶喆的就是爱你", "[kuwo]这,就是爱")


def test_norm_title_strips_punctuation(m):
    """官方歌名带标点（真机实测「这,就是爱」），口令不带标点也要能命中。"""
    assert m._norm_title("这,就是爱") == "这就是爱"
    assert m._title_hit("张杰的这就是爱", "这,就是爱")
    assert not m._title_hit("陶喆的就是爱你", "这,就是爱")


def test_title_hit_single_char_title_requires_exact(m):
    """单字歌名只接受完全相等，避免「爱」撞进任何长口令。"""
    assert m._title_hit("爱", "爱")
    assert not m._title_hit("就是爱你", "爱")


# ---------------- 收藏匹配 ----------------


def test_match_favorite_only_by_title(m):
    """收藏匹配只认歌名：歌词/歌手相似不命中。"""
    m.STATE.favorites = [
        _track("g1", "这就是爱", "张杰"),
        _track("g2", "就是爱你", "陶喆"),
    ]
    assert m._match_favorite("陶喆的就是爱你") == "陶喆 - 就是爱你"
    assert m._match_favorite("王力宏的这就是爱") == "张杰 - 这就是爱"
    assert m._match_favorite("稻香") is None


def test_match_favorite_prefers_exact_title(m):
    """多个候选时完全相等的歌名优先（《晴天》不能被《晴天娃娃》抢走）。"""
    m.STATE.favorites = [
        _track("g1", "晴天娃娃", "周杰伦"),
        _track("g2", "晴天", "周杰伦"),
    ]
    assert m._match_favorite("周杰伦的晴天") == "周杰伦 - 晴天"


def test_artist_favorite_pick_exact_only(m):
    """B 方案例外：口令恰好等于歌手名才随机播该歌手收藏，不做包含匹配。"""
    m.STATE.favorites = [
        _track("g1", "这就是爱", "张杰"),
        _track("g2", "就是爱你", "陶喆"),
    ]
    assert m._artist_favorite_pick("陶喆") == "陶喆 - 就是爱你"
    assert m._artist_favorite_pick("张杰") == "张杰 - 这就是爱"
    assert m._artist_favorite_pick("陶") is None
    assert m._artist_favorite_pick("林俊杰") is None


# ---------------- 搜索解析：本地优先 / 拒绝歌词撞词 ----------------


def test_resolve_song_skips_lyric_neighbor_and_uses_online(m, monkeypatch):
    """点「陶喆的就是爱你」：本地《这就是爱》被拒，改播在线同名歌。"""
    fn = _fake_fn({
        "陶喆的就是爱你": [
            _track("g-local", "这就是爱", "张杰"),
            _track("online:abc", "就是爱你", "陶喆"),
        ],
        "陶喆 就是爱你": [],
    })
    monkeypatch.setattr(m.STATE, "fn", fn)
    monkeypatch.setattr(m, "_inject_to_playlist", lambda name, guid: name)
    downloaded = []

    async def _download(track):
        downloaded.append(track.get("guid"))
        return True

    monkeypatch.setattr(m, "_download_online_track", _download)

    mode, name = asyncio.run(m._resolve_song("陶喆的就是爱你"))
    assert (mode, name) == ("downloaded", "陶喆 - 就是爱你")
    assert downloaded == ["online:abc"]
    assert fn.calls[0][1] == 50  # 搜索窗口放宽到 50


def test_resolve_song_prefers_local_title_hit(m, monkeypatch):
    """本地同名歌优先于在线源（在线结果排前面也不影响）。"""
    fn = _fake_fn({
        "陶喆的就是爱你": [
            _track("online:abc", "就是爱你", "陶喆"),
            _track("g-local", "就是爱你", "陶喆"),
        ],
        "陶喆 就是爱你": [],
    })
    monkeypatch.setattr(m.STATE, "fn", fn)
    monkeypatch.setattr(m, "_inject_to_playlist", lambda name, guid: name if guid else "")

    mode, name = asyncio.run(m._resolve_song("陶喆的就是爱你"))
    assert (mode, name) == ("ready", "陶喆 - 就是爱你")


def test_resolve_song_prefers_studio_over_live(m, monkeypatch):
    """同歌手同歌名时录音室版优先（真机实测 Live 版排在搜索结果前面）。"""
    fn = _fake_fn({
        "陶喆的就是爱你": [
            _track("g-live", "[kuwo]就是爱你 (Live)", "陶喆"),
            _track("g-studio", "[kuwo]就是爱你", "陶喆"),
        ],
        "陶喆 就是爱你": [],
    })
    monkeypatch.setattr(m.STATE, "fn", fn)
    picked = []
    monkeypatch.setattr(m, "_inject_to_playlist", lambda name, guid: picked.append(guid) or name)

    mode, name = asyncio.run(m._resolve_song("陶喆的就是爱你"))
    assert mode == "ready"
    assert picked == ["g-studio"]
    assert name == "陶喆 - [kuwo]就是爱你"


def test_resolve_song_no_title_hit_returns_none(m, monkeypatch):
    """只有歌词撞词结果时宁可不播，不再播错歌。"""
    fn = _fake_fn({
        "陶喆的就是爱你": [_track("g-local", "这就是爱", "张杰")],
        "陶喆 就是爱你": [],
    })
    monkeypatch.setattr(m.STATE, "fn", fn)
    monkeypatch.setattr(m, "_inject_to_playlist", lambda name, guid: name)

    assert asyncio.run(m._resolve_song("陶喆的就是爱你")) == (None, None)


def test_resolve_song_artist_weights_order(m, monkeypatch):
    """歌名相同（翻唱）时，口令里的歌手优先。"""
    fn = _fake_fn({
        "就是爱你 陶喆": [
            _track("online:other", "就是爱你", "王菲"),
            _track("online:tao", "就是爱你", "陶喆"),
        ],
    })
    monkeypatch.setattr(m.STATE, "fn", fn)
    monkeypatch.setattr(m, "_inject_to_playlist", lambda name, guid: name)
    picked = []

    async def _download(track):
        picked.append(track.get("guid"))
        return True

    monkeypatch.setattr(m, "_download_online_track", _download)

    mode, _ = asyncio.run(m._resolve_song("就是爱你 陶喆"))
    assert mode == "downloaded"
    assert picked == ["online:tao"]
