"""歌词搜索（3.8.0）：搜索框输入歌词片段也能找到歌。

本地：官方歌词分片库 LIKE 命中 → 官方同构条目注入本地段尾部（total 同步）；
在线：网易公开歌词搜索（type=1006）→ 候选歌名二跳搜 musicdl/lx → 匹配过滤。
"""
import httpx
import pytest
import sqlite3
from fastapi.testclient import TestClient

import proxy.app as p


@pytest.fixture()
def anyio_backend():
    return "asyncio"


# ---------------------------------------------------------------------------
# LRC 行级过滤
# ---------------------------------------------------------------------------

def test_lyric_text_hits_strips_timestamp_and_meta_lines():
    lrc = (
        "[ti:晴天]\n"
        "[by:某人]\n"
        "[00:12.34]故事的小黄花\n"
        "[00:18.50][01:02.00]从出生那年就飘着\n"
    )
    assert p._lyric_text_hits(lrc, "小黄花")
    # 命中在正文但行带多时间戳标签
    assert p._lyric_text_hits(lrc, "从出生那年")
    # 词只出现在 [ti:] 元数据行 → 不算命中
    assert not p._lyric_text_hits(lrc, "晴天")
    # 时间戳数字本身不参与匹配
    assert not p._lyric_text_hits(lrc, "1234")


def test_lyric_text_hits_case_insensitive_and_bytes():
    lrc = "[00:01.00]Hello World\n".encode("utf-8")
    assert p._lyric_text_hits(lrc, "hello world")
    assert not p._lyric_text_hits(b"", "anything")
    assert not p._lyric_text_hits(None, "anything")


# ---------------------------------------------------------------------------
# 本地命中：分片库扫描 + 官方库反查 + 条目构造
# ---------------------------------------------------------------------------

GUID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _make_shard(tmp_path, guid, text):
    d = tmp_path / "lyric-sqlite"
    d.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(d / f"lyric-0{guid[0]}.db"))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS lyric_content "
        "(guid TEXT PRIMARY KEY, content BLOB, created_at DATETIME)"
    )
    conn.execute(
        "REPLACE INTO lyric_content (guid, content, created_at) VALUES (?, ?, datetime('now'))",
        (guid, text.encode("utf-8")),
    )
    conn.commit()
    conn.close()
    return d


