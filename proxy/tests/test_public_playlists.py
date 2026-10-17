"""公共歌单注入、曲目缓存与只读保护测试（publicplaylists，issue #55）。

覆盖：选择串解析/默认值/去重、guid 编解码、未鉴权不出内容、注入顺序与封面伪装、
只读吸收、发现失败不影响官方列表、失败冷却、磁盘快照跨重启恢复与假 id 反查重建。
"""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy import publicplaylists as pubpl
import proxy.app as proxy_app
from proxy.app import CONF, app, ensure_registry_warm, fake_official_guid, resolve_real_guid


SELECTION = [
    {"provider": "musicbox", "source": "netease", "kind": "chart", "id": "3778678"},
]
SUMMARY = {
    "id": "3778678", "kind": "chart", "source": "netease", "name": "热歌榜",
    "cover_url": "http://img.test/hot.jpg", "track_count": 2, "author": "网易",
    "description": "", "updated_at": 1760000000,
}
TRACKS = [
    {"song_id": 101, "name": "晴天", "artist": "周杰伦", "album_name": "叶惠美",
     "album_pic_url": "http://img.test/a.jpg", "duration_ms": 269000, "has_sq": True},
    {"song_id": 102, "name": "七里香", "artist": "周杰伦", "album_name": "七里香",
     "album_pic_url": "", "duration_ms": 299000, "has_sq": False},
]


def _musicbox_handler(mode="ok"):
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        path = request.url.path
        if mode == "down":
            return httpx.Response(500)
        if path == "/api/v1/public-playlists/3778678":
            return httpx.Response(200, json={"ok": True, "data": SUMMARY})
        if path == "/api/v1/public-playlists/3778678/tracks":
            return httpx.Response(200, json={"ok": True, "data": TRACKS, "total": 2, "truncated": False})
        return httpx.Response(404)

    handler.state = state
    return handler


def _upstream(guid="user-public-1"):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/user/me"):
            return httpx.Response(200, json={"code": 0, "data": {"guid": guid}})
        if path.endswith("/playlist/list"):
            return httpx.Response(200, json={
                "code": 0,
                "data": {"list": [{"guid": "official", "name": "官方", "trackCount": 3}], "total": 1},
            })
        return httpx.Response(200, json={"code": 0, "data": None})
    return handler


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    pubpl.reset_for_test()
    monkeypatch.setenv("FNMUSIC_PUBLIC_PLAYLISTS_DIR", str(tmp_path / "public_cache"))
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))
    monkeypatch.setitem(CONF, "fav_dir", str(tmp_path / "online_favorites"))
    monkeypatch.setitem(CONF, "plt_dir", str(tmp_path / "playlist_tracks"))
    monkeypatch.setitem(CONF, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setitem(CONF, "recommend_hot", False)
    monkeypatch.setitem(CONF, "recommend_daily", False)
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "netease_my_playlists", False)
    monkeypatch.setitem(CONF, "netease_playlist_resident", None)
    monkeypatch.setitem(CONF, "public_playlists_enabled", True)
    monkeypatch.setitem(CONF, "public_playlists_selection", json.dumps(SELECTION))
    yield
    pubpl.reset_for_test()
    app.state.upstream_client = None
    app.state.musicbox_client = None


def _wire(mode="ok"):
    handler = _musicbox_handler(mode)
    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_upstream()), base_url="http://unix"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8770"
    )
    return handler


# ------------------------------------------------------------ 选择串与 guid --

def test_selection_guid_and_default_initialization():
    """空选择串回落默认四大榜；guid 自包含 provider/source/kind/id 可反解。"""
    entries = pubpl.parse_selection("", {"netease_enabled": True})
    assert [item["id"] for item in entries] == ["3778678", "3779629", "19723756", "2884035"]
    guid = pubpl.playlist_guid(entries[0])
    assert guid == "online:playlist:public:musicbox:netease:chart:3778678"
    assert pubpl.entry_from_guid(guid) == entries[0]
    assert pubpl.is_public_playlist_guid(guid) is True


