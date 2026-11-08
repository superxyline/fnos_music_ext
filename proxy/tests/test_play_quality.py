"""七档播放音质（移植上游 v2.12.0 quality_policy）：迁移与逐级降档。"""
import httpx
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import proxy.app as pa  # noqa: E402
from proxy import quality_policy as qp  # noqa: E402


def test_initial_play_quality_migration(monkeypatch):
    # 旧三档迁移：high+主档 lossless → lossless；balanced → 极高；smooth → 标准
    assert qp.effective_play_quality({"FNMUSIC_QUALITY_MODE": "high"}) == "lossless"
    assert qp.effective_play_quality({"FNMUSIC_QUALITY_MODE": "smooth"}) == "standard"
    # 显式新键优先
    assert qp.effective_play_quality({"FNMUSIC_PLAY_QUALITY": "hires"}) == "hires"
    # 老键迁移后仍是合法档位
    for v in ("standard", "higher", "exhigh", "lossless", "hires", "jymaster", "sky"):
        assert v in qp.QUALITY_LEVELS


def test_initial_play_quality_rejects_garbage():
    with pytest.raises(ValueError):
        qp.normalize_quality("不是档位", default=None)


@pytest.mark.anyio
async def test_netease_sky_ladder_falls_back(monkeypatch):
    """目标沉浸声（sky）：musicbox 不支持/无权益时逐级下降不回升。"""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url.params.get("quality")))
        return httpx.Response(200, json={"ok": True, "data": {"code": 200, "url": ""}})

    monkeypatch.setitem(pa.CONF, "play_quality", "sky")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="http://127.0.0.1:8770") as client:
        assert await pa.resolve_netease_url(client, "186016") is None
    assert seen == ["sky", "jymaster", "hires", "lossless", "exhigh", "higher", "standard"]


@pytest.mark.anyio
async def test_play_quality_hot_change_clears_pins(monkeypatch):
    """播放音质热重载：洛雪/网易钉链与配置指纹一并失效。"""
    import time

    pa._LX_URL_CONFIG = ("lossless",)
    pa._NETEASE_URL_CONFIG = ("lossless",)
    pa._LX_URL_PIN["p1"] = ({"url": "http://old"}, time.monotonic() + 60)
    monkeypatch.setitem(pa.CONF, "play_quality", "hires")
    pa._sync_netease_quality()
    assert pa._LX_URL_CONFIG == ("hires",)
    assert "p1" not in pa._LX_URL_PIN
    assert pa._NETEASE_URL_CONFIG == ("hires",)
