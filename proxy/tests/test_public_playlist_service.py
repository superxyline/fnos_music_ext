"""musicbox 公共歌单发现接口测试（netease_ext 公共函数 + /api/v1/public-playlists 路由）。

不联网：用假 NetEase 实例替换 _api_instance，只验证参数拼装、结果映射、
去重保序、未登录分支与路由顺序（capabilities 不能被 {playlist_id} 吃掉）。
"""
import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

MUSICBOX_SERVICE_DIR = Path(__file__).resolve().parent.parent.parent / "musicbox-service"
if str(MUSICBOX_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(MUSICBOX_SERVICE_DIR))

import netease_ext  # noqa: E402

_spec = importlib.util.spec_from_file_location("musicbox_public_app", MUSICBOX_SERVICE_DIR / "app.py")
musicbox_app = importlib.util.module_from_spec(_spec)
sys.modules["musicbox_public_app"] = musicbox_app
_spec.loader.exec_module(musicbox_app)


class _FakeResp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeApi:
    """只实现公共歌单链路用到的四个入口。"""

    def __init__(self, recommend_code=200):
        self.calls: list = []
        self.recommend_code = recommend_code

    def _raw_request(self, method, endpoint, data=None):
        self.calls.append(("get", endpoint, dict(data or {})))
        if endpoint.endswith("/api/toplist"):
            return _FakeResp({"code": 200, "list": [
                {"id": 3778678, "name": "热歌榜", "updateTime": 1760000000000, "trackCount": 200},
                {"id": 999, "name": "无 id 行应被丢弃", "id2": 0},
            ]})
        if endpoint.endswith("/api/personalized/playlist"):
            return _FakeResp({"code": 200, "result": [
                {"id": 111, "name": "私人推荐", "picUrl": "http://img/p.jpg", "trackCount": 5},
            ]})
        if endpoint.endswith("/api/playlist/highquality/list"):
            return _FakeResp({"code": 200, "total": 20, "more": True, "playlists": [
                {"id": 222, "name": "精选一", "updateTime": 1760000000000},
            ]})
        if endpoint.endswith("/api/playlist/highquality/tags"):
            return _FakeResp({"code": 200, "tags": [{"playlistTag": {"name": "流行"}}]})
        raise AssertionError(f"unexpected GET {endpoint}")

    def request(self, method, path, params=None):
        self.calls.append(("post", path, dict(params or {})))
        if path == "/weapi/playlist/catalogue":
            return {"code": 200, "categories": {"0": "语种", "1": "风格"},
                    "sub": [{"category": 0, "name": "华语"}, {"category": 1, "name": "摇滚"}]}
        if path == "/weapi/v1/discovery/recommend/resource":
            return {"code": self.recommend_code, "recommend": [
                {"id": 333, "name": "登录推荐", "creator": {"nickname": "网易"}},
            ]}
        if path == "/weapi/playlist/list":
            return {"code": 200, "total": 300, "more": True, "playlists": [
                {"id": 444, "name": "分类歌单", "coverImgUrl": "http://img/c.jpg"},
            ]}
        raise AssertionError(f"unexpected POST {path}")

    def eapi_request(self, path, params=None):
        self.calls.append(("eapi", path, dict(params or {})))
        return {"playlist": {
            "id": 3778678, "name": "热歌榜", "coverImgUrl": "http://img/hot.jpg",
            "trackCount": 3, "updateTime": 1760000000000,
            # 重复 id 去重、顺序保持
            "trackIds": [{"id": 101}, {"id": 102}, {"id": 101}],
        }}

    def songs_detail(self, ids):
        self.calls.append(("detail", list(ids)))
        return [
            {"id": sid, "name": f"歌{sid}", "ar": [{"name": "歌手"}],
             "al": {"name": "专辑", "picUrl": f"http://img/{sid}.jpg"}, "dt": 200000}
            for sid in ids
        ]


@pytest.fixture
def fake_api(monkeypatch):
    api = _FakeApi()
    monkeypatch.setattr(netease_ext, "_api_instance", api)
    logged_in = lambda: True  # noqa: E731  登录态单独可控，发现池多数入口匿名可用
    monkeypatch.setattr(netease_ext, "check_is_logged_in", logged_in)
    # app.py 用 from netease_ext import check_is_logged_in 直接绑定了名字，需单独覆盖
    monkeypatch.setattr(musicbox_app, "check_is_logged_in", logged_in)
    return api


@pytest.fixture
def client():
    return TestClient(musicbox_app.app)


# --------------------------------------------------------------- 纯函数层 --

