"""当前播放快照（管理台「小爱音箱」页）回归：歌名 + 本地/在线源标注。

用户需求（2026-10-28）：接管期间打开插件的小爱音箱页，要看到"正在播放哪首歌"
以及"是本地歌曲还是哪个在线源的歌"。

来源判定一律基于 guid（online:<源>:<id> / online:lx:<平台>:<id> / 其余=本地曲库），
与歌名解耦——避免"在线歌下载到本地就标成本地"的误判。
"""
import types

import app as bridge


# ---------------- 来源解析 ----------------

def test_guid_source_local_for_non_online_guid():
    """飞牛曲目 guid（无 online: 前缀）→ 本地曲库。"""
    for guid in ("", "abc123", "0f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c"):
        assert bridge._guid_source(guid) == ("local", "本地曲库"), guid


def test_guid_source_online_sources():
    """online:<源>:<id> 与 lx 的 online:lx:<平台>:<id> 都要能落到具体源名。"""
    assert bridge._guid_source("online:kuwo:228908") == ("kuwo", "在线 · 酷我")
    assert bridge._guid_source("online:migu:600929") == ("migu", "在线 · 咪咕")
    assert bridge._guid_source("online:bilibili:BV1xx") == ("bilibili", "在线 · 哔哩哔哩")
    # 自定义音源多一层平台段：平台段才是真实来源
    assert bridge._guid_source("online:lx:kw:88") == ("kw", "在线 · 酷我")
    assert bridge._guid_source("online:lx:tx:99") == ("tx", "在线 · QQ音乐")
    # 未知源回退大写键名，不能显示成空白
    assert bridge._guid_source("online:unknownsrc:1") == ("unknownsrc", "在线 · UNKNOWNSRC")
    # 只有两段（源站自己拼的 online:<id>）→ 通用在线音源
    assert bridge._guid_source("online:weirdid") == ("online", "在线音源")


# ---------------- 当前播放快照 ----------------

def _dev(name, cur_music, playing=True, did="d1"):
    return types.SimpleNamespace(
        is_playing=playing,
        device=types.SimpleNamespace(name=name, did=did, cur_music=cur_music),
    )


def _xm(devices, all_music=None, last_guid="", last_title="", at=0):
    return types.SimpleNamespace(
        device_manager=types.SimpleNamespace(devices=devices),
        music_library=types.SimpleNamespace(all_music=all_music or {}),
        last_played_guid=last_guid,
        last_played_title=last_title,
        last_played_at=at,
    )


def _install(monkeypatch, xm, favorites=()):
    monkeypatch.setattr(bridge, "XM", xm)
    monkeypatch.setattr(bridge.STATE, "favorites", list(favorites))


def test_now_playing_local_library_from_play_url(monkeypatch):
    """收藏曲目：歌名取设备当前曲，来源从该曲的 /play?guid= 反查（本地曲库）。"""
    name = "周杰伦 - 晴天"
    _install(monkeypatch, _xm(
        {"d1": _dev("客厅的小爱", name)},
        all_music={name: "http://192.168.x.x:8775/play?guid=abc123"},
    ))
    snap = bridge._now_playing()
    assert snap["name"] == name
    assert snap["source"] == "local"
    assert snap["source_label"] == "本地曲库"
    assert snap["is_playing"] is True
    assert snap["device"] == "客厅的小爱"


def test_now_playing_online_source_via_last_played(monkeypatch):
    """在线源点歌是"下载成 mp3 再播"，all_music 是文件路径 → 靠 last_played 兜底判源。"""
    name = "李荣浩 - 乌梅子酱"
    _install(monkeypatch, _xm(
        {"d1": _dev("客厅的小爱", name)},
        all_music={name: "/data/xiaomusic/music/download/李荣浩 - 乌梅子酱.mp3"},
        last_guid="online:migu:600929",
        last_title=name,
    ))
    snap = bridge._now_playing()
    assert snap["name"] == name
    assert (snap["source"], snap["source_label"]) == ("migu", "在线 · 咪咕")


def test_now_playing_name_falls_back_to_favorites(monkeypatch):
    """设备还没报当前曲（刚开播）时，用 /play 记录的 guid 反查收藏里的完整歌名。"""
    fav = {"guid": "abc123", "title": "晴天", "artists": [{"name": "周杰伦"}]}
    _install(
        monkeypatch,
        _xm({"d1": _dev("客厅的小爱", "")}, last_guid="abc123"),
        favorites=[fav],
    )
    snap = bridge._now_playing()
    assert snap["name"] == "周杰伦 - 晴天"
    assert snap["source_label"] == "本地曲库"


def test_now_playing_not_playing(monkeypatch):
    """没在播：is_playing=False，前端据 name 为空显示"当前未在播放"。"""
    _install(monkeypatch, _xm({"d1": _dev("客厅的小爱", "", playing=False)}))
    snap = bridge._now_playing()
    assert snap["is_playing"] is False
    assert snap["name"] == ""


def test_now_playing_survives_missing_engine(monkeypatch):
    """桥接未就绪（XM=None）/设备表为空：health 不能被拖垮。"""
    monkeypatch.setattr(bridge, "XM", None)
    snap = bridge._now_playing()
    assert snap["name"] == "" and snap["is_playing"] is False
    _install(monkeypatch, _xm({}))
    assert bridge._now_playing()["name"] == ""


def test_health_exposes_now_playing():
    """/health 必须带 now_playing（管理台只调它一个接口拿状态）。"""
    from pathlib import Path

    body = Path(bridge.__file__).read_text(encoding="utf-8")
    health = body[body.index('@app.get("/health")'):]
    health = health[:health.index('@app.get("/", response_class=HTMLResponse)')]
    assert '"now_playing"' in health
    assert "_now_playing()" in health
