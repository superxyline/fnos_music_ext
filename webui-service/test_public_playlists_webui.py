"""公共歌单（issue #55）WebUI 侧回归。

锁定行为：
- 发现代理：白名单目标路径、provider 仅 musicbox、列表短缓存 / 曲目与能力声明不缓存
- 服务不可达返回 503 + hint，前端可提示「先启用网易盒子」
- 常驻判定扩展：公共歌单选了 musicbox 歌单即需要常驻（账号歌单可关闭）
- 选择串默认值与 proxy publicplaylists.parse_selection 同口径
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
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
FNMUSIC_NETEASE_MY_PLAYLISTS='false'
"""

SELECTION = [{"provider": "musicbox", "source": "netease", "kind": "chart", "id": "3778678"}]


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(BASE_ENV, encoding="utf-8")
    (tmp_path / "VERSION").write_text("3.1.2\n", encoding="utf-8")
    monkeypatch.setattr(webui, "ENV_PATH", env)
    monkeypatch.setattr(webui, "VERSION_PATH", tmp_path / "VERSION")
    webui._public_discovery_cache.clear()
    webui._public_discovery_locks.clear()
    webui._preview_until.clear()
    yield env
    webui._public_discovery_cache.clear()
    webui._public_discovery_locks.clear()


def _values(monkeypatch, env_file, **updates):
    """写入 .env 后读取（保持「键缺失」语义：值为 None 表示删除该键）。"""
    lines = []
    for key, val in updates.items():
        if val is None:
            continue
        lines.append(f"{key}='{val}'")
    env_file.write_text(BASE_ENV + "\n".join(lines) + "\n", encoding="utf-8")
    return webui.read_env()


def _mock_musicbox(handler=None, state=None):
    state = state if state is not None else {"calls": 0}

    def default(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if handler is not None:
            return handler(request)
        if request.url.path.endswith("/categories"):
            return httpx.Response(200, json={"ok": True, "categories": [{"id": "", "name": "全部"}]})
        if request.url.path.endswith("/tracks"):
            return httpx.Response(200, json={"ok": True, "data": [{"song_id": 101, "name": "晴天"}]})
        if request.url.path.endswith("/capabilities"):
            return httpx.Response(200, json={"ok": True, "sources": [{"id": "netease"}]})
        return httpx.Response(200, json={"ok": True, "items": [{"id": "3778678"}], "has_more": False})

    webui.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(default))
    return state


# ------------------------------------------------------------------ 常驻判定 --

def test_public_playlists_need_musicbox_resident(env_file, monkeypatch):
    """账号歌单关闭、只开公共歌单：仍需要 musicbox 常驻（否则取流链路不在）。"""
    values = _values(
        monkeypatch, env_file,
        FNMUSIC_PUBLIC_PLAYLISTS_ENABLED="true",
        FNMUSIC_PUBLIC_PLAYLISTS_SELECTION=json.dumps(SELECTION, ensure_ascii=False),
    )
    assert webui._resident_active(values) is True


def test_public_playlists_disabled_does_not_pin(env_file, monkeypatch):
    values = _values(
        monkeypatch, env_file,
        FNMUSIC_PUBLIC_PLAYLISTS_ENABLED="false",
        FNMUSIC_PUBLIC_PLAYLISTS_SELECTION=json.dumps(SELECTION),
    )
    assert webui._resident_active(values) is False


def test_public_playlists_explicit_resident_off(env_file, monkeypatch):
    values = _values(
        monkeypatch, env_file,
        FNMUSIC_PUBLIC_PLAYLISTS_ENABLED="true",
        FNMUSIC_PUBLIC_PLAYLISTS_SELECTION=json.dumps(SELECTION),
        FNMUSIC_NETEASE_PLAYLIST_RESIDENT="false",
    )
    assert webui._resident_active(values) is False


def test_empty_selection_does_not_pin(env_file, monkeypatch):
    """显式清空选择（"[]"）后不再需要常驻。"""
    values = _values(
        monkeypatch, env_file,
        FNMUSIC_PUBLIC_PLAYLISTS_ENABLED="true",
        FNMUSIC_PUBLIC_PLAYLISTS_SELECTION="[]",
    )
    assert webui._resident_active(values) is False