def test_charts_channel_maps_rows(fake_api):
    data = netease_ext.public_playlists("charts")
    assert data["ok"] is True
    assert data["has_more"] is False
    row = data["items"][0]
    assert row["id"] == "3778678"
    assert row["kind"] == "chart"
    assert row["source"] == "netease"
    assert row["name"] == "热歌榜"
    # updateTime 是毫秒 -> 秒
    assert row["updated_at"] == 1760000000


def test_recommend_channel_uses_anonymous_endpoint(fake_api):
    data = netease_ext.public_playlists("recommend", limit=10)
    assert [it["id"] for it in data["items"]] == ["111"]
    assert data["items"][0]["cover_url"] == "http://img/p.jpg"
    assert ("get", "https://music.163.com/api/personalized/playlist", {"limit": 10}) in fake_api.calls


def test_account_recommend_requires_login(fake_api, monkeypatch):
    api = _FakeApi(recommend_code=301)
    monkeypatch.setattr(netease_ext, "_api_instance", api)
    data = netease_ext.public_playlists("account_recommend")
    assert data == {"ok": False, "error": "not_logged_in", "logged_in": False}


def test_playlist_channel_pagination(fake_api):
    data = netease_ext.public_playlists("playlist", category="华语", sort="new", page=2, limit=30)
    assert data["total"] == 300
    assert data["has_more"] is True
    assert data["items"][0]["id"] == "444"
    call = [c for c in fake_api.calls if c[1] == "/weapi/playlist/list"][0]
    assert call[2]["offset"] == 30
    assert call[2]["cat"] == "华语"
    assert call[2]["order"] == "new"


def test_categories_group_by_category_name(fake_api):
    data = netease_ext.public_playlist_categories("playlist")
    assert data["categories"] == [{"id": "", "name": "全部"}, {"id": "语种", "name": "语种"},
                                 {"id": "风格", "name": "风格"}]
    assert data["tags"] == {"语种": [{"id": "华语", "name": "华语"}],
                            "风格": [{"id": "摇滚", "name": "摇滚"}]}
    assert data["highquality_tags"] == [{"id": "流行", "name": "流行"}]


def test_public_playlist_chart_hits_toplists_without_detail(fake_api):
    data = netease_ext.public_playlist(3778678, "chart")
    assert data["data"]["name"] == "热歌榜"
    assert not [c for c in fake_api.calls if c[0] == "eapi"]  # 榜单直接命中，省一次详情请求


def test_public_playlist_tracks_dedupes_and_hydrates(fake_api):
    data = netease_ext.public_playlist_tracks(3778678)
    assert data["ok"] is True
    assert [row["song_id"] for row in data["data"]] == [101, 102]
    assert data["truncated"] is False
    assert fake_api.calls[-1][0] == "detail"


def test_public_playlist_tracks_fails_when_details_incomplete(fake_api, monkeypatch):
    monkeypatch.setattr(fake_api, "songs_detail", lambda ids: [])
    with pytest.raises(RuntimeError):
        netease_ext.public_playlist_tracks(3778678)


# ---------------------------------------------------------------- 路由层 --

def test_capabilities_not_shadowed_by_playlist_id_route(fake_api, client):
    resp = client.get("/api/v1/public-playlists/capabilities")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    source = data["sources"][0]
    assert source["id"] == "netease"
    assert source["logged_in"] is True
    assert "account_recommend" in source["channels"]


def test_categories_route(fake_api, client):
    resp = client.get("/api/v1/public-playlists/categories")
    assert resp.status_code == 200
    assert resp.json()["categories"][0] == {"id": "", "name": "全部"}


def test_list_and_detail_and_tracks_routes(fake_api, client):
    assert client.get("/api/v1/public-playlists", params={"channel": "charts"}).json()["items"][0]["id"] == "3778678"
    assert client.get("/api/v1/public-playlists/3778678", params={"kind": "chart"}).json()["data"]["name"] == "热歌榜"
    tracks = client.get("/api/v1/public-playlists/3778678/tracks").json()
    assert [row["song_id"] for row in tracks["data"]] == [101, 102]


def test_unknown_channel_is_rejected(fake_api, client):
    resp = client.get("/api/v1/public-playlists", params={"channel": "nope"})
    assert resp.json() == {"ok": False, "error": "unsupported channel: nope"}


def test_endpoint_returns_ok_false_instead_of_5xx(fake_api, client, monkeypatch):
    """拉取失败也要 200 + ok=False（代理侧按 ok 判失败，避免日志被 500 噪音淹没）。"""
    def boom(*args, **kwargs):
        raise RuntimeError("netease down")

    monkeypatch.setattr(musicbox_app, "public_playlists", boom)
    resp = client.get("/api/v1/public-playlists", params={"channel": "charts"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "error": "netease down"}