def test_explicit_empty_selection_does_not_restore_defaults():
    """显式 "[]"（用户主动清空）不得被默认榜单覆盖。"""
    assert pubpl.parse_selection("[]", {"netease_enabled": True}) == []


def test_netease_selection_deduplicates_chart_and_playlist():
    """同一 id 的榜单/歌单两种 kind 视为同一份内容，只保留先出现的一条。"""
    rows = [SELECTION[0], {**SELECTION[0], "kind": "playlist"}]
    assert pubpl.parse_selection(json.dumps(rows)) == SELECTION


def test_non_musicbox_provider_is_dropped():
    """本仓库洛雪运行时无发现接口：lx 条目被丢弃，不影响 musicbox 条目。"""
    rows = [{"provider": "lx", "source": "kg", "kind": "playlist", "id": "id_1"}, SELECTION[0]]
    assert pubpl.parse_selection(json.dumps(rows)) == SELECTION
    assert pubpl.entry_from_guid("online:playlist:public:lx:kg:playlist:id_1") is None


# ------------------------------------------------------------------ 注入链路 --

def test_public_detail_requires_finos_authentication():
    """未登录飞牛时不吐公共歌单内容，也不触达 musicbox。"""
    handler = _wire()
    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(401, json={"code": 401})),
        base_url="http://unix",
    )
    with TestClient(app) as client:
        response = client.get("/music/api/v1/playlist/detail",
                              params={"guid": pubpl.playlist_guid(SELECTION[0])})
    assert response.status_code == 401
    assert handler.state["calls"] == 0


def test_playlist_list_detail_tracks_and_readonly():
    """注入顺序（公共歌单在官方之前）、卡片字段、曲目分页与只读吸收。"""
    _wire()
    guid = pubpl.playlist_guid(SELECTION[0])
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/playlist/list")
        assert resp.status_code == 200
        rows = resp.json()["data"]["list"]
        assert [row["guid"] for row in rows] == [guid, "official"]
        assert rows[0]["name"] == "[网易] 热歌榜"
        assert rows[0]["trackCount"] == 2
        assert rows[0]["isDaily"] is True
        # coverId 伪装成官方 track_+32hex（官方 App 只渲染这种形态）
        assert rows[0]["coverId"].startswith("track_")
        assert rows[0]["coverId"][6:] == fake_official_guid(guid)

        resp = client.get("/music/api/v1/playlist/detail", params={"guid": guid})
        assert resp.status_code == 200
        assert resp.json()["data"]["name"] == "[网易] 热歌榜"

        resp = client.get("/music/api/v1/track/playlist-detail/list",
                          params={"playlistGUID": guid, "size": -1})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 2
        assert [it["title"] for it in data["list"]] == ["晴天", "七里香"]
        # 曲目 guid 经 disguise_client_json 伪装成 32hex 下发（反解回真实 online guid），
        # 播放/歌词/封面仍走既有 online:netease 链路
        guids = [str(it["guid"]) for it in data["list"]]
        assert all(not g.startswith("online:") for g in guids)
        assert [resolve_real_guid(g) for g in guids] == ["online:netease:101", "online:netease:102"]

        # 只读：加歌/移歌/删除全部吸收（不回写任何第三方）
        for path in ("/music/api/v1/playlist/add-track", "/music/api/v1/playlist/remove-track"):
            assert client.post(path, json={"guid": guid, "trackGUIDs": ["x"]}).json()["code"] == 0
        assert client.post("/music/api/v1/playlist/delete", json={"guid": guid}).json()["code"] == 0


def test_public_playlists_off_by_default():
    """开关关闭：不注入，也不发任何 musicbox 请求。"""
    handler = _wire()
    CONF["public_playlists_enabled"] = False
    try:
        with TestClient(app) as client:
            resp = client.get("/music/api/v1/playlist/list")
        assert [row["guid"] for row in resp.json()["data"]["list"]] == ["official"]
        assert handler.state["calls"] == 0
    finally:
        CONF["public_playlists_enabled"] = True


