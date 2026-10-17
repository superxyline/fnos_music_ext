"""issue #56：musicbox 有界异步共享取链。

上游 v2.9.0 用 EAPI 直连重写；本仓库沿用 NEMbox 进程内解析，只补协议无关的
三条收益：不阻塞事件循环、同曲同音质并发合并、调用者取消不打断共享工作。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
import time
import types
from pathlib import Path

import pytest

MB_DIR = Path(__file__).resolve().parents[2] / "musicbox-service"


def _load_playback():
    cached = sys.modules.get("musicbox_playback_under_test")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("musicbox_playback_under_test", MB_DIR / "playback.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["musicbox_playback_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


playback = _load_playback()


@pytest.fixture(autouse=True)
def _reset():
    playback.reset()
    yield
    playback.reset()


@pytest.fixture
def fake_ext(monkeypatch):
    """用替身 netease_ext 控制解析耗时与次数。"""
    state = {"calls": 0, "delay": 0.05, "result": {"url": "https://media.test/a.mp3"}}
    module = types.ModuleType("netease_ext")

    def get_song_url(song_id, quality, fresh=False):
        state["calls"] += 1
        time.sleep(state["delay"])
        return dict(state["result"])

    module.get_song_url = get_song_url
    monkeypatch.setitem(sys.modules, "netease_ext", module)
    return state


@pytest.mark.anyio
async def test_concurrent_same_key_shares_one_resolution(fake_ext):
    results = await asyncio.gather(*[
        playback.get_song_url_async(1001, "exhigh") for _ in range(5)
    ])
    assert all(r and r["url"] == "https://media.test/a.mp3" for r in results)
    assert fake_ext["calls"] == 1, "同曲同音质并发必须只解析一次"


@pytest.mark.anyio
async def test_different_quality_resolves_separately(fake_ext):
    await asyncio.gather(
        playback.get_song_url_async(1002, "exhigh"),
        playback.get_song_url_async(1002, "lossless"),
    )
    assert fake_ext["calls"] == 2, "不同音质档是独立的解析"


@pytest.mark.anyio
async def test_caller_cancel_does_not_cancel_shared_work(fake_ext):
    fake_ext["delay"] = 0.3
    first = asyncio.ensure_future(playback.get_song_url_async(1003, "exhigh"))
    await asyncio.sleep(0.05)
    second = asyncio.ensure_future(playback.get_song_url_async(1003, "exhigh"))
    await asyncio.sleep(0.05)
    first.cancel()  # 模拟客户端断开/App 换歌
    with pytest.raises(asyncio.CancelledError):
        await first
    result = await second
    assert result and result["url"] == "https://media.test/a.mp3"
    assert fake_ext["calls"] == 1, "取消一个等待者不得重新发起或打断共享解析"


@pytest.mark.anyio
async def test_timeout_raises_resolution_timeout(fake_ext, monkeypatch):
    monkeypatch.setattr(playback, "BUDGET_S", 0.1)
    fake_ext["delay"] = 0.6
    with pytest.raises(playback.ResolutionTimeout):
        await playback.get_song_url_async(1004, "exhigh")


@pytest.mark.anyio
async def test_inflight_cleared_after_completion(fake_ext):
    await playback.get_song_url_async(1005, "exhigh")
    await asyncio.sleep(0.01)
    assert playback.inflight() == 0


def test_no_blocking_event_loop(fake_ext):
    """解析在线程池里跑：事件循环在解析期间必须仍能推进其它协程。"""
    fake_ext["delay"] = 0.2
    ticks = {"n": 0}

    async def scenario():
        async def ticker():
            while True:
                ticks["n"] += 1
                await asyncio.sleep(0.01)

        task = asyncio.ensure_future(ticker())
        await playback.get_song_url_async(1006, "exhigh")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert ticks["n"] > 3, "同步解析不得阻塞事件循环"
