import httpx2
import openai
import pytest

from my_agent_llm.config import Config
from my_agent_llm.models import Message
from my_agent_llm.providers.openai import OpenAIProvider
from my_agent_llm.request_stats import ModelRequestStats, record_model_usage, track_model_request


@pytest.mark.anyio
async def test_openai_sdk_retry_is_counted_inside_one_logical_call(monkeypatch):
    attempts = []

    async def respond(request):
        attempts.append(request.headers.get("x-stainless-retry-count"))
        if len(attempts) == 1:
            return httpx2.Response(429, headers={"retry-after-ms": "1"}, json={"error": {"message": "busy"}})
        return httpx2.Response(200, json={
            "id": "offline-response",
            "created": 0,
            "object": "chat.completion",
            "model": "offline",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "summary"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        })

    default_client = openai.DefaultAsyncHttpxClient
    monkeypatch.setattr(openai, "DefaultAsyncHttpxClient", lambda **kwargs: default_client(
        transport=httpx2.MockTransport(respond), **kwargs,
    ))
    provider = OpenAIProvider(Config(
        model="offline", base_url="https://offline.invalid/v1", api_key="offline-test", max_retries=1,
    ))
    stats = ModelRequestStats()
    try:
        with stats.collect(), track_model_request("summary"):
            response = await provider.achat([Message(role="user", content="Summarize")], model="offline")
            record_model_usage(response.usage)
    finally:
        await provider.async_client.close()
        provider.client.close()

    assert attempts == ["0", "1"]
    assert response.content == "summary"
    summary = stats.snapshot()["summary"]
    assert summary["calls"] == 1
    assert summary["http_attempts_observed"] == 2
    assert summary["sdk_retry_http_attempts_observed"] == 1
    assert summary["calls_without_http_observations"] == summary["calls_without_usage"] == 0
    assert summary["reported_usage"]["total_tokens"] == 6
    assert stats.snapshot()["retry"]["calls"] == 0