def test_selection_rows_default_matches_proxy(env_file, monkeypatch):
    """键缺失 -> 默认榜单（proxy 侧空选择同口径，两边常驻判定必须一致）。"""
    values = _values(monkeypatch, env_file, FNMUSIC_PUBLIC_PLAYLISTS_ENABLED="true")
    values.pop("FNMUSIC_PUBLIC_PLAYLISTS_SELECTION", None)
    assert webui._public_selection_rows(values) == [
        {"provider": "musicbox", "source": "netease", "kind": "chart", "id": "3778678"}
    ]
    assert webui._public_musicbox_needed(values) is True


def test_selection_rows_invalid_json_is_empty(env_file, monkeypatch):
    values = _values(
        monkeypatch, env_file,
        FNMUSIC_PUBLIC_PLAYLISTS_ENABLED="true",
        FNMUSIC_PUBLIC_PLAYLISTS_SELECTION="{not json",
    )
    assert webui._public_selection_rows(values) == []
    assert webui._public_musicbox_needed(values) is False


# ------------------------------------------------------------------ 发现代理 --

def test_list_is_cached_but_capabilities_and_tracks_are_not(env_file, monkeypatch):
    state = _mock_musicbox()
    with TestClient(webui.app) as client:
        for _ in range(2):
            resp = client.get("/api/public-playlists", params={"provider": "musicbox", "channel": "charts"})
            assert resp.status_code == 200
        assert state["calls"] == 1          # 列表命中缓存
        for _ in range(2):
            client.get("/api/public-playlists/capabilities", params={"provider": "musicbox"})
        assert state["calls"] == 3          # 能力声明不缓存（含登录态）
        for _ in range(2):
            client.get("/api/public-playlists/3778678/tracks", params={"provider": "musicbox"})
        assert state["calls"] == 5          # 曲目列表不缓存（体积大、时效强）


def test_public_provider_must_be_musicbox(env_file, monkeypatch):
    _mock_musicbox()
    with TestClient(webui.app) as client:
        resp = client.get("/api/public-playlists", params={"provider": "lxmusic"})
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


def test_unknown_target_is_rejected(env_file, monkeypatch):
    _mock_musicbox()
    with TestClient(webui.app) as client:
        resp = client.get("/api/public-playlists/a/b/c", params={"provider": "musicbox"})
    assert resp.status_code == 404


def test_musicbox_unreachable_returns_503_with_hint(env_file, monkeypatch):
    def boom(request):
        raise httpx.ConnectError("connection refused", request=request)

    _mock_musicbox(boom)
    with TestClient(webui.app, raise_server_exceptions=False) as client:
        resp = client.get("/api/public-playlists", params={"provider": "musicbox", "channel": "charts"})
    assert resp.status_code == 503
    body = resp.json()
    assert body["ok"] is False
    assert "hint" in body


def test_account_recommend_channel_bypasses_cache(env_file, monkeypatch):
    state = _mock_musicbox(lambda request: httpx.Response(
        200, json={"ok": True, "logged_in": True, "items": [{"id": "333"}]}))
    with TestClient(webui.app) as client:
        for _ in range(2):
            resp = client.get("/api/public-playlists",
                              params={"provider": "musicbox", "channel": "account_recommend"})
            assert resp.status_code == 200
    assert state["calls"] == 2   # 登录态相关结果不进缓存


# -------------------------------------------------------------------- 管理页 --

STATIC = HERE / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "style.css").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")


def test_html_has_browser_block():
    for el_id in ("public-playlists-enabled", "public-playlists-browser", "public-channel",
                  "public-category", "public-sort", "public-list", "public-selected",
                  "public-prev", "public-next", "public-page", "public-note", "public-refresh"):
        assert f'id="{el_id}"' in HTML, f"缺少元素 {el_id}"
    # 列表容器默认隐藏（勾选开关才展开）
    assert re.search(r'id="public-playlists-browser"[^>]*hidden', HTML)


def test_hidden_stack_has_css_fallback():
    """`.stack{display:flex}` 会压过浏览器对 hidden 的默认 display:none，必须兜底。"""
    assert re.search(r"\.stack\[hidden\]\s*\{[^}]*display:\s*none", CSS)


def test_js_persists_selection_and_dedupes_like_proxy():
    assert "FNMUSIC_PUBLIC_PLAYLISTS_SELECTION" in JS
    # 去重键与 proxy publicplaylists 同口径：musicbox 的 chart/playlist 视为同一份
    assert 'return [item.provider, item.source, "playlist", String(item.id)].join("|");' in JS
    # 选择上限与后端 _MAX_SELECTION 一致
    assert "publicSelection.length >= 50" in JS


def test_resident_switch_depends_on_both_sources():
    assert "function updateResidentAvailability()" in JS
    assert "publicMusicboxSelected()" in JS
