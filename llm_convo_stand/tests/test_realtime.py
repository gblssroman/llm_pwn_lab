import uuid

import pytest

from llm_convo.realtime import RealtimeBroker


@pytest.mark.asyncio
async def test_broker_fans_out_transient_token_events() -> None:
    broker = RealtimeBroker()
    conversation_id = uuid.uuid4()

    async with (
        broker.subscribe(conversation_id) as first,
        broker.subscribe(conversation_id) as second,
    ):
        await broker.publish(
            conversation_id,
            "turn_delta",
            {"sequence": 1, "speaker": "agent_a", "content": "hello"},
        )

        first_event = await first.get()
        second_event = await second.get()

    assert first_event.event == "turn_delta"
    assert first_event.data["content"] == "hello"
    assert second_event.id == first_event.id
