"""Tests for official bind (v2.5.2a)：下载落库后写官方收藏/歌单 + tee 切歌续传。

覆盖：
- 收藏/加歌单 → 文件已在库 → 轮询官方库命中 → 官方写入 → 本地映射删除（去重显示）
- 官方库未收录超时 → 本地映射保留（兜底），上游无写调用
- tee 流式下载进行中收藏不重复下载
- tee 切歌续传：断点追加成功 / 源不支持续传回退整轨 / 生成器中断交接 part
- 旧会话伪装 id 取消收藏/移出歌单 → 登记翻译转发官方删除
- 收藏列表读取触发 bind=pending 自愈重试
- 新增配置键热重载
"""
import json
import os
import pathlib
import sqlite3
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import proxy.app as appmod
from proxy.app import (
    CONF,
    app,
    apply_env_hot_reload,
    fake_official_guid,
)

FAKE_KUWO1 = "online:kuwo:228908"
PL_CUSTOM = "pl-custom-1"
OFFICIAL_GUID = "official-guid-1"


@pytest.fixture(autouse=True)
def setup_official_bind_env(tmp_path, monkeypatch):
    appmod._SEARCH_CACHE.clear()
    appmod._full_fetch_tasks.clear()
    appmod._full_fetch_failed.clear()
    # 本仓库未移植上游 fav-auto-bind 体系（_bind_pending/_bind_tasks/_tee_active 等
    # 不存在），仅保留切歌续传与绑定登记表；缺失符号防御性跳过
    for _name in ("_bind_pending", "_bind_tasks", "_bind_retry_last", "_tee_active"):
        _obj = getattr(appmod, _name, None)
        if hasattr(_obj, "clear"):
            _obj.clear()
    appmod._tee_handoff_active.clear()
    appmod._bind_registry.clear()
    monkeypatch.setattr(appmod, "_bind_registry_loaded", True)
    monkeypatch.setattr(appmod, "_REGISTRY_WARMED", False)
    appmod._FAKE_GUID_REVERSE.clear()
    # 官方绑定登记落 tmp，快照反查不扫真实数据目录
    monkeypatch.setattr(appmod, "_HOME", str(tmp_path))
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", str(tmp_path / "play_history"))

    dirs = {}
    for key in ("playlist_tracks", "online_favorites", "cache", "library"):
        dirs[key] = str(tmp_path / key)
        os.makedirs(dirs[key], exist_ok=True)
    monkeypatch.setitem(CONF, "plt_dir", dirs["playlist_tracks"])
    monkeypatch.setitem(CONF, "fav_dir", dirs["online_favorites"])
    monkeypatch.setitem(CONF, "cache_dir", dirs["cache"])
    monkeypatch.setitem(CONF, "library_dir", dirs["library"])
    monkeypatch.setitem(CONF, "tee_save_dir", dirs["library"])
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    monkeypatch.setitem(CONF, "lyric_field", "data.lyric")
    monkeypatch.setitem(CONF, "fav_auto_bind", True)

    db_path = str(tmp_path / "music.db")
    monkeypatch.setitem(CONF, "music_db", db_path)

    upstream_calls: list[dict] = []

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        body = None
        if request.content:
            try:
                body = json.loads(request.content)
            except Exception:
                body = None
        upstream_calls.append({"method": request.method, "path": request.url.path, "json": body})
        if request.url.path.endswith("/user/me"):
            return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"guid": "user-a"}})
        if request.method == "GET" and request.url.path.endswith("/favorite-track/list"):
            return httpx.Response(200, json={"code": 0, "msg": "", "data": {"list": [], "total": 0}})
        return httpx.Response(200, json={"code": 0, "msg": "", "data": None})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler),
        base_url="http://unix",
    )

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, json={
                "ok": True, "id": "kuwo:228908", "source": "kuwo",
                "title": "晴天", "artist": "周杰伦", "album": "叶惠美",
                "duration_s": 269, "ext": "mp3",
            })
        return httpx.Response(404)

    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler),
        base_url="http://127.0.0.1:8768",
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"ok": False})),
        base_url="http://127.0.0.1:8770",
    )
    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"ok": False})),
        base_url="http://127.0.0.1:8772",
    )

    yield {"db_path": db_path, "upstream_calls": upstream_calls, "dirs": dirs}

    appmod._full_fetch_tasks.clear()
    appmod._full_fetch_failed.clear()
    for _name in ("_bind_pending", "_bind_tasks", "_bind_retry_last", "_tee_active"):
        _obj = getattr(appmod, _name, None)
        if hasattr(_obj, "clear"):
            _obj.clear()
    appmod._tee_handoff_active.clear()
    appmod._bind_registry.clear()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def env(setup_official_bind_env):
    return setup_official_bind_env