def _make_music_db(tmp_path, guid, title="晴天", artist="周杰伦", album="叶惠美"):
    path = tmp_path / "song.mp3"
    path.write_bytes(b"ID3fake-audio")
    db = tmp_path / "music.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE track (id INTEGER PRIMARY KEY, guid TEXT, title TEXT,
            duration_ms INTEGER, cover_guid TEXT, audio_file_id INTEGER,
            album_id INTEGER, is_admin_deleted INTEGER DEFAULT 0,
            is_audio_file_deleted INTEGER DEFAULT 0);
        CREATE TABLE audio_file (id INTEGER PRIMARY KEY, path TEXT);
        CREATE TABLE album (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE artist (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE track_artist (track_id INTEGER, artist_id INTEGER);
        CREATE TABLE lyric (id INTEGER PRIMARY KEY, guid TEXT, track_id INTEGER,
            source INTEGER, stored_guid TEXT, created_at DATETIME, updated_at DATETIME);
        INSERT INTO audio_file (id, path) VALUES (1, '%s');
        INSERT INTO album (id, name) VALUES (1, '%s');
        INSERT INTO artist (id, name) VALUES (1, '%s');
        INSERT INTO track (id, guid, title, duration_ms, cover_guid, audio_file_id, album_id)
            VALUES (1, '%s', '%s', 269000, 'c0000000000000000000000000000000', 1, 1);
        INSERT INTO track_artist (track_id, artist_id) VALUES (1, 1);
        INSERT INTO lyric (id, guid, track_id, source, stored_guid) VALUES (1, 'lg1', 1, 1, '%s');
        """
        % (str(path).replace("\\", "/"), album, artist, guid, title, GUID)
    )
    conn.commit()
    conn.close()
    return db


@pytest.fixture()
def lyric_env(tmp_path, monkeypatch):
    """临时分片库 + 临时官方库 + 保存恢复 CONF 歌词相关项。"""
    lrc = "[00:12.34]故事的小黄花\n[00:18.50]从出生那年就飘着\n"
    _make_shard(tmp_path, GUID, lrc)
    db = _make_music_db(tmp_path, GUID)
    keys = (
        "lyric_search_enabled", "lyric_online_enabled", "lyric_sqlite_dir",
        "lyric_search_min_len", "lyric_search_max_local", "lyric_online_candidates", "music_db",
    )
    saved = {k: p.CONF.get(k) for k in keys}
    p.CONF["lyric_search_enabled"] = True
    p.CONF["lyric_sqlite_dir"] = str(tmp_path / "lyric-sqlite")
    p.CONF["lyric_search_min_len"] = 2
    p.CONF["lyric_search_max_local"] = 20
    p.CONF["music_db"] = str(db)
    yield tmp_path
    p.CONF.update(saved)


def test_search_lyric_local_hits_and_shape(lyric_env):
    hits = p._search_lyric_local("小黄花")
    assert len(hits) == 1
    it = hits[0]
    assert it["guid"] == GUID
    assert it["title"] == "晴天"
    assert it["artist"] == "周杰伦"
    assert it["albumName"] == "叶惠美"
    assert it["duration_ms"] == 269000
    # audioSpec.path 指向真实文件，客户端点击走官方本地播放链路
    assert it["audioSpec"]["path"].endswith("song.mp3")
    assert it["audioSpec"]["size"] > 0
    assert it["hasLyric"] is True
    assert it["is_online"] is False
    assert it["coverId"] == "track_c0000000000000000000000000000000"


def test_search_lyric_local_meta_word_not_hit(lyric_env):
    # 关键词只出现在 [ti:] 标签行（标题"晴天"写进 meta）→ 不误命中
    # 分片内容只有正文两行，搜标题不存在的词应为空
    assert p._search_lyric_local("不存在的歌词句子") == []


def test_search_lyric_local_too_short_keyword(lyric_env):
    p.CONF["lyric_search_min_len"] = 2
    assert p._search_lyric_local("花") == []


def test_search_lyric_local_disabled(lyric_env):
    p.CONF["lyric_search_enabled"] = False
    assert p._search_lyric_local("小黄花") == []


# ---------------------------------------------------------------------------
# search_track 注入接缝：官方 0 条 + 歌词命中 → 本地段含命中条目且 total 同步
# ---------------------------------------------------------------------------

def _setup_search_clients(monkeypatch, online=None):
    p._SEARCH_CACHE.clear()
    monkeypatch.setitem(p.CONF, "musicdl_enabled", False)
    monkeypatch.setitem(p.CONF, "netease_enabled", False)
    monkeypatch.setitem(p.CONF, "lx_enabled", False)
    monkeypatch.setitem(p.CONF, "lyric_search_enabled", True)
    monkeypatch.setitem(p.CONF, "lyric_online_enabled", False)
    monkeypatch.setitem(p.CONF, "search_debounce_s", 0.0)
    p.app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})
        ),
        base_url="http://unix",
    )
    if online is not None:
        p.app.state.musicdl_client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"ok": True, "items": online})
            ),
            base_url="http://musicdl",
        )


def test_search_track_injects_local_lyric_hits(lyric_env, monkeypatch):
    _setup_search_clients(monkeypatch)
    with TestClient(p.app) as client:
        data = client.get("/music/api/v1/search/track?q=小黄花").json()["data"]
    assert len(data["list"]) == 1
    assert data["list"][0]["guid"] == GUID
    assert data["total"] == 1


def test_search_track_lyric_hit_dedup_with_official(lyric_env, monkeypatch):
    """官方按标题已搜到同一首 → 歌词命中不重复注入。"""
    p._SEARCH_CACHE.clear()
    local = [{"guid": GUID, "title": "晴天", "artist": "周杰伦"}]
    p.app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"code": 0, "data": {"list": local, "total": 1}})
        ),
        base_url="http://unix",
    )
    monkeypatch.setitem(p.CONF, "musicdl_enabled", False)
    monkeypatch.setitem(p.CONF, "netease_enabled", False)
    monkeypatch.setitem(p.CONF, "lx_enabled", False)
    monkeypatch.setitem(p.CONF, "lyric_online_enabled", False)
    monkeypatch.setitem(p.CONF, "search_debounce_s", 0.0)
    with TestClient(p.app) as client:
        data = client.get("/music/api/v1/search/track?q=小黄花").json()["data"]
    assert data["total"] == 1
    assert [x["guid"] for x in data["list"]] == [GUID]


# ---------------------------------------------------------------------------
# 在线歌词源：网易候选 → 二跳搜 musicdl → 候选匹配过滤
# ---------------------------------------------------------------------------

NETEASE_RESP = {
    "code": 200,
    "result": {
        "songs": [
            {"id": 1, "name": "晴天", "artists": [{"id": 2, "name": "Lucky小爱"}]},
            {"id": 3, "name": "晴天", "artists": [{"id": 4, "name": "舟子"}]},
        ],
        "songCount": 2,
    },
}


def test_strip_lyric_title_suffix():
    assert p._strip_lyric_title_suffix("晴天(深情版)") == "晴天"
    assert p._strip_lyric_title_suffix("晴天 (女声版))") == "晴天"   # 网易候选带多余右括号
    assert p._strip_lyric_title_suffix("晴天 (翻唱版)") == "晴天"
    assert p._strip_lyric_title_suffix("青花瓷 (Live)") == "青花瓷"
    assert p._strip_lyric_title_suffix("我们的晴天") == "我们的晴天"  # 无括号不动
    assert p._strip_lyric_title_suffix("") == ""


def test_lyric_candidate_match():
    # 候选组：网易歌词搜索给的翻唱候选组（歌名 + 组内歌手并集）
    cand = {"title": "晴天", "artists": ["Lucky小爱", "舟子"]}
    # 标题 + 组内歌手命中 → 强匹配 2
    assert p._lyric_candidate_match({"title": "晴天", "artist": "舟子"}, cand) == 2
    assert p._lyric_candidate_match({"title": "晴天 (Live)", "artist": "Lucky小爱"}, cand) == 2
    # 标题命中但歌手是原版（候选组没有）→ 弱匹配 1（收，排后——原版正是用户想找的）
    assert p._lyric_candidate_match({"title": "晴天", "artist": "周杰伦"}, cand) == 1
    # 标题互含（平台名带括号后缀）+ 无关歌手 → 弱匹配 1
    assert p._lyric_candidate_match({"title": "晴天 (深情版)", "artist": "王大陆"}, cand) == 1
    # 标题完全无关 → 拒
    assert p._lyric_candidate_match({"title": "七里香", "artist": "周杰伦"}, cand) == 0


@pytest.mark.anyio
async def test_fetch_lyric_online_search_filters(monkeypatch):
    monkeypatch.setitem(p.CONF, "lyric_search_enabled", True)
    monkeypatch.setitem(p.CONF, "lyric_online_enabled", True)
    monkeypatch.setitem(p.CONF, "lyric_online_candidates", 2)
    monkeypatch.setitem(p.CONF, "musicdl_enabled", True)
    monkeypatch.setitem(p.CONF, "lx_enabled", False)
    p.app.state.lyric_net_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=NETEASE_RESP)),
        base_url="https://music.163.com",
    )
    # 二跳：同歌名的原版（组外歌手，弱匹配排后）+ 翻唱（组内歌手，强匹配排前）
    # + 完全无关的七里香（拒）。二跳 q 只带歌名不带歌手（请求次数=去重歌名数）
    seen_queries = []

    def musicdl_handler(r):
        seen_queries.append(str(r.url.params.get("keyword")))
        return httpx.Response(200, json={"ok": True, "items": [
            {"id": "kuwo:1", "source": "kuwo", "title": "晴天", "artist": "周杰伦",
             "duration_s": 269, "ext": "mp3"},
            {"id": "kuwo:2", "source": "kuwo", "title": "晴天 (Live)", "artist": "Lucky小爱",
             "duration_s": 280, "ext": "mp3"},
            {"id": "kuwo:3", "source": "kuwo", "title": "七里香", "artist": "周杰伦",
             "duration_s": 300, "ext": "mp3"},
        ]})

    p.app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler),
        base_url="http://musicdl",
    )

    class FakeRequest:
        pass

    req = FakeRequest()
    req.app = p.app
    out = await p.fetch_lyric_online_search(req, "故事的小黄花")
    # 两个同名候选合并成一组 → 只发一次二跳（q=歌名）
    assert seen_queries == ["晴天"]
    # 强匹配（组内歌手）在前，弱匹配（原版）在后
    assert [x["id"] for x in out] == ["kuwo:2", "kuwo:1"]


@pytest.mark.anyio
async def test_fetch_lyric_online_search_netease_down(monkeypatch):
    """网易歌词接口挂掉 → 歌词源静默返回空，不影响主搜索。"""
    monkeypatch.setitem(p.CONF, "lyric_search_enabled", True)
    monkeypatch.setitem(p.CONF, "lyric_online_enabled", True)
    monkeypatch.setitem(p.CONF, "musicdl_enabled", True)
    monkeypatch.setitem(p.CONF, "lx_enabled", False)
    p.app.state.lyric_net_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(503)),
        base_url="https://music.163.com",
    )

    class FakeRequest:
        pass

    req = FakeRequest()
    req.app = p.app
    assert await p.fetch_lyric_online_search(req, "随便什么词") == []
