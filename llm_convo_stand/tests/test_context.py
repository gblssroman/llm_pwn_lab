from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from llm_convo.context import (
    build_compression_messages,
    build_working_context,
    estimate_tokens,
    needs_compression,
)
from llm_convo.schemas import AgentConfig, GenerationParameters, StartConversationRequest


@dataclass
class Item:
    sequence: int
    speaker: str
    content: str


def test_working_context_maps_history_from_agent_b_perspective() -> None:
    result = build_working_context(
        system_prompt="You are the critic.",
        task_prompt="Reach a decision.",
        speaker="agent_b",
        summary="A proposed option X.",
        messages=[
            Item(4, "agent_a", "Use X."),
            Item(5, "agent_b", "Why?"),
            Item(6, "agent_a", "Because it is simpler."),
        ],
    )

    assert result.messages == [
        {"role": "system", "content": "You are the critic."},
        {
            "role": "user",
            "content": (
                "Shared task from the user:\nReach a decision.\n\n"
                "Shared summary of the earlier conversation:\nA proposed option X."
            ),
        },
        {"role": "user", "content": "Use X."},
        {"role": "assistant", "content": "Why?"},
        {"role": "user", "content": "Because it is simpler."},
    ]
    assert result.estimated_tokens > 0


def test_working_context_maps_history_from_agent_a_perspective() -> None:
    result = build_working_context(
        system_prompt="Private instructions for A.",
        task_prompt="Debate the proposal.",
        speaker="agent_a",
        summary=None,
        messages=[
            Item(1, "agent_a", "Initial proposal."),
            Item(2, "agent_b", "Counterargument."),
        ],
    )

    assert result.messages == [
        {"role": "system", "content": "Private instructions for A."},
        {"role": "user", "content": "Shared task from the user:\nDebate the proposal."},
        {"role": "assistant", "content": "Initial proposal."},
        {"role": "user", "content": "Counterargument."},
    ]


def test_working_context_has_only_the_current_agents_system_prompt() -> None:
    result = build_working_context(
        system_prompt="Only agent B's private prompt.",
        task_prompt="Shared task.",
        speaker="agent_b",
        summary="Shared facts only.",
        messages=[Item(1, "agent_a", "Visible reply from A.")],
    )

    assert [item for item in result.messages if item["role"] == "system"] == [
        {"role": "system", "content": "Only agent B's private prompt."}
    ]
    assert result.messages[-1] == {"role": "user", "content": "Visible reply from A."}


def test_context_threshold_reserves_output_tokens() -> None:
    assert needs_compression(
        estimated_prompt_tokens=800,
        max_output_tokens=100,
        context_size=1000,
        threshold=0.9,
    )
    assert not needs_compression(
        estimated_prompt_tokens=700,
        max_output_tokens=100,
        context_size=1000,
        threshold=0.9,
    )


def test_compression_prompt_includes_existing_summary_and_new_segment() -> None:
    payload = build_compression_messages(
        previous_summary="Prior facts",
        messages=[Item(3, "agent_a", "New fact")],
        target_tokens=256,
    )

    assert "Prior facts" in payload[1]["content"]
    assert "New fact" in payload[1]["content"]
    assert "256" in payload[1]["content"]


def test_token_estimate_handles_cyrillic_conservatively() -> None:
    assert estimate_tokens("Привет, мир") >= 5


def test_start_request_rejects_an_impossible_initial_context() -> None:
    agent = AgentConfig(
        system_prompt="system",
        parameters=GenerationParameters(max_tokens=900),
    )
    with pytest.raises(ValidationError, match="does not fit"):
        StartConversationRequest(
            task_prompt="task",
            agent_a=agent,
            agent_b=agent,
            context_size=1024,
            compression_threshold=0.9,
        )


def test_start_request_uses_each_agents_context_limit() -> None:
    constrained = AgentConfig(
        system_prompt="system",
        context_size=1024,
        compression_threshold=0.9,
        parameters=GenerationParameters(max_tokens=900),
    )
    unconstrained = AgentConfig(system_prompt="system")

    with pytest.raises(ValidationError, match="agent_a"):
        StartConversationRequest(
            task_prompt="task",
            agent_a=constrained,
            agent_b=unconstrained,
            context_size=32768,
        )


def test_extra_body_cannot_override_core_generation_fields() -> None:
    with pytest.raises(ValidationError, match="cannot override"):
        AgentConfig(system_prompt="system", extra_body={"max_tokens": 1})
