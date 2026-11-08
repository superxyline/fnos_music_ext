"""LLM 推荐升级：双协议解析、每日限额拦截、用量账本（移植上游 #83/#84/#85）。"""
import asyncio
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_usage  # noqa: E402
import recommend  # noqa: E402


@pytest.fixture()
def usage_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_LLM_USAGE_FILE", str(tmp_path / "llm_usage.json"))
    monkeypatch.setenv("FNMUSIC_LLM_DAILY_TOKEN_LIMIT", "1000")
    monkeypatch.setenv("FNMUSIC_LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("FNMUSIC_LLM_API_KEY", "sk-test")
    llm_usage._STATE = None
    llm_usage._STATE_DAY = ""
    yield tmp_path
    llm_usage._STATE = None
    llm_usage._STATE_DAY = ""


def _chat_body(usage=True):
    body = {"choices": [{"message": {"content": "[{\"title\": \"晴天\", \"artist\": \"周杰伦\"}]"},
                         "finish_reason": "stop"}]}
    if usage:
        body["usage"] = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    return body


def _responses_body(truncated=False):
    out = {"output": [{"content": [{"type": "output_text", "text": "[{\"title\": \"晴天\", \"artist\": \"周杰伦\"}]"}]}],
           "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}}
    if truncated:
        out["output"][0]["status"] = "incomplete"
        out["output"][0]["content"] = []
        out["status"] = "incomplete"
    return out


@pytest.mark.anyio
async def test_chat_protocol_records_usage(usage_env):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        return httpx.Response(200, json=_chat_body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        recs = await recommend.call_llm(client, "推荐")
    assert recs and recs[0]["title"] == "晴天"
    day = llm_usage.used_today()
    assert day["requests"] == 1 and day["total"] == 150


@pytest.mark.anyio
async def test_responses_protocol(usage_env, monkeypatch):
    monkeypatch.setenv("FNMUSIC_LLM_PROTOCOL", "responses")

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/responses")
        payload = request.read()
        assert b"max_output_tokens" in payload and b"messages" not in payload
        return httpx.Response(200, json=_responses_body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        recs = await recommend.call_llm(client, "推荐")
    assert recs and recs[0]["title"] == "晴天"
    assert llm_usage.used_today()["total"] == 150


@pytest.mark.anyio
async def test_output_budget_exhaustion_recorded_not_silent(usage_env, monkeypatch):
    """issue #83：推理模型输出耗尽不再静默降级，记入账本最近失败。"""
    monkeypatch.setenv("FNMUSIC_LLM_PROTOCOL", "responses")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_responses_body(truncated=True))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await recommend.call_llm(client, "推荐") == []
    day = llm_usage.used_today()
    assert day["requests"] == 1
    assert (day.get("lastFailure") or {}).get("reason") == "输出预算耗尽"


@pytest.mark.anyio
async def test_daily_limit_blocks_before_request(usage_env):
    """每日 token 上限：达到后请求前拦截（不计请求数，记额度拦截失败）。"""
    llm_usage.record({"prompt": 900, "completion": 200, "total": 1100})
    assert not llm_usage.within_limit()

    async def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("超限后不应发起请求")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await recommend.call_llm(client, "推荐") == []
    day = llm_usage.used_today()
    assert day["requests"] == 1  # 拦截不加请求数
    assert (day.get("lastFailure") or {}).get("reason") == "额度拦截"


@pytest.mark.anyio
async def test_http_error_recorded(usage_env):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await recommend.call_llm(client, "推荐") == []
    day = llm_usage.used_today()
    assert day["requests"] == 1
    assert (day.get("lastFailure") or {}).get("reason") == "HTTP 500"


def test_limit_zero_means_unlimited(usage_env, monkeypatch):
    monkeypatch.setenv("FNMUSIC_LLM_DAILY_TOKEN_LIMIT", "0")
    llm_usage.record({"prompt": 5, "completion": 5, "total": 99999999})
    assert llm_usage.within_limit()
