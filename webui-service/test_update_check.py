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
