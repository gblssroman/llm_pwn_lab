import asyncio
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from llm_convo.context import INITIAL_AGENT_A_MESSAGE
from llm_convo.db import Base, create_session_factory
from llm_convo.llm import LLMResult
from llm_convo.models import ContextSnapshot, Conversation, ConversationStatus, Message
from llm_convo.runner import ConversationRunner
from llm_convo.schemas import AgentConfig, GenerationParameters, StartConversationRequest
from llm_convo.service import ConversationService
from llm_convo.tracing import Tracing


class FakeLLM:
    def __init__(self, *, delay: float = 0.005, large_turns: bool = False) -> None:
        self.delay = delay
        self.large_turns = large_turns
        self.calls = 0
        self.requests: list[list[dict[str, str]]] = []

    async def complete(
        self,
        *,
        config: AgentConfig,
        messages: list[dict[str, str]],
        parameter_overrides: dict[str, Any] | None = None,
        on_stream_start: Any | None = None,
        on_token: Any | None = None,
    ) -> LLMResult:
        await asyncio.sleep(self.delay)
        self.calls += 1
        self.requests.append([item.copy() for item in messages])
        is_summary = messages[0]["content"].startswith("You compress")
        content = (
            "compact summary"
            if is_summary
            else f"turn {self.calls} " + ("x" * 300 if self.large_turns else "ok")
        )
        if on_stream_start:
            await on_stream_start()
        if on_token:
            await on_token(content)
        return LLMResult(
            content=content,
            prompt_tokens=10,
            completion_tokens=10,
            total_tokens=20,
            finish_reason="stop",
            latency_ms=5,
            first_token_ms=1,
            streamed=True,
            attempts=1,
        )


async def wait_until(predicate, deadline_seconds: float = 2.0) -> None:
    async with asyncio.timeout(deadline_seconds):
        while not await predicate():  # noqa: ASYNC110 - predicate requires DB polling
            await asyncio.sleep(0.01)


@pytest.fixture
async def database(tmp_path):
    database_path = tmp_path / "runner.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = create_session_factory(engine)
    yield sessions
    await engine.dispose()


def start_request(*, timeout_seconds: int = 10, context_size: int = 32768):
    agent = AgentConfig(
        system_prompt="Debate carefully",
        api_key_env=None,
        parameters=GenerationParameters(max_tokens=50),
    )
    return StartConversationRequest(
        agent_a=agent,
        agent_b=agent,
        timeout_seconds=timeout_seconds,
        context_size=context_size,
        retain_recent_messages=2,
    )


@pytest.mark.asyncio
async def test_runner_alternates_and_preserves_full_transcript(database) -> None:
    service = ConversationService(database)
    created = await service.create(start_request(), "runner-alternates")
    runner = ConversationRunner(database, FakeLLM(), Tracing(), poll_seconds=1, heartbeat_seconds=5)
    runner.enqueue(created.conversation.id)

    async def has_four_turns() -> bool:
        conversation = await service.get(created.conversation.id)
        return bool(conversation and conversation.turn_count >= 4)

    await wait_until(has_four_turns)
    await service.request_stop(created.conversation.id)
    runner.cancel(created.conversation.id)

    async def is_stopped() -> bool:
        conversation = await service.get(created.conversation.id)
        return bool(conversation and conversation.status == ConversationStatus.stopped.value)

    await wait_until(is_stopped)
    messages = await service.messages(created.conversation.id, 0, 100)
    assert messages is not None
    assert [message.speaker for message in messages[:4]] == [
        "agent_a",
        "agent_b",
        "agent_a",
        "agent_b",
    ]
    assert [message.sequence for message in messages] == list(range(1, len(messages) + 1))
    await runner.shutdown(0.1)


@pytest.mark.asyncio
async def test_runner_sends_role_aware_history_and_keeps_system_prompts_private(
    database,
) -> None:
    service = ConversationService(database)
    request = StartConversationRequest(
        agent_a=AgentConfig(
            system_prompt="Private system A",
            api_key_env=None,
            parameters=GenerationParameters(max_tokens=50),
        ),
        agent_b=AgentConfig(
            system_prompt="Private system B",
            api_key_env=None,
            parameters=GenerationParameters(max_tokens=50),
        ),
        timeout_seconds=10,
    )
    created = await service.create(request, "runner-role-aware-history")
    llm = FakeLLM()
    runner = ConversationRunner(database, llm, Tracing(), poll_seconds=1, heartbeat_seconds=5)
    runner.enqueue(created.conversation.id)

    async def has_four_turns() -> bool:
        conversation = await service.get(created.conversation.id)
        return bool(conversation and conversation.turn_count >= 4)

    await wait_until(has_four_turns)
    await service.request_stop(created.conversation.id)
    runner.cancel(created.conversation.id)
    await runner.shutdown(0.1)

    first_a, first_b, second_a, second_b = llm.requests[:4]
    assert first_a == [
        {"role": "system", "content": "Private system A"},
        {"role": "user", "content": INITIAL_AGENT_A_MESSAGE},
    ]
    assert first_b == [
        {"role": "system", "content": "Private system B"},
        {"role": "user", "content": "turn 1 ok"},
    ]
    assert [item["role"] for item in second_a] == [
        "system",
        "assistant",
        "user",
    ]
    assert [item["role"] for item in second_b] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert all("Private system B" not in item["content"] for item in second_a)
    assert all("Private system A" not in item["content"] for item in second_b)


@pytest.mark.asyncio
async def test_runner_enforces_hard_timeout_during_model_call(database) -> None:
    service = ConversationService(database)
    created = await service.create(start_request(timeout_seconds=1), "runner-hard-timeout")
    runner = ConversationRunner(
        database, FakeLLM(delay=5), Tracing(), poll_seconds=1, heartbeat_seconds=5
    )
    runner.enqueue(created.conversation.id)

    async def is_timed_out() -> bool:
        conversation = await service.get(created.conversation.id)
        return bool(conversation and conversation.status == ConversationStatus.timed_out.value)

    await wait_until(is_timed_out, deadline_seconds=2)
    await runner.shutdown(0.1)


@pytest.mark.asyncio
async def test_compression_keeps_full_messages_and_creates_snapshot(database) -> None:
    service = ConversationService(database)
    created = await service.create(start_request(context_size=1024), "runner-compression")
    runner = ConversationRunner(
        database,
        FakeLLM(large_turns=True),
        Tracing(),
        poll_seconds=1,
        heartbeat_seconds=5,
    )
    runner.enqueue(created.conversation.id)

    async def has_snapshot() -> bool:
        async with database() as session:
            count = await session.scalar(
                select(func.count(ContextSnapshot.id)).where(
                    ContextSnapshot.conversation_id == created.conversation.id
                )
            )
            return bool(count)

    await wait_until(has_snapshot)
    await service.request_stop(created.conversation.id)
    runner.cancel(created.conversation.id)
    await asyncio.sleep(0.02)

    async with database() as session:
        conversation = await session.get(Conversation, created.conversation.id)
        message_count = await session.scalar(
            select(func.count(Message.id)).where(Message.conversation_id == created.conversation.id)
        )
        snapshot_count = await session.scalar(
            select(func.count(ContextSnapshot.id)).where(
                ContextSnapshot.conversation_id == created.conversation.id
            )
        )
    assert conversation is not None
    assert conversation.summary_through_sequence > 0
    assert message_count >= conversation.summary_through_sequence
    assert snapshot_count >= 1
    await runner.shutdown(0.1)
