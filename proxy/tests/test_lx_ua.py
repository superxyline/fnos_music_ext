"""issue #54：洛雪直链取流的 UA 一致性与失败冷却有界化。

背景（实测酷我）：lxmusic 探活用 Chrome PC UA 验链通过，但解析结果 headers 为空
时代理取流会回退 httpx 默认 UA，UA 敏感 CDN 直接 403——表象是「探活成功、播放
失败」，重试路径又反复重新解析，白烧音源额度。这里锁死三件事：

1. 代理侧兜底 UA 与 lxmusic 探活 UA 逐字符一致（跨服务门禁，任一侧单改即红）；
2. 解析结果白名单透传 Referer/UA，缺失时兜底；
3. 同曲直链钉住 + 失败 60s 冷却，冷却窗内快速失败不再出网。
"""
import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

import proxy.app as pa

LX_DIR = Path(__file__).resolve().parents[2] / "lxmusic-service"


def _lxmusic_module():
    cached = sys.modules.get("lxmusic_service_app")
    if cached is not None:
        return cached
    if str(LX_DIR) not in sys.path:
        sys.path.insert(0, str(LX_DIR))
    spec = importlib.util.spec_from_file_location("lxmusic_service_app", LX_DIR / "app.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lxmusic_service_app"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch):
    monkeypatch.setattr(pa, "_LX_URL_PIN", {})
    monkeypatch.setattr(pa, "_LX_FAIL_COOLDOWN", {})


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="http://lx.local",
                             transport=httpx.MockTransport(handler))


def test_cross_service_ua_matches_lxmusic_probe():
    """代理兜底 UA 必须与 lxmusic 探活 UA 完全一致，单边改动即失败。"""
    lx = _lxmusic_module()
    assert pa._LX_MEDIA_UA == lx.UA_PC


def test_media_headers_pass_through_and_fallback():
    passthrough = pa._lx_media_headers(
        {"headers": {"User-Agent": "custom-ua", "Referer": "https://www.kuwo.cn/",
                     "Cookie": "should-be-dropped"}}
    )
    assert passthrough["User-Agent"] == "custom-ua"
    assert passthrough["Referer"] == "https://www.kuwo.cn/"
    assert "Cookie" not in passthrough

    assert pa._lx_media_headers(None)["User-Agent"] == pa._LX_MEDIA_UA
    assert pa._lx_media_headers({"headers": {}})["User-Agent"] == pa._LX_MEDIA_UA


@pytest.mark.anyio
async def test_resolve_lx_url_pins_result_and_skips_second_call():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        return httpx.Response(200, json={
            "ok": True,
            "data": {"url": "https://media.test/a.flac", "ext": "flac",
                     "headers": {"User-Agent": pa._LX_MEDIA_UA}},
        })

    client = _client(handler)
    first = await pa.resolve_lx_url(client, "lx:kw:123")
    second = await pa.resolve_lx_url(client, "lx:kw:123")
    assert first and second
    assert first["url"] == second["url"]
    assert len(calls) == 1, "钉住生效后同曲不应重复解析"
    assert pa._LX_URL_PIN.get("lx:kw:123")


@pytest.mark.anyio
async def test_fresh_bypasses_pin_but_respects_cooldown():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={
            "ok": True,
            "data": {"url": "https://media.test/b.flac", "ext": "flac"},
        })

    client = _client(handler)
    await pa.resolve_lx_url(client, "lx:kw:9")
    assert len(calls) == 1
    # fresh 旁路钉链 → 真实重新解析
    await pa.resolve_lx_url(client, "lx:kw:9", fresh=True)
    assert len(calls) == 2

    # 取流失败后标记冷却：fresh 快速失败，不再出网
    pa._lx_url_pin_drop("lx:kw:9")
    pa._lx_failure_mark("lx:kw:9")
    assert pa._lx_failure_active("lx:kw:9")
    assert await pa.resolve_lx_url(client, "lx:kw:9", fresh=True) is None
    assert len(calls) == 2, "冷却窗内 fresh 不得再出网"


def test_failure_cooldown_expires(monkeypatch):
    pa._lx_failure_mark("lx:kw:1")
    assert pa._lx_failure_active("lx:kw:1")
    real = pa.time.monotonic
    monkeypatch.setattr(pa.time, "monotonic", lambda: real() + pa._LX_FAIL_COOLDOWN_S + 1)
    assert not pa._lx_failure_active("lx:kw:1")
    assert "lx:kw:1" not in pa._LX_FAIL_COOLDOWN


@pytest.mark.anyio
async def test_lxmusic_result_headers_carry_probe_ua(monkeypatch):
    """lxmusic 解析出口必须带探活同款 UA —— 空 headers 是 403 的根源。"""
    lx = _lxmusic_module()

    async def fake_probe(client, url, headers=None, **kwargs):
        # 探活用的 UA 必须已经是 UA_PC（而不是 httpx 默认头）
        assert (headers or {}).get("User-Agent") == lx.UA_PC
        return True, url, "audio/flac", 1234

    monkeypatch.setattr(lx, "probe_url", fake_probe)
    result = await lx._verify_result(_client(lambda r: httpx.Response(200)), {
        "url": "https://media.test/c.flac",
        "headers": {"Referer": "https://www.kuwo.cn/"},
    })
    assert result["headers"]["User-Agent"] == lx.UA_PC
    assert result["headers"]["Referer"] == "https://www.kuwo.cn/"
