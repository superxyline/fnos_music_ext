"""收藏绑定对齐上游 #82（4502fde）：滚动缓存先转存曲库再绑定。

对照上游修复逐项覆盖（我方实现的缺口补齐）：
- _promote_rolling_cache_to_library：音频 + .lrc 随迁进曲库；元数据 ID3 兜底；
  元数据双空返回 None 保持缓存形态（绝不 unknown 入库）
- _schedule_rolling_promote：同 guid 在途去重（重复收藏/列表自愈并发只搬一次）；
  转存不了回退后台整轨下载（不卡在缓存里被淘汰）
- _after_rolling_promote：转存成功即登记待关联 + 起轮询（对齐上游绑定调度）
- _spawn_link_poller：同 (user,guid) 在途轮询去重 + 完成后清理
- _reconcile_official_links_for_user：per-user 锁串行，并发对账不重叠打官方搜索
- _seed_pending_from_favorites：只认曲库文件；滚动缓存形态不进待关联集合
- 收藏列表自愈：滚动缓存命中触发转存（端到端）
"""
import asyncio
import anyio
import json
import os
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import proxy.app as appmod
from proxy.app import CONF, app

FAKE_KUWO = "online:kuwo:228908"
OFFICIAL_GUID = "official-guid-1"
USER = "user-a"


@pytest.fixture(autouse=True)
def rolling_env(tmp_path, monkeypatch):
    """滚动缓存/曲库/收藏目录全部落 tmp，内存态清空。"""
    appmod._pending_official_links.clear()
    appmod._link_poll_tasks.clear()
    appmod._rolling_promote_active.clear()
    appmod._reconcile_locks.clear()
    appmod._full_fetch_tasks.clear()
    appmod._full_fetch_failed.clear()
    appmod._bind_registry.clear()
    monkeypatch.setattr(appmod, "_bind_registry_loaded", True)
    monkeypatch.setattr(appmod, "_HOME", str(tmp_path))

    dirs = {k: str(tmp_path / k) for k in ("cache", "library", "fav")}
    for d in dirs.values():
        os.makedirs(d, exist_ok=True)
    monkeypatch.setitem(CONF, "cache_dir", dirs["cache"])
    monkeypatch.setitem(CONF, "tee_save_dir", dirs["library"])
    monkeypatch.setitem(CONF, "library_dir", dirs["library"])
    monkeypatch.setitem(CONF, "fav_dir", dirs["fav"])
    monkeypatch.setitem(CONF, "lyric_auto_dl", False)
    monkeypatch.setitem(CONF, "library_scan_path", "")

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"code": 0, "data": None})),
        base_url="http://unix",
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404)), base_url="http://127.0.0.1:8768")
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404)), base_url="http://127.0.0.1:8770")
    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404)), base_url="http://127.0.0.1:8772")

    yield dirs

    for task in list(appmod._link_poll_tasks.values()):
        task.cancel()
    appmod._link_poll_tasks.clear()
    appmod._rolling_promote_active.clear()
    appmod._reconcile_locks.clear()
    appmod._pending_official_links.clear()
    appmod._full_fetch_tasks.clear()
    appmod._full_fetch_failed.clear()
    appmod._bind_registry.clear()


@pytest.fixture
def anyio_backend():
    return "asyncio"


def make_rolling(guid: str, cache_dir: str, ext: str = "mp3", with_lrc: bool = False) -> str:
    """在 cache 目录造一份该 guid 的滚动缓存产物（cache_safe_guid 命名）。"""
    path = os.path.join(cache_dir, f"{appmod.cache_safe_guid(guid)}.{ext}")
    with open(path, "wb") as f:
        f.write(b"\x00\x01\x02not-a-real-audio" * 64)
    if with_lrc:
        with open(os.path.splitext(path)[0] + ".lrc", "w", encoding="utf-8") as f:
            f.write("[00:01.00]测试歌词\n")
    return path