def test_upstream_failure_does_not_block_official_list():
    """发现池挂掉只丢公共歌单卡片，官方列表照旧返回。"""
    handler = _wire("down")
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/playlist/list")
        assert resp.status_code == 200
        assert [row["guid"] for row in resp.json()["data"]["list"]] == ["official"]
        assert resp.json()["data"]["total"] == 1
    assert handler.state["calls"] == 1


def test_selection_hidden_when_runtime_unusable(monkeypatch):
    """音源与常驻都不可用（显式关闭常驻）时不注入：取流链路不在，挂了也播不了。"""
    handler = _wire()
    monkeypatch.setitem(CONF, "netease_enabled", False)
    monkeypatch.setitem(CONF, "netease_playlist_resident", False)
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/playlist/list")
    assert [row["guid"] for row in resp.json()["data"]["list"]] == ["official"]
    assert handler.state["calls"] == 0


# ---------------------------------------------------------------- 缓存与容错 --

def test_cached_card_remains_visible_while_another_summary_loads(monkeypatch):
    """慢歌单（摘要未回）不得拖走已缓存的其它歌单卡片。"""
    entry = {**SELECTION[0], "id": "3779629"}
    monkeypatch.setitem(CONF, "public_playlists_selection", json.dumps([SELECTION[0], entry]))
    pubpl._summary_cache[pubpl.playlist_guid(SELECTION[0])] = (SUMMARY, float("inf"))

    async def run():
        gate = asyncio.Event()

        async def handler(request):
            await gate.wait()
            return httpx.Response(200, json={"ok": True, "data": {**SUMMARY, "id": entry["id"]}})

        state = type("State", (), {})()
        state.musicbox_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://musicbox"
        )
        monkeypatch.setattr(pubpl, "schedule_prefetch", lambda *args: None)
        try:
            cards = await pubpl.peek_cards(state, CONF, lambda item: item)
            assert [card["guid"] for card in cards] == [pubpl.playlist_guid(SELECTION[0])]
            gate.set()
            await asyncio.gather(*list(pubpl._summary_tasks.values()))
        finally:
            await state.musicbox_client.aclose()

    asyncio.run(run())


def test_tracks_failure_cooldown_without_disk_snapshot():
    """无磁盘快照时连续失败只打一次 musicbox（冷却期内快速失败）。"""
    handler = _musicbox_handler("down")

    async def run() -> int:
        state = type("State", (), {})()
        state.musicbox_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://musicbox",
        )
        try:
            for _ in range(2):
                with pytest.raises(RuntimeError):
                    await pubpl.load_tracks(state, SELECTION[0], lambda item: {"guid": str(item["item_id"])})
            return handler.state["calls"]
        finally:
            await state.musicbox_client.aclose()

    assert asyncio.run(run()) == 1


def test_disk_summary_and_tracks_survive_restart():
    """磁盘快照跨重启可用，且 ensure_registry_warm 能重建曲目假 id 反查。"""
    _wire()
    guid = pubpl.playlist_guid(SELECTION[0])
    with TestClient(app) as client:
        client.get("/music/api/v1/playlist/list")
        client.get("/music/api/v1/track/playlist-detail/list",
                   params={"playlistGUID": guid, "size": -1})
        track_fake = fake_official_guid("online:netease:101")
    pubpl.reset_for_test()          # 模拟重启：内存清空，磁盘缓存仍在
    down = _wire("down")
    assert pubpl.cached_card(guid)["name"] == "[网易] 热歌榜"
    assert pubpl.cover_url_for(guid) == "http://img.test/hot.jpg"
    assert [t["guid"] for t in pubpl.cached_tracks(guid)] == ["online:netease:101", "online:netease:102"]
    proxy_app._REGISTRY_WARMED = False
    try:
        ensure_registry_warm()
        assert resolve_real_guid(track_fake) == "online:netease:101"
    finally:
        proxy_app._REGISTRY_WARMED = True
    assert down.state["calls"] == 0  # 全程未触达已挂的 musicbox
