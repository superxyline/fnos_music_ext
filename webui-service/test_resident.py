"""网易云歌单常驻（B1 / 上游 issue #38）回归。

锁定行为：
- 常驻三态：未配置（老配置）＝账号歌单即常驻；true＝常驻；false＝不常驻
- 切源时 musicbox 进程保留（不因音源切换被停）
- 保存对账：未登录则回收进程 + 告警，5 分钟内不再反复拉起
- 已登录：进程未运行则拉起并保活；取消常驻则停止释放内存
- 预览清退/保存收尾在常驻生效时保留 musicbox
"""
from __future__ import annotations

import importlib.util
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

HERE = Path(__file__).resolve().parent


def _load_webui():
    cached = sys.modules.get("webui_service_app")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("webui_service_app", HERE / "app.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webui_service_app"] = mod
    spec.loader.exec_module(mod)
    return mod


webui = _load_webui()

BASE_ENV = """FNMUSIC_NETEASE_ENABLED='false'
FNMUSIC_MUSICDL_ENABLED='true'
FNMUSIC_LX_ENABLED='false'
FNMUSIC_ONLINE_SOURCES='KuwoMusicClient'
MUSICDL_SOURCES='KuwoMusicClient'
LX_SOURCE_URL=''
FNMUSIC_NETEASE_MY_PLAYLISTS='true'
"""


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(BASE_ENV, encoding="utf-8")
    (tmp_path / "VERSION").write_text("3.1.2\n", encoding="utf-8")
    monkeypatch.setattr(webui, "ENV_PATH", env)
    monkeypatch.setattr(webui, "VERSION_PATH", tmp_path / "VERSION")
    webui._preview_until.clear()
    webui._resident_login_cache.update(ts=0.0, value=None)
    webui._resident_start_block.update(until=0.0, reason="")
    webui._resident_state["active"] = False
    return env


@pytest.fixture
def svctl(monkeypatch):
    calls: list[tuple] = []
    state = {"musicbox": "STOPPED"}

    def fake(*args, timeout=20.0):
        calls.append(tuple(args))
        if args[0] == "status":
            rows = []
            for prog in ("musicdl", "musicbox", "lxmusic", "webui"):
                st = "RUNNING   pid 7, uptime 0:01:00" if (
                    prog == "musicbox" and state["musicbox"] == "RUNNING"
                ) or prog in ("musicdl", "webui") else "STOPPED   Not started"
                rows.append(f"{prog}   {st}")
            return 3, "\n".join(rows)
        if args[0] in ("start", "stop") and args[1] == "musicbox":
            state["musicbox"] = "RUNNING" if args[0] == "start" else "STOPPED"
        return 0, f"{args[0]}ed {args[1]}"

    monkeypatch.setattr(webui, "supervisorctl", fake)
    fake.calls = calls
    fake.state = state
    return fake


@contextmanager
def authed_client():
    with TestClient(webui.app) as client:
        client.headers.update({"X-Trim-Isadmin": "true"})
        yield client


def _mock_http(logged_in=True, handler=None):
    def default(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/v1/auth/status"):
            return httpx.Response(200, json={"logined": logged_in, "data": {"logged_in": logged_in}})
        if request.url.path.endswith("/healthz"):
            return httpx.Response(200, json={"ok": True})
        if handler is not None:
            return handler(request)
        return httpx.Response(200, json={"ok": True, "data": []})

    webui.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(default))


# ------------------------------------------------------------------ 三态判定 ---

@pytest.mark.parametrize("raw,expected", [
    (None, True),      # 未配置：沿用老行为（账号歌单即常驻）
    ("", True),
    ("true", True),
    ("false", False),
])
def test_resident_tristate(env_file, raw, expected):
    values = webui.read_env()
    if raw is None:
        values.pop("FNMUSIC_NETEASE_PLAYLIST_RESIDENT", None)
    else:
        values["FNMUSIC_NETEASE_PLAYLIST_RESIDENT"] = raw
    assert webui._resident_active(values) is expected


