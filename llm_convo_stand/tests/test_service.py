import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from llm_convo.db import Base, create_session_factory
from llm_convo.models import ConversationStatus
from llm_convo.schemas import AgentConfig, GenerationParameters, StartConversationRequest
from llm_convo.service import ConversationService, IdempotencyConflictError


@pytest.fixture
async def service():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    result = ConversationService(create_session_factory(engine))
    yield result
    await engine.dispose()


def request(task: str = "Discuss") -> StartConversationRequest:
    agent = AgentConfig(system_prompt="You are helpful", api_key_env=None)
    return StartConversationRequest(task_prompt=task, agent_a=agent, agent_b=agent)


@pytest.mark.asyncio
async def test_start_is_idempotent(service: ConversationService) -> None:
    first = await service.create(request(), "same-key")
    second = await service.create(request(), "same-key")

    assert first.deduplicated is False
    assert second.deduplicated is True
    assert first.conversation.id == second.conversation.id


@pytest.mark.asyncio
async def test_agent_specific_provider_and_generation_settings_are_persisted(
    service: ConversationService,
) -> None:
    agent_a = AgentConfig(
        system_prompt="A",
        model="model-a",
        base_url="http://agent-a.example/v1",
        context_size=8192,
        parameters=GenerationParameters(temperature=0.2, max_tokens=111),
    )
    agent_b = AgentConfig(
        system_prompt="B",
        model="model-b",
        base_url="http://agent-b.example/v1",
        context_size=16384,
        parameters=GenerationParameters(temperature=1.1, max_tokens=222),
    )
    created = await service.create(
        StartConversationRequest(task_prompt="Discuss", agent_a=agent_a, agent_b=agent_b),
        "different-agents",
    )

    config = created.conversation.config
    assert config["agent_a"]["model"] == "model-a"
    assert config["agent_a"]["base_url"] == "http://agent-a.example/v1"
    assert config["agent_a"]["parameters"]["max_tokens"] == 111
    assert config["agent_a"]["context_size"] == 8192
    assert config["agent_b"]["model"] == "model-b"
    assert config["agent_b"]["base_url"] == "http://agent-b.example/v1"
    assert config["agent_b"]["parameters"]["max_tokens"] == 222
    assert config["agent_b"]["context_size"] == 16384


@pytest.mark.asyncio
async def test_reused_key_with_different_body_conflicts(service: ConversationService) -> None:
    await service.create(request("one"), "same-key")
    with pytest.raises(IdempotencyConflictError):
        await service.create(request("two"), "same-key")


@pytest.mark.asyncio
async def test_stop_is_idempotent(service: ConversationService) -> None:
    created = await service.create(request(), "stop-key")
    first = await service.request_stop(created.conversation.id)
    second = await service.request_stop(created.conversation.id)

    assert first is not None
    assert second is not None
    assert second.status == ConversationStatus.stopped.value


@pytest.mark.asyncio
async def test_list_conversations_supports_history_and_status_filter(
    service: ConversationService,
) -> None:
    first = await service.create(request("first"), "list-first")
    await service.create(request("second"), "list-second")
    await service.request_stop(first.conversation.id)

    all_items, total = await service.list_conversations(status=None, limit=10, offset=0)
    stopped, stopped_total = await service.list_conversations(
        status=ConversationStatus.stopped, limit=10, offset=0
    )

    assert total == 2
    assert [item.task_prompt for item in all_items] == ["second", "first"]
    assert stopped_total == 1
    assert stopped[0].id == first.conversation.id