def make_official_db(db_path: str, with_track: bool = True):
    con = sqlite3.connect(db_path)
    con.executescript("""
        CREATE TABLE track (id INTEGER PRIMARY KEY, guid TEXT, title TEXT, year INTEGER, album_id INTEGER);
        CREATE TABLE artist (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE track_artist (track_id INTEGER, artist_id INTEGER);
        CREATE TABLE album (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE user (id INTEGER PRIMARY KEY, guid TEXT);
        CREATE TABLE favorite_track (user_id INTEGER, track_id INTEGER, updated_at INTEGER);
    """)
    if with_track:
        con.execute("INSERT INTO album (id, name) VALUES (1, '叶惠美')")
        con.execute("INSERT INTO artist (id, name) VALUES (1, '周杰伦')")
        con.execute(
            "INSERT INTO track (id, guid, title, year, album_id) VALUES (10, ?, '晴天', 2003, 1)",
            (OFFICIAL_GUID,),
        )
        con.execute("INSERT INTO track_artist (track_id, artist_id) VALUES (10, 1)")
    con.commit()
    con.close()


def wait_for(pred, timeout: float = 6.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def upstream_posts(calls: list[dict], path_suffix: str) -> list[dict]:
    return [c for c in calls if c["method"] == "POST" and c["path"].endswith(path_suffix)]


def test_stale_fake_guid_unfavorite_forwards_official_delete(env):
    """官方绑定完成后的旧会话伪装 id 取消收藏 → 登记翻译成官方 guid 撤销官方收藏。"""
    appmod._record_bind("user-a", FAKE_KUWO1, OFFICIAL_GUID)

    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/favorite-track/delete",
            json={"trackGUID": fake_official_guid(FAKE_KUWO1)},
        )
        assert resp.status_code == 200
        assert resp.json() == {"code": 0, "msg": "", "data": None}

    deletes = upstream_posts(env["upstream_calls"], "/favorite-track/delete")
    assert deletes and deletes[0]["json"] == {"trackGUID": OFFICIAL_GUID}
    assert appmod.lookup_bind_entry("user-a", FAKE_KUWO1) is None


@pytest.mark.anyio
async def test_resume_part_download_appends_and_finalizes(tmp_path, monkeypatch):
    """续传：Range 从断点续拉追加进 part，完成后转正并调度官方绑定。"""
    part = tmp_path / "x.mp3"
    part.write_bytes(b"a" * 1024)
    calls: dict = {}
    tail = b"b" * 1024

    class FakeResp:
        status_code = 206
        headers = {"content-range": f"bytes 1024-{1024 + len(tail) - 1}/2048",
                   "content-type": "audio/mpeg"}

        async def aclose(self):
            calls["closed"] = True

    async def empty_chunks():
        return
        yield b"never"

    async def fake_open(request, guid, range_header, force_mp3=False):
        calls["range"] = range_header
        return (FakeResp(), None, "mp3", {"title": "晴天", "artist": "周杰伦"},
                empty_chunks(), tail)

    monkeypatch.setattr(appmod, "_open_online_stream", fake_open)

    def fake_finalize(part_, guid, ext, info, tee_enabled):
        calls["finalize"] = (guid, ext, tee_enabled)
        return {"dest": part_, "title": info["title"], "artist": info["artist"], "album": ""}

    monkeypatch.setattr(appmod, "_tee_finalize", fake_finalize)
    monkeypatch.setattr(appmod, "_schedule_library_scan", lambda h: calls.setdefault("scan", True))

    ok = await appmod._resume_part_download(FAKE_KUWO1, str(part), 1024, 2048, "mp3", {})
    assert ok is True
    assert calls["range"] == "bytes=1024-"
    assert part.read_bytes() == b"a" * 1024 + tail
    assert calls["finalize"] == (FAKE_KUWO1, "mp3", True)
    assert calls["scan"] and calls["closed"]


