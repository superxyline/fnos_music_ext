"""搜索歌词前缀（2026-11-23 用户需求）。

需求：
  1. 搜索框直接输入内容 → 只按歌名/歌手搜，不再通过歌词撞词；
  2. 输入「搜歌词 xxx」→ 才按歌词搜 xxx。

实现要点：前缀词在网关剥离（不能透传给官方，否则官方按整串搜不到），
歌词搜索（本地分片库 + 在线网易歌词源）只在歌词模式下启用。
"""
import httpx
import pytest
from fastapi.testclient import TestClient

import proxy.app as p


# ---------------- 关键词拆分 ----------------

@pytest.mark.parametrize("raw,expect_kw,expect_mode", [
    ("某人", "某人", False),
    ("汪苏泷 某人", "汪苏泷 某人", False),
    ("搜歌词 某人", "某人", True),
    ("搜歌词某人", "某人", True),
    ("搜歌词：某人", "某人", True),
    ("按歌词搜 某人", "某人", True),
    ("歌词搜索 某人", "某人", True),
    ("  搜歌词   某人  ", "某人", True),
    ("搜歌词", "搜歌词", False),      # 只有前缀：按普通词搜，避免空结果
    ("", "", False),
])
def test_split_lyric_keyword(raw, expect_kw, expect_mode):
    assert p.split_lyric_keyword(raw) == (expect_kw, expect_mode)


def test_split_lyric_keyword_legacy_switch(monkeypatch):
    """开关关闭时恢复 3.8.0 旧行为：任何关键词都启用歌词搜索。"""
    monkeypatch.setitem(p.CONF, "lyric_prefix_required", False)
    monkeypatch.setitem(p.CONF, "lyric_search_enabled", True)
    assert p.split_lyric_keyword("某人") == ("某人", True)


# ---------------- 上游查询串改写 ----------------

def test_rewrite_query_keyword_keeps_paging():
    q = "q=%E6%90%9C%E6%AD%8C%E8%AF%8D%20%E6%9F%90%E4%BA%BA&page=1&size=50"
    out = p.rewrite_query_keyword(q, "某人")
    assert "page=1" in out and "size=50" in out
    assert "%E6%9F%90%E4%BA%BA" in out
    assert "%E6%90%9C%E6%AD%8C%E8%AF%8D" not in out  # 「搜歌词」不再出现


def test_rewrite_query_keyword_keyword_param():
    out = p.rewrite_query_keyword("keyword=abc&page=2", "某人")
    assert "page=2" in out
    assert "keyword=" in out and "abc" not in out


# ---------------- 集成：前缀剥离 + 歌词注入时机 ----------------

def _client_with_upstream(seen):
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})

    p._SEARCH_CACHE.clear()
    p.app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://unix")
    return TestClient(p.app)


@pytest.fixture
def no_online(monkeypatch):
    for k in ("musicdl_enabled", "netease_enabled", "lx_enabled", "musicbox_enabled"):
        monkeypatch.setitem(p.CONF, k, False)
    monkeypatch.setitem(p.CONF, "search_debounce_s", 0.0)
    monkeypatch.setitem(p.CONF, "lyric_prefix_required", True)
    monkeypatch.setitem(p.CONF, "lyric_search_enabled", True)


def test_plain_search_does_not_touch_lyrics(monkeypatch, no_online):
    """普通搜索：不注入歌词，上游收到原词。"""
    calls = []
    monkeypatch.setattr(p, "_inject_lyric_local_hits", lambda lst, kw: calls.append(kw) or 0)
    seen = []
    client = _client_with_upstream(seen)
    client.get("/music/api/v1/search/track", params={"q": "某人"})
    assert calls == [], "普通搜索不得注入歌词"
    assert any("%E6%9F%90%E4%BA%BA" in u for u in seen), seen


def test_lyric_prefix_triggers_lyric_search_and_strips_prefix(monkeypatch, no_online):
    """「搜歌词 某人」：歌词搜索启用，上游只收到「某人」。"""
    calls = []
    monkeypatch.setattr(p, "_inject_lyric_local_hits", lambda lst, kw: calls.append(kw) or 0)
    seen = []
    client = _client_with_upstream(seen)
    client.get("/music/api/v1/search/track", params={"q": "搜歌词 某人"})
    assert calls == ["某人"], f"歌词模式关键词应已剥离: {calls}"
    # 上游请求里不能出现「搜歌词」整串
    assert seen
    assert all("搜歌词" not in u and "%E6%90%9C%E6%AD%8C%E8%AF%8D" not in u for u in seen), seen


def test_settings_switch_overrides_prefix(monkeypatch, no_online):
    """设置里关掉「歌词搜索」后，写「搜歌词 xxx」也不得搜歌词。

    开关 = FNMUSIC_LYRIC_SEARCH_ENABLED（管理台「搜索」分组，hot reload）。
    """
    monkeypatch.setitem(p.CONF, "lyric_search_enabled", False)
    calls = []
    monkeypatch.setattr(p, "_inject_lyric_local_hits", lambda lst, kw: calls.append(kw) or 0)
    seen = []
    client = _client_with_upstream(seen)
    client.get("/music/api/v1/search/track", params={"q": "搜歌词 某人"})
    assert calls == [], "开关关闭时不得触发歌词搜索"
    # 前缀仍会被剥离（否则官方按整串搜不到），按普通词返回
    assert seen and all("搜歌词" not in u and "%E6%90%9C%E6%AD%8C%E8%AF%8D" not in u for u in seen), seen


def test_lyric_local_search_guards_switch(monkeypatch):
    """本地歌词扫描函数本身也守开关（双保险）。"""
    monkeypatch.setitem(p.CONF, "lyric_search_enabled", False)
    assert p._search_lyric_local("某人") == []
