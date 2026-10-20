"""飞牛音乐客户端与向导扫描的回归测试（GitHub issue #5）。

背景：fnOS 网页入口默认强制 HTTPS——http://<ip>:5666/* 一律 302 到
https://<ip>:5667/*（自签证书）。桥接原本 follow_redirects=False 且未关
TLS 校验，导致：
- 向导第 3 步填 http://:5666 → 登录吃 302（非 JSON）→ "登录失败"；
  填 https://:5667 → 自签证书 TLS 校验失败；
- scan_fnmusic 要求 200 且不跟随重定向 → found 永远为空。

修复：客户端 verify=False + 登录前一次性探测重定向升级 base_url
（全局仍不跟随，保留 stream 跨源剥离凭据的设计）；扫描跟随重定向并返回
最终源站。本文件用两个本地 HTTP 服务器真实复现「A 站 302 → B 站」场景。
"""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_PY = (HERE / "app.py").read_text(encoding="utf-8")
CLIENT_PY = (HERE / "fnmusic_client.py").read_text(encoding="utf-8")


# ---------------- 静态断言 ----------------

def test_client_disables_tls_verify():
    """fnOS 默认自签证书；桥接→飞牛是内网跳板，关 TLS 校验。"""
    assert "verify=False" in CLIENT_PY


def test_client_ignores_env_proxy():
    """不继承环境代理：飞牛与桥接同机，直连即可。

    容器里常为在线源设 HTTP_PROXY——被继承会让内网请求绕去代理，
    慢且可能被拦截（实测测试环境因此 10s 超时）。
    """
    assert "trust_env=False" in CLIENT_PY
    scan = APP_PY[APP_PY.index("async def scan_fnmusic("):]
    scan = scan[:scan.index("\n\n")]
    assert "trust_env=False" in scan


def test_client_keeps_no_global_redirects():
    """全局仍不跟随重定向——open_stream 靠它做跨源跳转剥离凭据。"""
    assert "follow_redirects=False" in CLIENT_PY


def test_client_normalizes_base_url_on_login():
    """login 前一次性探测重定向并升级 base_url（token 检查之后、发登录请求之前）。"""
    assert "async def _normalize_base_url_once" in CLIENT_PY
    body = CLIENT_PY[CLIENT_PY.index("async def _normalize_base_url_once"):]
    body = body[:body.index("\n    async def login(")]
    assert "follow_redirects=True" in body
    assert "自动升级 base_url" in body
    # 只做一次（重复登录不重复探测）
    assert "if self._base_normalized:" in body
    login = CLIENT_PY[CLIENT_PY.index("async def login("):]
    login = login[:login.index("\n    # ---------- 业务接口")]
    assert "_normalize_base_url_once()" in login
    assert login.index("_normalize_base_url_once()") < login.index(
        'path = "/user/password-login"')


def test_scan_fnmusic_follows_redirects_and_resolves():
    """扫描必须跟随重定向 + 关 TLS 校验，并返回最终源站地址。"""
    scan = APP_PY[APP_PY.index("async def scan_fnmusic("):]
    scan = scan[:scan.index("\n\n")]
    assert "follow_redirects=True" in scan
    assert "verify=False" in scan
    # 跟随后的最终源站（强制 HTTPS 时即 https://:5667）而非原始候选地址
    assert "urlparse(str(r.url))" in scan


def test_wizard_persists_resolved_base_url():
    """向导保存的是登录后的实际地址（自动升级过的不被丢）。"""
    body = APP_PY[APP_PY.index("async def wizard_fnmusic("):]
    body = body[:body.index("\n\n")]
    assert '"fnmusic_base_url": client.base_url' in body


# ---------------- 集成：真实「A 站 302 → B 站」 ----------------

def test_client_upgrades_base_url_through_redirect():
    """A 站把所有请求 302 到 B 站：客户端登录成功后 base_url 必须升级为 B。"""
    from fnmusic_client import FnMusicClient

    state = {"login_hits": 0, "state_hits": 0}

    class FinalHandler(BaseHTTPRequestHandler):
        """最终源站：正常返回 JSON（对应升级后的 https://:5667）"""

        def log_message(self, *args):
            pass

        def _json(self, obj):
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/music/api/v1/initialization/state":
                state["state_hits"] += 1
                self._json({"code": 0, "data": {"initialized": True}})
            elif path == "/music/api/v1/favorite-track/list":
                self._json({"code": 0, "data": {
                    "list": [{"guid": "abc", "title": "晴天",
                              "artists": [{"name": "周杰伦"}]}],
                    "total": 1}})
            else:
                self.send_error(404)

        def do_POST(self):
            # 必须消费请求体：keep-alive 连接上不读 body 会污染同连接下一个
            # 请求（httpx ReadError，2026-10-20 实测偶发）
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            state["login_hits"] += 1
            self._json({"code": 0, "data": {"token": "test-token-123"}})

    class RedirectHandler(BaseHTTPRequestHandler):
        """入口站：一律 302 到最终站（对应强制 HTTPS 的 http://:5666）"""

        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{final_port}{self.path}")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{final_port}{self.path}")
            self.send_header("Content-Length", "0")
            self.end_headers()

    final_server = ThreadingHTTPServer(("127.0.0.1", 0), FinalHandler)
    final_port = final_server.server_address[1]
    redirect_server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    entry_port = redirect_server.server_address[1]

    threading.Thread(target=final_server.serve_forever, daemon=True).start()
    threading.Thread(target=redirect_server.serve_forever, daemon=True).start()

    async def _run():
        client = FnMusicClient(f"http://127.0.0.1:{entry_port}", "u", "p")
        try:
            await client.login()
            favorites = await client.get_favorites()
        finally:
            await client.aclose()
        return client, favorites

    try:
        client, favorites = asyncio.run(_run())
    finally:
        final_server.shutdown()
        redirect_server.shutdown()

    # base_url 已升级为最终源站
    assert client.base_url == f"http://127.0.0.1:{final_port}"
    # 登录与拉收藏都成功（没有因 302 报"响应不是 JSON"）
    assert client.token == "test-token-123"
    assert len(favorites) == 1 and favorites[0]["title"] == "晴天"
    # 登录 POST 直达最终站（入口站一次都没收到过 POST）
    assert state["login_hits"] == 1
    # 探测（initialization/state 在入口站 302 后被跟随到最终站）只发生一次
    assert state["state_hits"] == 1