def test_resident_requires_my_playlists(env_file):
    values = webui.read_env()
    values["FNMUSIC_NETEASE_PLAYLIST_RESIDENT"] = "true"
    values["FNMUSIC_NETEASE_MY_PLAYLISTS"] = "false"
    assert webui._resident_active(values) is False


# ------------------------------------------------------------------ 切源保留 ---

def test_switch_provider_keeps_resident_musicbox(env_file, svctl):
    svctl.state["musicbox"] = "RUNNING"
    actions = webui.switch_provider_process("musicbox", "musicdl")
    ops = [(a["program"], a["op"]) for a in actions if a.get("kind") == "process"]
    assert ("musicbox", "keep") in ops
    assert not any(c[:2] == ("stop", "musicbox") for c in svctl.calls)


def test_switch_provider_stops_musicbox_when_not_resident(env_file, svctl):
    env = env_file
    env.write_text(BASE_ENV + "FNMUSIC_NETEASE_PLAYLIST_RESIDENT='false'\n", encoding="utf-8")
    svctl.state["musicbox"] = "RUNNING"
    actions = webui.switch_provider_process("musicbox", "musicdl")
    ops = [(a["program"], a["op"]) for a in actions if a.get("kind") == "process"]
    assert ("musicbox", "stop") in ops


# ------------------------------------------------------------------ 保存对账 ---

def test_save_reclaims_musicbox_when_not_logged_in(env_file, svctl):
    _mock_http(logged_in=False)
    with authed_client() as client:
        r = client.put("/api/config", json={"values": {"FNMUSIC_NETEASE_PLAYLIST_RESIDENT": True}})
        assert r.status_code == 200
        rj = r.json()
    resident = [a for a in rj["actions"] if a.get("kind") == "resident"]
    assert any(a["op"] == "stop" and a["ok"] for a in resident)
    assert any(a["op"] == "deactivate" and "未登录" in a["error"] for a in resident)
    # 5 分钟防抖已布防
    assert webui._resident_start_block["until"] > time.monotonic()


def test_save_keeps_musicbox_when_logged_in(env_file, svctl):
    _mock_http(logged_in=True)
    with authed_client() as client:
        r = client.put("/api/config", json={"values": {"FNMUSIC_NETEASE_PLAYLIST_RESIDENT": True}})
        assert r.status_code == 200
    assert svctl.state["musicbox"] == "RUNNING"
    assert webui._resident_state["active"] is True


def test_start_block_prevents_repeated_launch(env_file, svctl):
    _mock_http(logged_in=False)
    with authed_client() as client:
        client.put("/api/config", json={"values": {"FNMUSIC_NETEASE_PLAYLIST_RESIDENT": True}})
        starts_before = sum(1 for c in svctl.calls if c[:2] == ("start", "musicbox"))
        client.put("/api/config", json={"values": {"FNMUSIC_NETEASE_PLAYLIST_RESIDENT": True}})
        starts_after = sum(1 for c in svctl.calls if c[:2] == ("start", "musicbox"))
    assert starts_after == starts_before, "未登录防抖期内不得反复 start→stop 空转"


def test_disabling_resident_stops_process(env_file, svctl):
    svctl.state["musicbox"] = "RUNNING"
    _mock_http(logged_in=True)
    with authed_client() as client:
        r = client.put("/api/config", json={"values": {"FNMUSIC_NETEASE_PLAYLIST_RESIDENT": False}})
        assert r.status_code == 200
    assert svctl.state["musicbox"] == "STOPPED"


# ------------------------------------------------------------------ 预览保活 ---

def test_preview_reap_keeps_resident_musicbox(env_file, svctl):
    svctl.state["musicbox"] = "RUNNING"
    webui._preview_until["musicbox"] = time.monotonic() - 1
    assert webui.preview_reap() == []
    assert ("stop", "musicbox") not in [c[:2] for c in svctl.calls]


def test_preview_returns_note_when_resident_running(env_file, svctl):
    svctl.state["musicbox"] = "RUNNING"
    webui._resident_state["active"] = True
    _mock_http(logged_in=True)
    with authed_client() as client:
        r = client.post("/api/preview", json={"provider": "musicbox"})
        assert r.status_code == 200
        assert r.json()["preview"] is False
        assert "常驻" in r.json()["note"]