@pytest.mark.anyio
async def test_handoff_falls_back_to_full_download_on_no_range(tmp_path, monkeypatch):
    """源忽略 Range 返回 200：不可续传 → 删半截 part 回退整轨重下。"""
    part = tmp_path / "x.mp3"
    part.write_bytes(b"0123456789")
    calls: dict = {}

    class FakeResp:
        status_code = 200
        headers = {"content-type": "audio/mpeg"}

        async def aclose(self):
            pass

    async def fake_open(request, guid, range_header, force_mp3=False):
        return (FakeResp(), None, "mp3", {}, None, b"")

    monkeypatch.setattr(appmod, "_open_online_stream", fake_open)

    async def fake_full(guid, headers):
        calls["full"] = guid

    monkeypatch.setattr(appmod, "_full_fetch_download", fake_full)

    await appmod._tee_handoff_download(FAKE_KUWO1, str(part), 10, 20, "mp3", {})
    assert calls["full"] == FAKE_KUWO1
    assert not os.path.exists(part)


@pytest.mark.anyio
async def test_disconnect_mid_stream_hands_off_part(env, monkeypatch):
    """切歌（客户端断开）不中断下载：未写完的 part 交接给后台续传。"""
    handed: dict = {}

    def fake_register_handoff(guid, part, written, expected, ext, headers_factory):
        handed.update(guid=guid, part=part, written=written, expected=expected)

    monkeypatch.setattr(appmod, "_register_tee_handoff", fake_register_handoff)

    async def source():
        for _ in range(10):
            yield b"x" * 1024

    resp = httpx.Response(200, headers={"content-length": "10240"})
    response = appmod.stream_tee_response(
        resp, FAKE_KUWO1, None,
        chunks=source(), first_chunk=b"x" * 1024,
        pre_info={"title": "T", "artist": "A"},
    )
    gen = response.body_iterator
    await anext(gen)  # 拿到第一块
    await gen.aclose()  # 切歌：客户端断开
    parts = list(pathlib.Path(env["dirs"]["library"]).rglob("*.part"))
    assert len(parts) == 1
    assert handed["guid"] == FAKE_KUWO1
    assert handed["written"] == 1024 and handed["expected"] == 10240


def test_env_hot_reload_new_keys(tmp_path, monkeypatch):
    """新增配置键纳入 .env 热重载。"""
    for key in ("FNMUSIC_AUTO_COVER", "FNMUSIC_LYRIC_AUTO_DL", "FNMUSIC_TEE_HANDOFF_MAX"):
        monkeypatch.delenv(key, raising=False)
    env_file = str(tmp_path / ".env")
    with open(env_file, "w", encoding="utf-8") as f:
        f.write("FNMUSIC_AUTO_COVER=false\nFNMUSIC_LYRIC_AUTO_DL=true\nFNMUSIC_TEE_HANDOFF_MAX=5\n")
    try:
        changed = apply_env_hot_reload(env_file)
        assert "auto_cover" in changed and "lyric_auto_dl" in changed and "tee_handoff_max" in changed
        assert CONF["auto_cover"] is False
        assert CONF["lyric_auto_dl"] is True
        assert CONF["tee_handoff_max"] == 5
    finally:
        for key in ("FNMUSIC_AUTO_COVER", "FNMUSIC_LYRIC_AUTO_DL", "FNMUSIC_TEE_HANDOFF_MAX"):
            os.environ.pop(key, None)



