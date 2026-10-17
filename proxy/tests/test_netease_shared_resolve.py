"""网易共享取链与首播预热回归（B5：上游 cc4d92f 安全子集 / issue #56）。

锁定行为：
- 同曲并发只解析一次，所有等待者拿同一条 rendition（防换流断流）
- 整条音质阶梯共用一个预算，超预算按失败返回（不再 5 档 x 5s 串行吃满起播耐心）
- 预热钉链单独记账，音质配置变更时统一下掉
- metadata 端点对网易曲目触发预热，非网易曲目不动
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import proxy.app as pa
from proxy.app import CONF, app, resolve_netease_url


def _client(handler, base_url="http://127.0.0.1:8770"):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=base_url)


@pytest.mark.anyio
async def test_concurrent_resolve_shares_one_upstream_call():
    calls = []

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params.get("quality"))
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"ok": True, "data": {"code": 200, "url": "http://x/flac"}})

    async with _client(slow_handler) as client:
        results = await asyncio.gather(*[resolve_netease_url(client, "9001") for _ in range(3)])
    assert results == ["http://x/flac"] * 3
    assert calls == ["lossless"], "同曲并发应只解析一次（首个档位命中即止）"


@pytest.mark.anyio
async def test_resolve_budget_timeout_returns_none(monkeypatch):
    monkeypatch.setattr(pa, "_NETEASE_URL_BUDGET_S", 0.02)

    async def stuck_handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.5)
        return httpx.Response(200, json={"ok": True, "data": {"code": 200, "url": "http://x/late"}})

    async with _client(stuck_handler) as client:
        assert await resolve_netease_url(client, "9002") is None
    # 超预算不该留下钉链（下次重试要能重新出网）
    assert "9002" not in pa._NETEASE_URL_PIN


@pytest.mark.anyio
async def test_waiter_survives_leader_cancellation():
    """调用方取消（App 频繁 seek）不得打断共享任务，其他等待者仍拿到结果。"""
    started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.sleep(0.08)
        return httpx.Response(200, json={"ok": True, "data": {"code": 200, "url": "http://x/keep"}})

    async with _client(handler) as client:
        first = asyncio.create_task(resolve_netease_url(client, "9003"))
        await started.wait()
        second = asyncio.create_task(resolve_netease_url(client, "9003"))
        await asyncio.sleep(0.01)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert await second == "http://x/keep"


@pytest.mark.anyio
async def test_prefetch_records_preheated_pin_and_quality_change_drops_it(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "data": {"code": 200, "url": "http://x/pre"}})

    async with _client(handler) as client:
        url = await resolve_netease_url(client, "9004", prefetch=True)
    assert url == "http://x/pre"
    assert "9004" in pa._NETEASE_PREHEATED_PINS
    assert pa._NETEASE_URL_PIN["9004"][0] == "http://x/pre"

    # 音质配置变更：预热钉链必须下掉（旧 rendition 不可比）
    monkeypatch.setitem(CONF, "netease_quality", "exhigh")
    pa._sync_netease_quality()
    assert "9004" not in pa._NETEASE_URL_PIN
    assert not pa._NETEASE_PREHEATED_PINS
    monkeypatch.setitem(CONF, "netease_quality", "lossless")
    pa._sync_netease_quality()


@pytest.mark.anyio
async def test_normal_play_clears_preheated_marker(monkeypatch):
    """真实播放请求命中预热钉链时，该钉链不再算「预热专属」（音质变更不该连带下掉）。"""
    pa._NETEASE_URL_PIN["9005"] = ("http://x/hot", time.monotonic() + 600)
    pa._NETEASE_PREHEATED_PINS["9005"] = pa._NETEASE_URL_PIN["9005"]
    async with _client(lambda request: httpx.Response(500)) as client:
        assert await resolve_netease_url(client, "9005") == "http://x/hot"
    assert "9005" not in pa._NETEASE_PREHEATED_PINS
    assert "9005" in pa._NETEASE_URL_PIN


@pytest.mark.anyio
async def test_prefetch_skips_non_netease_and_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(pa, "_source_enabled", lambda guid: True)
    monkeypatch.setattr(pa, "find_cache_file", lambda guid: None)
    monkeypatch.setattr(pa, "resolve_netease_url",
                        lambda *a, **kw: calls.append((a, kw)) or asyncio.sleep(0))

    class _Req:
        class app:
            class state:
                musicbox_client = None

    pa._prefetch_netease(_Req(), "online:lx:kg:HASH")   # 非网易：不预热
    pa._prefetch_netease(_Req(), "online:netease:9006")  # 网易：登记预热
    await asyncio.sleep(0)          # 让预热任务真正启动
    assert len(calls) == 1
    assert calls[0][1].get("prefetch") is True


def test_track_metadata_triggers_prefetch_for_netease_only(monkeypatch):
    seen = []
    monkeypatch.setattr(pa, "_prefetch_netease", lambda request, guid: seen.append(guid))
    async def fake_online_info(*args, **kwargs):
        return _fake_info()

    monkeypatch.setattr(pa, "_online_info", fake_online_info)
    monkeypatch.setattr(pa, "_source_enabled", lambda guid: True)
    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"code": 0, "data": {}})),
        base_url="http://unix",
    )
    with TestClient(app) as client:
        client.get("/music/api/v1/track/metadata/online:netease:9007")
        client.get("/music/api/v1/track/metadata/online:kuwo:9008")
    assert seen == ["online:netease:9007"]


def _fake_info() -> dict:
    return {
        "title": "晴天", "artist": "周杰伦", "album": "叶惠美",
        "duration_s": 269.0, "cover_url": "http://img/a.jpg", "lyric": "",
    }