def wait_for(pred, timeout: float = 6.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def library_files(dirs: dict) -> list[str]:
    return [f for f in os.listdir(dirs["library"])
            if not f.endswith((".ref", ".lrc"))]


# ---- 1. 转存本体：音频 + 歌词随迁 ----

def test_promote_moves_audio_and_lrc_into_library(rolling_env):
    """滚动缓存 + .lrc 一起定名挪进曲库，缓存原文件消失，ref 指向曲库。"""
    guid = "online:kuwo:3001"
    cached = make_rolling(guid, rolling_env["cache"], with_lrc=True)

    dest = appmod._promote_rolling_cache_to_library(guid, {"title": "晴天", "artist": "周杰伦"})

    assert dest
    assert os.path.dirname(dest) == os.path.abspath(rolling_env["library"])
    assert os.path.basename(dest) == "周杰伦 - 晴天.mp3"
    assert not os.path.exists(cached)  # 缓存原文件已搬走
    assert not os.path.exists(os.path.splitext(cached)[0] + ".lrc")
    assert os.path.exists(os.path.splitext(dest)[0] + ".lrc")  # 歌词随迁
    assert appmod.find_cache_file(guid) == dest  # ref 指向曲库文件
    assert not appmod._is_rolling_cache_stem(dest, guid)


def test_promote_metadata_falls_back_to_id3(rolling_env, monkeypatch):
    """快照缺元数据时用文件自带 ID3 兜底定名（聚合源 mp3 大多带标签）。"""
    guid = "online:kuwo:3002"
    make_rolling(guid, rolling_env["cache"])
    monkeypatch.setattr(appmod, "read_media_tags", lambda p: ("ID3歌名", "ID3歌手", "ID3专辑"))

    dest = appmod._promote_rolling_cache_to_library(guid, {})

    assert dest and os.path.basename(dest) == "ID3歌手 - ID3歌名.mp3"


def test_promote_returns_none_without_metadata_keeps_cache(rolling_env):
    """快照与 ID3 双空：返回 None 且保持缓存形态，绝不以 unknown 定名入库。"""
    guid = "online:kuwo:3003"
    cached = make_rolling(guid, rolling_env["cache"])

    assert appmod._promote_rolling_cache_to_library(guid, {}) is None
    assert os.path.exists(cached)
    assert library_files(rolling_env) == []


# ---- 2. 调度：在途去重 + 失败回退 ----

@pytest.mark.anyio
async def test_schedule_rolling_promote_dedups_in_flight(rolling_env, monkeypatch):
    """同 guid 并发触发（重复收藏/列表自愈）只搬一次文件。"""
    guid = "online:kuwo:3004"
    make_rolling(guid, rolling_env["cache"])
    calls: list[str] = []

    def slow_promote(g, hint):
        calls.append(g)
        time.sleep(0.3)
        return os.path.join(rolling_env["library"], "周杰伦 - 晴天.mp3")

    monkeypatch.setattr(appmod, "_promote_rolling_cache_to_library", slow_promote)
    monkeypatch.setattr(appmod, "_spawn_link_poller", lambda *a, **k: None)
    req = appmod._synth_request({})

    appmod._schedule_rolling_promote(req, guid, {"title": "晴天", "artist": "周杰伦"}, USER)
    appmod._schedule_rolling_promote(req, guid, {"title": "晴天", "artist": "周杰伦"}, USER)
    await asyncio.sleep(0.6)

    assert calls == [guid]  # 在途去重：只搬一次
    assert guid not in appmod._rolling_promote_active  # 完成后清理在途标记


@pytest.mark.anyio
async def test_promote_none_falls_back_to_full_fetch(rolling_env, monkeypatch):
    """元数据缺失转存不了 → 回退后台整轨下载（新鲜元数据定名落库）。"""
    guid = "online:kuwo:3005"
    make_rolling(guid, rolling_env["cache"])
    downloaded: list[str] = []

    async def fake_full(g, headers, info_hint=None, favorite_context=None):
        downloaded.append(g)

    monkeypatch.setattr(appmod, "_full_fetch_download", fake_full)
    req = appmod._synth_request({})

    appmod._schedule_rolling_promote(req, guid, {}, USER)
    await asyncio.sleep(0.3)

    assert downloaded == [guid]
    assert guid in appmod._full_fetch_tasks


@pytest.mark.anyio
async def test_register_favorite_download_promotes_and_registers_link(rolling_env, monkeypatch):
    """收藏触达滚动缓存：转存进曲库 + 登记待关联 + 起轮询（#82 主路径）。"""
    guid = "online:kuwo:3006"
    make_rolling(guid, rolling_env["cache"], with_lrc=True)
    spawned: list[str] = []
    monkeypatch.setattr(appmod, "_spawn_link_poller",
                        lambda g, info, headers, user: spawned.append(g))
    req = appmod._synth_request({})

    ok = appmod._register_favorite_download(req, guid, {"title": "晴天", "artist": "周杰伦"}, USER)
    assert ok is False  # 复用已下载文件，不重复拉流
    await asyncio.sleep(0.3)

    assert library_files(rolling_env) == ["周杰伦 - 晴天.mp3"]
    assert f"{USER}:{guid}" in appmod._pending_official_links  # 即时登记，不等列表自愈
    assert spawned == [guid]


# ---- 3. 轮询去重 + 对账串行 ----

@pytest.mark.anyio
async def test_spawn_link_poller_dedups_and_cleans_up(monkeypatch):
    """同 (user,guid) 在途不重复起轮询；完成后清理，超时重试可再起。"""
    started: list[str] = []

    async def fake_poller(guid, info, headers, user_guid):
        started.append(guid)
        await asyncio.sleep(0.2)

    monkeypatch.setattr(appmod, "_link_official_favorite", fake_poller)

    appmod._spawn_link_poller("online:kuwo:1", {"title": "t"}, {}, USER)
    appmod._spawn_link_poller("online:kuwo:1", {"title": "t"}, {}, USER)
    await asyncio.sleep(0.05)
    assert started == ["online:kuwo:1"]  # 第二个被在途去重挡掉
    assert len(appmod._link_poll_tasks) == 1

    await asyncio.sleep(0.4)
    assert appmod._link_poll_tasks == {}  # 完成后清理

    appmod._spawn_link_poller("online:kuwo:1", {"title": "t"}, {}, USER)
    await asyncio.sleep(0.05)
    assert len(started) == 2  # 上一轮已结束：可再起（超时重试场景）


class _FakeClient:
    """对账用的假上游客户端：搜索慢速可计数，记录建官方红心的 POST。"""

    def __init__(self):
        self.searching = 0
        self.max_searching = 0
        self.posts: list[dict] = []
        self._json = None

    def build_request(self, method, path, params=None, headers=None, json=None):
        self._json = json
        return httpx.Request(method, "http://unix" + path)

    async def send(self, req, **kw):
        path = req.url.path
        if req.method == "GET" and path.endswith("/search/track"):
            self.searching += 1
            self.max_searching = max(self.max_searching, self.searching)
            await anyio.sleep(0.2)
            self.searching -= 1
            return httpx.Response(200, json={"code": 0, "data": {"list": [{
                "guid": OFFICIAL_GUID, "title": "晴天", "artists": [{"name": "周杰伦"}],
            }]}})
        if req.method == "POST" and path.endswith("/favorite-track/create"):
            self.posts.append(self._json)
        return httpx.Response(200, json={"code": 0, "msg": "", "data": None})


@pytest.mark.anyio
async def test_reconcile_serialized_per_user(rolling_env, monkeypatch):
    """同用户并发对账串行：官方搜索不重叠，每个待关联条目只建一次官方红心。"""
    fake = _FakeClient()
    monkeypatch.setattr(appmod, "get_upstream_client", lambda _app: fake)
    monkeypatch.setattr(appmod, "_repair_filename_track_metadata", lambda *a, **k: None)
    for g in ("online:kuwo:4001", "online:kuwo:4002"):
        appmod._register_pending_official_link(USER, g, {"title": "晴天", "artist": "周杰伦"})

    await asyncio.gather(
        appmod._reconcile_official_links_for_user({}, USER),
        appmod._reconcile_official_links_for_user({}, USER),
    )

    assert fake.max_searching == 1  # 串行：没有两个搜索重叠
    assert fake.posts == [{"trackGUID": OFFICIAL_GUID}] * 2  # 每条目只建一次
    assert appmod._pending_official_links == {}  # 全部转换完成并弹出


# ---- 4. seed 去污染 + 列表自愈 ----

def test_seed_skips_rolling_cache_only(rolling_env):
    """只认曲库文件：滚动缓存形态不进待关联集合（官方扫描不索引 cache 目录）。"""
    guid_rolling = "online:kuwo:5001"
    guid_lib = "online:kuwo:5002"
    make_rolling(guid_rolling, rolling_env["cache"])
    dest = os.path.join(rolling_env["library"], "周杰伦 - 晴天.mp3")
    with open(dest, "wb") as f:
        f.write(b"x" * 2048)
    appmod.remember_media_path(guid_lib, dest)

    appmod._seed_pending_from_favorites(USER, [
        {"guid": guid_rolling, "track": {"title": "月光", "artists": [{"name": "A"}]}},
        {"guid": guid_lib, "track": {"title": "晴天", "artists": [{"name": "周杰伦"}]}},
    ])

    assert list(appmod._pending_official_links) == [f"{USER}:{guid_lib}"]


def test_favorite_list_selfheal_promotes_rolling_cache(rolling_env, monkeypatch):
    """端到端：收藏列表请求把滚动缓存形态的在线收藏转存进曲库并登记待关联。"""
    guid = "online:kuwo:5003"
    make_rolling(guid, rolling_env["cache"], with_lrc=True)
    appmod.save_online_favorites(USER, [{
        "guid": guid, "createdAt": 0,
        "track": {"title": "晴天", "artists": [{"name": "周杰伦"}]},
    }])

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user/me"):
            return httpx.Response(200, json={"code": 0, "data": {"guid": USER}})
        if request.method == "GET" and request.url.path.endswith("/favorite-track/list"):
            return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})
        return httpx.Response(200, json={"code": 0, "data": None})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix")

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/favorite-track/list")
        assert resp.status_code == 200
        # 自愈循环调度转存任务：文件进曲库（含歌词随迁）+ 待关联即时登记
        assert wait_for(lambda: os.path.exists(
            os.path.join(rolling_env["library"], "周杰伦 - 晴天.mp3")))
        assert wait_for(lambda: f"{USER}:{guid}" in appmod._pending_official_links)
    assert os.path.exists(os.path.join(rolling_env["library"], "周杰伦 - 晴天.lrc"))
