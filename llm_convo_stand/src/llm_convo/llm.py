import asyncio
import json
import logging
import os
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from llm_convo.logging import log_event
from llm_convo.schemas import AgentConfig, StreamMode

logger = logging.getLogger(__name__)

AsyncCallback = Callable[[], Awaitable[None]]
TokenCallback = Callable[[str], Awaitable[None]]


class LLMError(RuntimeError):
    pass


class RetryableLLMError(LLMError):
    pass


class StreamUnsupportedError(LLMError):
    pass


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class LLMResult:
    content: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    finish_reason: str | None
    latency_ms: int
    first_token_ms: int | None
    streamed: bool
    attempts: int
    metadata: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[ToolCall] = field(default_factory=list)


def _api_key(config: AgentConfig) -> str | None:
    if config.api_key_env:
        return os.getenv(config.api_key_env) or None
    return None


def _headers(config: AgentConfig) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    key = _api_key(config)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = response.text[:2_000]
    return f"LLM endpoint returned HTTP {response.status_code}: {body}"


def _raise_for_status(response: httpx.Response, *, streaming: bool) -> None:
    if response.is_success:
        return
    message = _error_message(response)
    if response.status_code in {408, 409, 429} or response.status_code >= 500:
        raise RetryableLLMError(message)
    if streaming and response.status_code in {400, 404, 405, 415, 422, 501}:
        raise StreamUnsupportedError(message)
    raise LLMError(message)


def _parse_tool_calls(value: Any) -> list[ToolCall]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LLMError("LLM endpoint returned malformed tool calls")
    result: list[ToolCall] = []
    for item in value:
        try:
            function = item["function"]
            call_id = item["id"]
            name = function["name"]
            raw_arguments = function.get("arguments") or "{}"
            arguments = (
                json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            )
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise LLMError("LLM endpoint returned malformed tool call arguments") from exc
        if (
            not isinstance(call_id, str)
            or not call_id
            or not isinstance(name, str)
            or not name
            or not isinstance(arguments, dict)
        ):
            raise LLMError("LLM endpoint returned malformed tool call")
        result.append(ToolCall(id=call_id, name=name, arguments=arguments))
    return result


class OpenAICompatibleClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._stream_capability: dict[tuple[str, str], bool] = {}
        self._stream_usage_capability: dict[tuple[str, str], bool] = {}

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def complete(
        self,
        *,
        config: AgentConfig,
        messages: list[dict[str, Any]],
        parameter_overrides: dict[str, Any] | None = None,
        on_stream_start: AsyncCallback | None = None,
        on_token: TokenCallback | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResult:
        parameters = config.parameters.to_openai_payload()
        if parameter_overrides:
            parameters.update(parameter_overrides)
        base_payload: dict[str, Any] = {
            "model": config.model,
            "messages": messages,
            **parameters,
            **config.extra_body,
        }
        if tools:
            base_payload["tools"] = tools
            base_payload["tool_choice"] = tool_choice or "auto"

        capability_key = (str(config.base_url), config.model)
        use_stream = config.stream is not StreamMode.never and (
            config.stream is StreamMode.always or self._stream_capability.get(capability_key, True)
        )
        include_usage = self._stream_usage_capability.get(capability_key, True)
        last_error: Exception | None = None
        attempts = 0
        for attempt in range(config.max_retries + 1):
            try:
                if use_stream:
                    attempts += 1
                    try:
                        result = await self._stream(
                            config,
                            base_payload,
                            include_usage=include_usage,
                            on_stream_start=on_stream_start,
                            on_token=on_token,
                        )
                    except StreamUnsupportedError as first_stream_error:
                        if include_usage:
                            log_event(
                                logger,
                                logging.INFO,
                                "llm_stream_options_unsupported",
                                model=config.model,
                                base_url=str(config.base_url),
                                error=str(first_stream_error),
                            )
                            attempts += 1
                            try:
                                # Some compatible servers support SSE but reject the optional
                                # stream_options field used to request token usage.
                                result = await self._stream(
                                    config,
                                    base_payload,
                                    include_usage=False,
                                    on_stream_start=on_stream_start,
                                    on_token=on_token,
                                )
                                self._stream_usage_capability[capability_key] = False
                                self._stream_capability[capability_key] = True
                            except StreamUnsupportedError as stream_error:
                                attempts += 1
                                result = await self._fallback_to_non_stream(
                                    config=config,
                                    base_payload=base_payload,
                                    error=stream_error,
                                    attempts_so_far=attempts,
                                )
                                self._stream_capability[capability_key] = False
                        else:
                            attempts += 1
                            result = await self._fallback_to_non_stream(
                                config=config,
                                base_payload=base_payload,
                                error=first_stream_error,
                                attempts_so_far=attempts,
                            )
                            self._stream_capability[capability_key] = False
                    else:
                        self._stream_capability[capability_key] = True
                else:
                    attempts += 1
                    result = await self._non_stream(config, base_payload)
                result.attempts = attempts
                return result
            except (RetryableLLMError, httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt >= config.max_retries:
                    break
                delay = min(8.0, 0.5 * (2**attempt)) + random.uniform(0, 0.25)
                log_event(
                    logger,
                    logging.WARNING,
                    "llm_request_retry",
                    model=config.model,
                    base_url=str(config.base_url),
                    request_attempts=attempts,
                    retry_in_seconds=round(delay, 3),
                    error=str(exc),
                )
                await asyncio.sleep(delay)
            except httpx.HTTPError as exc:
                raise LLMError(f"LLM HTTP client error: {exc}") from exc
        raise LLMError(
            f"LLM request failed after {attempts} attempt(s): {last_error}"
        ) from last_error

    async def _fallback_to_non_stream(
        self,
        *,
        config: AgentConfig,
        base_payload: dict[str, Any],
        error: StreamUnsupportedError,
        attempts_so_far: int,
    ) -> LLMResult:
        if config.stream is StreamMode.always:
            raise error
        log_event(
            logger,
            logging.INFO,
            "llm_streaming_unsupported_fallback",
            model=config.model,
            base_url=str(config.base_url),
            request_attempts=attempts_so_far,
            error=str(error),
        )
        return await self._non_stream(config, base_payload)

    async def _non_stream(self, config: AgentConfig, base_payload: dict[str, Any]) -> LLMResult:
        started = time.perf_counter()
        response = await self._client.post(
            f"{str(config.base_url).rstrip('/')}/chat/completions",
            headers=_headers(config),
            json={**base_payload, "stream": False},
            timeout=config.request_timeout_seconds,
        )
        _raise_for_status(response, streaming=False)
        try:
            data = response.json()
            choice = data["choices"][0]
            message = choice["message"]
            content = message.get("content") or ""
            tool_calls = _parse_tool_calls(message.get("tool_calls"))
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("Malformed non-streaming response from LLM endpoint") from exc
        if not isinstance(content, str):
            raise LLMError("LLM endpoint returned malformed message content")
        if not content.strip() and not tool_calls:
            raise LLMError("LLM endpoint returned neither text nor tool calls")
        usage = data.get("usage") or {}
        elapsed = int((time.perf_counter() - started) * 1000)
        return LLMResult(
            content=content or "",
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            finish_reason=choice.get("finish_reason"),
            latency_ms=elapsed,
            first_token_ms=elapsed,
            streamed=False,
            attempts=1,
            metadata={"response_id": data.get("id"), "created": data.get("created")},
            tool_calls=tool_calls,
        )

    async def _stream(
        self,
        config: AgentConfig,
        base_payload: dict[str, Any],
        *,
        include_usage: bool = True,
        on_stream_start: AsyncCallback | None = None,
        on_token: TokenCallback | None = None,
    ) -> LLMResult:
        started = time.perf_counter()
        first_token_ms: int | None = None
        chunks: list[str] = []
        tool_fragments: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        finish_reason: str | None = None
        response_id: str | None = None
        payload = {**base_payload, "stream": True}
        if include_usage:
            payload["stream_options"] = {"include_usage": True}

        async with self._client.stream(
            "POST",
            f"{str(config.base_url).rstrip('/')}/chat/completions",
            headers={**_headers(config), "Accept": "text/event-stream"},
            json=payload,
            timeout=config.request_timeout_seconds,
        ) as response:
            if not response.is_success:
                # HTTPX does not buffer bodies opened through ``stream()``. Read an
                # error response before _raise_for_status inspects its JSON/text.
                await response.aread()
            _raise_for_status(response, streaming=True)
            if on_stream_start:
                try:
                    await on_stream_start()
                except Exception as exc:  # pragma: no cover - UI delivery is best effort
                    log_event(
                        logger,
                        logging.WARNING,
                        "stream_start_callback_failed",
                        error=str(exc),
                    )
            async for line in response.aiter_lines():
                line = line.strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    break
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise LLMError("Malformed SSE JSON from LLM endpoint") from exc
                response_id = response_id or event.get("id")
                if event.get("usage"):
                    usage = event["usage"]
                choices = event.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                for fragment in delta.get("tool_calls") or []:
                    if not isinstance(fragment, dict):
                        raise LLMError("Malformed streaming tool call")
                    try:
                        index = int(fragment.get("index", 0))
                    except (TypeError, ValueError) as exc:
                        raise LLMError("Malformed streaming tool call index") from exc
                    current = tool_fragments.setdefault(
                        index,
                        {"id": "", "function": {"name": "", "arguments": ""}},
                    )
                    if fragment.get("id"):
                        current["id"] = fragment["id"]
                    function = fragment.get("function") or {}
                    if function.get("name"):
                        current["function"]["name"] += function["name"]
                    if function.get("arguments"):
                        current["function"]["arguments"] += function["arguments"]
                    if first_token_ms is None:
                        first_token_ms = int((time.perf_counter() - started) * 1000)
                content = delta.get("content")
                if content:
                    if first_token_ms is None:
                        first_token_ms = int((time.perf_counter() - started) * 1000)
                    chunks.append(content)
                    if on_token:
                        try:
                            await on_token(content)
                        except Exception as exc:  # pragma: no cover - UI delivery is best effort
                            log_event(
                                logger,
                                logging.WARNING,
                                "stream_token_callback_failed",
                                error=str(exc),
                            )
                finish_reason = choice.get("finish_reason") or finish_reason

        elapsed = int((time.perf_counter() - started) * 1000)
        raw_tool_calls = [tool_fragments[index] for index in sorted(tool_fragments)]
        tool_calls = _parse_tool_calls(raw_tool_calls)
        if not chunks and not tool_calls:
            raise LLMError("Streaming response ended without text or tool calls")
        return LLMResult(
            content="".join(chunks),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            finish_reason=finish_reason,
            latency_ms=elapsed,
            first_token_ms=first_token_ms,
            streamed=True,
            attempts=1,
            metadata={"response_id": response_id},
            tool_calls=tool_calls,
        )
