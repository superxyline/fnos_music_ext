"""webui-service 小爱音箱（xiaomusic）配置页回归：入口 / Schema / 进程起停 / 代理转发。

supervisorctl 与桥接 HTTP 均离线：前者 monkeypatch，后者 MockTransport。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"


def _load_webui():
    # 独立模块名加载：与 test_webui.py 的同名加载互不覆盖（各自持有 app 实例）
    spec = importlib.util.spec_from_file_location("webui_service_app_xm", HERE / "app.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webui_service_app_xm"] = mod
    spec.loader.exec_module(mod)
    return mod


webui = _load_webui()


# ------------------------------------------------------------------ 静态入口 --

def test_xiaomusic_entry_in_both_navs():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert html.count('data-page="xiaomusic"') == 2, "桌面侧栏与移动底栏都要有「小爱音箱」入口"
    assert 'id="page-xiaomusic"' in html
    assert 'id="xm-enabled"' in html


def test_done_page_renders_two_independent_health_rows():
    """完成页必须有两个**独立**状态项（口令轮询 / 小米账号），而不是一行互斥提示。

    回归背景：早期实现只有一个 <p id="xm-mi-state"> 且 pull_ask_on 正常时只显示
    登录态那一句 —— 用户看不到「口令轮询状态」，误以为状态区没渲染。
    """
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'id="xm-h-poll"' in js, "完成页缺少口令轮询状态行"
    assert 'id="xm-h-mi"' in js, "完成页缺少小米账号状态行"
    assert "xmSetHealth" in js
    # 两行各自独立判定：轮询看 pull_ask_on、账号看 mi_logged，不能写成 if/else 互斥
    body = js[js.index("function xmRefreshDone"):]
    body = body[:body.index('\n}')]
    assert "pull_ask_on" in body and "mi_logged" in body
    assert body.index("pull_ask_on") < body.index("mi_logged")
    # 旧的单行实现不应残留
    assert 'id="xm-mi-state"' not in js
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    for cls in (".xm-health-item", ".xm-health-item.ok", ".xm-health-item.err"):
        assert cls in css, f"样式缺少 {cls}"



def test_done_page_renders_two_independent_health_rows():
    """完成页必须有两个**独立**状态项（口令轮询 / 小米账号），而不是一行互斥提示。

    回归背景：早期实现只有一个 <p id="xm-mi-state"> 且 pull_ask_on 正常时只显示
    登录态那一句 —— 用户看不到「口令轮询状态」，误以为状态区没渲染。
    """
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'id="xm-h-poll"' in js, "完成页缺少口令轮询状态行"
    assert 'id="xm-h-mi"' in js, "完成页缺少小米账号状态行"
    assert "xmSetHealth" in js
    # 两行各自独立判定：轮询看 pull_ask_on、账号看 mi_logged，不能写成 if/else 互斥
    body = js[js.index("function xmRefreshDone"):]
    body = body[:body.index("\n}")]
    assert "pull_ask_on" in body and "mi_logged" in body
    assert body.index("pull_ask_on") < body.index("mi_logged")
    # 旧的单行实现不应残留
    assert 'id="xm-mi-state"' not in js
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    for cls in (".xm-health-item", ".xm-health-item.ok", ".xm-health-item.err"):
        assert cls in css, f"样式缺少 {cls}"



def test_xiaomusic_schema_key():
    spec = webui.SCHEMA["FNMUSIC_XIAOMUSIC_ENABLED"]
    assert spec["kind"] == "bool"
    assert spec["default"] == "false"
    assert spec["reload"] == "process"
    assert spec["group"] == "xiaomusic"


# ------------------------------------------------------------------ 夹具 --

@pytest.fixture
def env_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("FNMUSIC_MUSICDL_ENABLED='true'\n", encoding="utf-8")
    (tmp_path / "VERSION").write_text("3.3.0\n", encoding="utf-8")
    monkeypatch.setattr(webui, "ENV_PATH", env)
    monkeypatch.setattr(webui, "VERSION_PATH", tmp_path / "VERSION")
    webui._preview_until.clear()  # 预览状态是模块级全局，用例间隔离
    return env


@pytest.fixture
def svctl(monkeypatch):
    calls: list[tuple] = []

    def fake(*args, timeout=20.0):
        calls.append(tuple(args))
        if args[0] == "status":
            return 3, ("xiaomusic                       STOPPED   Not started\n"
                       "webui                          RUNNING   pid 8, uptime 0:01:00")
        return 0, f"{args[0]}ed {args[1]}"

    monkeypatch.setattr(webui, "supervisorctl", fake)
    return calls


def _mock_http(handler):
    webui.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ------------------------------------------------------------------ 进程起停 --

def test_put_enable_starts_xiaomusic(env_file, svctl):
    with TestClient(webui.app) as client:
        r = client.put("/api/config", json={"values": {"FNMUSIC_XIAOMUSIC_ENABLED": True}})
    assert r.status_code == 200
    assert {"kind": "process", "program": "xiaomusic", "op": "start",
            "ok": True, "error": ""} in r.json()["actions"]
    assert ("start", "xiaomusic") in svctl


def test_put_disable_stops_xiaomusic(env_file, svctl):
    with TestClient(webui.app) as client:
        client.put("/api/config", json={"values": {"FNMUSIC_XIAOMUSIC_ENABLED": True}})
        r = client.put("/api/config", json={"values": {"FNMUSIC_XIAOMUSIC_ENABLED": False}})
    assert r.status_code == 200
    assert {"kind": "process", "program": "xiaomusic", "op": "stop",
            "ok": True, "error": ""} in r.json()["actions"]


# ------------------------------------------------------------------ 代理转发 --

def test_xiaomusic_status_merges_health_and_wizard(env_file, svctl):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={
                "ok": True, "favorites": 12, "playlist_songs": 12, "push_ok": True,
                "last_error": "", "nas_ip": "192.168.x.x", "public_port": 8775})
        if request.url.path == "/api/wizard/state":
            return httpx.Response(200, json={
                "step": 3, "nas_ip": "192.168.x.x",
                "fnmusic": {"fnmusic_base_url": "http://192.168.x.x:5666",
                            "fnmusic_username": "u"}})
        return httpx.Response(404, json={"error": "unexpected path"})

    _mock_http(handler)
    with TestClient(webui.app) as client:
        r = client.get("/api/xiaomusic/status")
    assert r.status_code == 200
    rj = r.json()
    assert rj["favorites"] == 12
    assert rj["wizard"]["step"] == 3


def test_xiaomusic_proxy_unreachable_returns_503(env_file, svctl):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _mock_http(handler)
    with TestClient(webui.app) as client:
        r = client.get("/api/xiaomusic/status")
    assert r.status_code == 503
    assert "桥接服务不可达" in r.json()["error"]


def test_xiaomusic_wizard_devices_passthrough(env_file, svctl):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["method"] = request.method
        return httpx.Response(200, json={"ok": True, "devices": [{"deviceID": "x", "did": "y"}]})

    _mock_http(handler)
    with TestClient(webui.app) as client:
        r = client.get("/api/xiaomusic/wizard/devices")
    assert r.status_code == 200
    assert seen["path"] == "/api/wizard/devices"
    assert seen["method"] == "GET"
    assert r.json()["devices"][0]["did"] == "y"


def test_status_includes_xiaomusic_process(env_file, svctl):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    _mock_http(handler)
    with TestClient(webui.app) as client:
        r = client.get("/api/status")
    rj = r.json()
    assert "xiaomusic" in rj["processes"]
    assert rj["processes"]["xiaomusic"]["state"] == "STOPPED"
    # 未启用：不探活，标注按需未启动
    assert rj["services"]["xiaomusic"]["reachable"] is False
    assert "未启用" in rj["services"]["xiaomusic"]["note"]


def test_no_xiaomusic_advanced_settings_entry():
    """完成页不得残留「xiaomusic 高级设置」入口（2026-10-20 移除，易用性）。

    该入口指向 xiaomusic 引擎自带的 /xm/ 页面——引擎已整体移除，入口必须
    一起删掉，否则用户点进去 404。
    """
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "高级设置" not in app_js
    assert "/xm/" not in app_js
    assert "xmUrl" not in app_js


# --------------------------------------------------- 正在播放 + 口令介绍 --

def test_done_page_renders_now_playing_card():
    """完成页要有「正在播放」卡片：歌名 + 来源徽标（本地曲库 / 在线 · XX）。

    2026-10-28 用户需求：接管期间打开小爱音箱页，能看到当前播的歌与来源。
    """
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'id="xm-now-name"' in js, "缺少当前歌曲歌名节点"
    assert 'id="xm-now-src"' in js, "缺少来源徽标节点"
    assert "正在播放" in js
    # 刷新入口必须挂在轮询回调里（否则只有首次渲染，不跟随播放变化）
    body = js[js.index("function xmRefreshDone"):]
    body = body[:body.index("\n}")]
    assert "xmRefreshNow(" in body, "轮询/首次渲染都必须刷新当前播放"
    # 来源徽标按 source 区分配色（本地曲库绿色，在线源强调色）
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".xm-now-badge" in css
    assert '.xm-now-badge[data-source="local"]' in css


def test_done_page_documents_seq_play_command():
    """「顺序播放本地收藏」必须写进口令介绍（连同整张循环与排序口径）。"""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "顺序播放本地收藏" in js, "口令表缺少顺序播放"
    assert "整张放完再从头循环" in js or "整张放完" in js, "要写清播完循环"
    assert "最近收藏的在前" in js, "要写清排序口径（最新收藏在前）"
    # 顶部开关说明也提一句，用户不进完成页也能看到
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "顺序播放本地收藏" in html


# --------------------------------------------------- 正在播放 + 口令介绍 --

def test_done_page_renders_now_playing_card():
    """完成页要有「正在播放」卡片：歌名 + 来源徽标（本地曲库 / 在线 · XX）。

    2026-10-28 用户需求：接管期间打开小爱音箱页，能看到当前播的歌与来源。
    """
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'id="xm-now-name"' in js, "缺少当前歌曲歌名节点"
    assert 'id="xm-now-src"' in js, "缺少来源徽标节点"
    assert "正在播放" in js
    # 刷新入口必须挂在轮询回调里（否则只有首次渲染，不跟随播放变化）
    body = js[js.index("function xmRefreshDone"):]
    body = body[:body.index("\n}")]
    assert "xmRefreshNow(" in body, "轮询/首次渲染都必须刷新当前播放"
    # 来源徽标按 source 区分配色（本地曲库绿色，在线源强调色）
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".xm-now-badge" in css
    assert '.xm-now-badge[data-source="local"]' in css


def test_done_page_documents_seq_play_command():
    """「顺序播放本地收藏」必须写进口令介绍（连同整张循环与排序口径）。"""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "顺序播放本地收藏" in js, "口令表缺少顺序播放"
    assert "整张放完再从头循环" in js or "整张放完" in js, "要写清播完循环"
    assert "最近收藏的在前" in js, "要写清排序口径（最新收藏在前）"
    # 顶部开关说明也提一句，用户不进完成页也能看到
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "顺序播放本地收藏" in html


def test_done_page_documents_singer_playlist_command():
    """「播放X的本地收藏歌曲」也要写进口令介绍（歌手合辑：随机打乱连播）。"""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "播放周杰伦的本地收藏歌曲" in js, "口令表缺少按歌手播放"
    assert "歌手合辑" in js
    assert "随机打乱" in js
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "播放周杰伦的本地收藏歌曲" in html


def test_done_page_documents_singer_playlist_command():
    """「播放X的本地收藏歌曲」也要写进口令介绍（歌手合辑：随机打乱连播）。"""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "播放周杰伦的本地收藏歌曲" in js, "口令表缺少按歌手播放"
    assert "歌手合辑" in js
    assert "随机打乱" in js
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "播放周杰伦的本地收藏歌曲" in html


def test_xiaomusic_switch_marks_dirty_so_save_bar_appears():
    """勾选「接管小爱音箱音乐播放」必须弹出底部保存条。

    2026-10-29 用户反馈（分发给他人的实例）：勾上后提示要保存，但页面上
    找不到保存按钮——保存条只在 markDirty() 后才从底部滑出，而这个
    checkbox 当时没绑 change 事件，所以永远不会出现。
    """
    import re

    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")

    # 直接断言这个开关接了脏标记
    assert re.search(
        r'\$\("#xm-enabled"\)\.addEventListener\("change".*markDirty', js
    ), "xm-enabled 没绑定 markDirty，勾选后保存条不会出现"

    # 通用防护：所有 switch-row 里的开关都必须在 markDirty 上下文里出现
    blocks = re.findall(r'<label class="switch-row">(.*?)</label>', html, re.S)
    ids = []
    for block in blocks:
        ids.extend(re.findall(r'id="([a-z0-9-]+)"', block))
    assert ids, "index.html 里没解析到任何 switch-row 开关"

    lines = js.splitlines()
    ctx = []
    for n, line in enumerate(lines):
        if "markDirty" in line:
            ctx.extend(lines[max(0, n - 3): n + 2])
    ctx_text = "\n".join(ctx)
    for i in ids:
        assert f'"#{i}"' in ctx_text, f"开关 {i} 未绑定脏标记：勾选后保存条不会出现"


def test_xiaomusic_switch_marks_dirty_so_save_bar_appears():
    """勾选「接管小爱音箱音乐播放」必须弹出底部保存条。

    2026-10-29 用户反馈（分发给他人的实例）：勾上后提示要保存，但页面上
    找不到保存按钮——保存条只在 markDirty() 后才从底部滑出，而这个
    checkbox 当时没绑 change 事件，所以永远不会出现。
    """
    import re

    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")

    # 直接断言这个开关接了脏标记
    assert re.search(
        r'\$\("#xm-enabled"\)\.addEventListener\("change".*markDirty', js
    ), "xm-enabled 没绑定 markDirty，勾选后保存条不会出现"

    # 通用防护：所有 switch-row 里的开关都必须在 markDirty 上下文里出现
    blocks = re.findall(r'<label class="switch-row">(.*?)</label>', html, re.S)
    ids = []
    for block in blocks:
        ids.extend(re.findall(r'id="([a-z0-9-]+)"', block))
    assert ids, "index.html 里没解析到任何 switch-row 开关"

    lines = js.splitlines()
    ctx = []
    for n, line in enumerate(lines):
        if "markDirty" in line:
            ctx.extend(lines[max(0, n - 3): n + 2])
    ctx_text = "\n".join(ctx)
    for i in ids:
        assert f'"#{i}"' in ctx_text, f"开关 {i} 未绑定脏标记：勾选后保存条不会出现"
