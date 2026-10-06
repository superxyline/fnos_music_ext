"""/api/musicdl/test 音源测试端点单测：MockTransport 假 musicdl，不触网。"""
import os
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as webui  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(webui, "preview_renew", lambda p: None)
    with TestClient(webui.app) as c:
        yield c


def _mock_get_http(handler):
    transport = httpx.MockTransport(handler)
    return lambda request: httpx.AsyncClient(transport=transport)


def test_summarize_groups_by_source_and_reports_errors():
    data = {
        "ok": True,
        "items": [
            {"source": "kuwo", "title": "晴天", "artist": "周杰伦"},
            {"source": "kuwo", "title": "晴天", "artist": "周杰伦", "_dup": 1},
            {"source": "migu", "title": "晴天 (Live)", "artist": "周杰伦"},
        ],
        "errors": {"kwdomain": "timeout after 10s"},
    }
    rows = webui._summarize_musicdl_search(data)
    by = {r["source"]: r for r in rows}
    assert by["kuwo"]["count"] == 2
    assert by["kuwo"]["error"] is None
    assert by["kuwo"]["samples"][0] == "晴天 - 周杰伦"
    assert by["migu"]["count"] == 1
    assert by["kwdomain"] == {"source": "kwdomain", "count": 0,
                              "samples": [], "error": "timeout after 10s"}


def test_summarize_empty_response():
    rows = webui._summarize_musicdl_search({"ok": True, "items": [], "errors": {}})
    assert rows == []


def test_endpoint_rejects_blank_keyword(client):
    resp = client.post("/api/musicdl/test", json={"keyword": "   "})
    assert resp.status_code == 400
    assert "歌名" in resp.json()["detail"]


def test_endpoint_passes_sources_and_summarizes(client, monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={
            "ok": True,
            "items": [{"source": "kuwo", "title": "晴天", "artist": "周杰伦"}],
            "errors": {},
        })

    monkeypatch.setattr(webui, "get_http", _mock_get_http(handler))
    resp = client.post("/api/musicdl/test",
                       json={"keyword": "晴天 周杰伦", "sources": ["kuwo", "migu"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["keyword"] == "晴天 周杰伦"
    assert "tookMs" in body
    assert "sources=kuwo%2Cmigu" in captured["url"] or "sources=kuwo,migu" in captured["url"]
    assert "limit=3" in captured["url"]
    assert body["platforms"][0]["source"] == "kuwo"
    assert body["platforms"][0]["count"] == 1


def test_endpoint_musicdl_down_is_502(client, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(webui, "get_http",
                        lambda request: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    resp = client.post("/api/musicdl/test", json={"keyword": "晴天"})
    assert resp.status_code == 502
    assert "不可达" in resp.json()["detail"]
