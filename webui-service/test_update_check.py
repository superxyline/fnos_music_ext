"""检查更新（B3）：双源回退、缓存/负缓存、版本比较、changelog 清洗。

上游 javycoder 的实现原样移植，仓库地址改成本仓库；测试覆盖上游声明的
9 个后端用例等价场景。
"""
from __future__ import annotations

import importlib.util
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

GH = "api.github.com"
GITEE = "gitee.com"


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    webui._update_cache.clear()
    yield
    webui._update_cache.clear()


@pytest.fixture
def client():
    with TestClient(webui.app) as c:
        c.headers.update({"X-Trim-Isadmin": "true"})
        yield c


def _mock(handler):
    webui.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("latest,current,expected", [
    ("2.9.0", "2.8.3", True),
    ("2.9", "2.9.0", False),      # 逐位补零：2.9 ≡ 2.9.0
    ("2.10.0", "2.9.9", True),
    ("v3.1.2", "3.1.2", False),
    ("2.8.0", "2.8.0a", False),   # 预发布尾巴比正式版旧
    ("2.8.0b", "2.8.0a", True),
    ("0.0.0", "3.1.2", False),
    ("abc", "3.1.2", False),
])
def test_version_newer(latest, current, expected):
    assert webui._version_newer(latest, current) is expected


def test_strip_release_body_removes_markdown():
    body = "## 2.9.0\n\n### 新增\n\n- **粗体** 与 `代码` 以及 [链接文字](https://x.y)\n\n\n尾行"
    out = webui._strip_release_body(body)
    assert "##" not in out and "**" not in out and "`" not in out
    assert "链接文字" in out and "https://x.y" not in out
    assert "\n\n\n" not in out


def test_update_check_prefers_github(client):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == GH
        return httpx.Response(200, json={
            "tag_name": "v9.9.9", "name": "9.9.9", "published_at": "2026-10-15T00:00:00Z",
            "body": "## 9.9.9\n- 新特性", "html_url": "https://github.com/x/y/releases/tag/v9.9.9",
        })

    _mock(handler)
    data = client.get("/api/update-check").json()
    assert data["ok"] is True
    assert data["source"] == "github"
    assert data["has_update"] is True
    assert data["latest"]["version"] == "9.9.9"
    assert "##" not in data["latest"]["changelog"]


def test_update_check_falls_back_to_gitee(client):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == GH:
            return httpx.Response(403, json={"message": "rate limited"})
        assert request.url.host == GITEE
        return httpx.Response(200, json={"tag_name": "v3.2.0", "body": "x"})

    _mock(handler)
    data = client.get("/api/update-check").json()
    assert data["source"] == "gitee"
    assert data["has_update"] is True
    assert data["latest"]["version"] == "3.2.0"


def test_update_check_both_fail_is_silent(client):
    _mock(lambda request: httpx.Response(503))
    resp = client.get("/api/update-check")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True and data["has_update"] is False
    assert "error" in data


def test_gitee_null_body_treated_as_failure(client):
    """gitee 对「无发行版」返回 200 + null，必须当失败而不是崩溃。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == GH:
            return httpx.Response(403)
        return httpx.Response(200, content=b"null")

    _mock(handler)
    data = client.get("/api/update-check").json()
    assert data["has_update"] is False
    assert data["ok"] is True


def test_success_is_cached_and_not_refetched(client):
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        return httpx.Response(200, json={"tag_name": "v9.9.9", "body": ""})

    _mock(handler)
    first = client.get("/api/update-check").json()
    second = client.get("/api/update-check").json()
    assert first["source"] == "github"
    assert second["source"] == "github"
    assert hits["n"] == 1, "成功结果 6h 内不得重复出网"


def test_failure_negative_cache_expires(client, monkeypatch):
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        return httpx.Response(503)

    _mock(handler)
    client.get("/api/update-check")
    assert hits["n"] == 2  # 两个源各一次
    client.get("/api/update-check")
    assert hits["n"] == 2, "失败负缓存 10 分钟内不得重试"

    real = webui.time.time
    monkeypatch.setattr(webui.time, "time", lambda: real() + webui._UPDATE_FAIL_TTL + 1)
    client.get("/api/update-check")
    assert hits["n"] == 4, "负缓存过期后重新出网"


def test_cache_hit_recomputes_has_update_after_local_upgrade(client, monkeypatch):
    """本地升级后缓存未过期：has_update 必须按当前版本重算，NEW 不诈尸。

    2026-10-22 实测：热部署 3.4.2→3.5.0 后 webui 进程没重启，内存缓存停在
    升级前的版本对比，NEW 一直亮到 6h TTL 过期——用户看到"已是最新版还提示
    升级"。真源数据（latest）仍按 TTL 缓存不出网，只按当前版本重算结论。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tag_name": "v3.5.0", "body": "x"})

    _mock(handler)
    monkeypatch.setattr(webui, "_read_version", lambda: "3.4.2")
    first = client.get("/api/update-check").json()
    assert first["has_update"] is True

    # 模拟本地升级：VERSION 变了，缓存还在 TTL 内（不得重新出网）
    hits = {"n": 0}

    def counting(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        return httpx.Response(200, json={"tag_name": "v3.5.0", "body": "x"})

    _mock(counting)
    monkeypatch.setattr(webui, "_read_version", lambda: "3.5.0")
    second = client.get("/api/update-check").json()

    assert hits["n"] == 0, "缓存命中不得重新出网"
    assert second["has_update"] is False, "升级后必须立刻熄灭 NEW"
    assert second["current_version"] == "3.5.0"
    assert second["latest"]["version"] == "3.5.0", "真源数据仍走缓存"


def test_update_check_returns_both_mirrors(client):
    """升级弹窗要能自选下载源：latest.mirrors 必须同时给 GitHub/Gitee。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tag_name": "v9.9.9", "body": ""})

    _mock(handler)
    data = client.get("/api/update-check").json()
    mirrors = data["latest"]["mirrors"]
    assert [m["name"] for m in mirrors] == ["GitHub", "Gitee"]
    urls = {m["name"]: m["url"] for m in mirrors}
    assert urls["GitHub"].endswith("/releases/tag/v9.9.9")
    assert "gitee.com/superxyline/fnos_music_ext" in urls["Gitee"]
