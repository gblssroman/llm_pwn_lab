import json

import httpx
import pytest

from llm_convo.llm import LLMError, OpenAICompatibleClient
from llm_convo.schemas import AgentConfig, StreamMode


class UnreadErrorStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"error":"stream unsupported"}'


def config(stream: StreamMode = StreamMode.auto, max_retries: int = 0) -> AgentConfig:
    return AgentConfig(
        system_prompt="system",
        base_url="http://llm.test/v1",
        api_key_env=None,
        stream=stream,
        max_retries=max_retries,
    )


@pytest.mark.asyncio
async def test_streaming_completion_collects_sse_and_usage() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is True
        content = "".join(
            [
                'data: {"id":"r1","choices":[{"delta":{"content":"hel"},'
                '"finish_reason":null}]}\n\n',
                'data: {"id":"r1","choices":[{"delta":{"content":"lo"},'
                '"finish_reason":"stop"}]}\n\n',
                'data: {"id":"r1","choices":[],"usage":{"prompt_tokens":8,'
                '"completion_tokens":2,"total_tokens":10}}\n\n',
                "data: [DONE]\n\n",
            ]
        )
        return httpx.Response(200, text=content, headers={"content-type": "text/event-stream"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAICompatibleClient(http)
    starts = 0
    deltas: list[str] = []

    async def on_start() -> None:
        nonlocal starts
        starts += 1

    async def on_token(content: str) -> None:
        deltas.append(content)

    result = await client.complete(
        config=config(),
        messages=[{"role": "user", "content": "hi"}],
        on_stream_start=on_start,
        on_token=on_token,
    )

    assert result.content == "hello"
    assert result.streamed is True
    assert result.prompt_tokens == 8
    assert result.completion_tokens == 2
    assert result.finish_reason == "stop"
    assert starts == 1
    assert deltas == ["hel", "lo"]
    await http.aclose()


@pytest.mark.asyncio
async def test_auto_mode_falls_back_when_stream_is_unsupported() -> None:
    calls: list[bool] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["stream"])
        if body["stream"]:
            return httpx.Response(400, json={"error": "stream unsupported"})
        return httpx.Response(
            200,
            json={
                "id": "r2",
                "choices": [{"message": {"content": "fallback"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAICompatibleClient(http)
    result = await client.complete(config=config(), messages=[{"role": "user", "content": "hi"}])

    assert calls == [True, True, False]
    assert result.content == "fallback"
    assert result.streamed is False
    assert result.attempts == 3

    # Capability is cached, so later turns skip known-bad SSE requests.
    second = await client.complete(config=config(), messages=[{"role": "user", "content": "again"}])
    assert calls == [True, True, False, False]
    assert second.attempts == 1
    await http.aclose()


@pytest.mark.asyncio
async def test_streaming_retries_without_optional_usage_extension() -> None:
    payloads: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        payloads.append(body)
        if "stream_options" in body:
            return httpx.Response(400, json={"error": "unknown stream_options"})
        content = (
            'data: {"choices":[{"delta":{"content":"ok"},'
            '"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
        )
        return httpx.Response(200, text=content, headers={"content-type": "text/event-stream"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAICompatibleClient(http)
    first = await client.complete(config=config(), messages=[{"role": "user", "content": "hi"}])
    second = await client.complete(config=config(), messages=[{"role": "user", "content": "again"}])

    assert first.streamed is True
    assert first.attempts == 2
    assert second.streamed is True
    assert second.attempts == 1
    assert ["stream_options" in payload for payload in payloads] == [True, False, False]
    await http.aclose()


@pytest.mark.asyncio
async def test_always_stream_does_not_fallback() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "stream unsupported"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAICompatibleClient(http)
    with pytest.raises(LLMError):
        await client.complete(
            config=config(StreamMode.always), messages=[{"role": "user", "content": "hi"}]
        )
    await http.aclose()


@pytest.mark.asyncio
async def test_streaming_error_body_is_read_before_it_is_inspected() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            headers={"content-type": "application/json"},
            stream=UnreadErrorStream(),
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAICompatibleClient(http)
    with pytest.raises(LLMError, match="stream unsupported"):
        await client.complete(
            config=config(StreamMode.always), messages=[{"role": "user", "content": "hi"}]
        )
    await http.aclose()


@pytest.mark.asyncio
async def test_non_streaming_response() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "r3",
                "created": 1,
                "choices": [{"message": {"content": "answer"}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAICompatibleClient(http)
    result = await client.complete(
        config=config(StreamMode.never), messages=[{"role": "user", "content": "hi"}]
    )

    assert result.content == "answer"
    assert result.total_tokens == 12
    assert result.finish_reason == "length"
    await http.aclose()
