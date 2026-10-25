from typing import Any

import pytest

from llm_convo.llm import LLMResult, ToolCall
from llm_convo.react import AgentBReactGraph, _aggregate


def result(
    content: str = "",
    *,
    tool_calls: list[ToolCall] | None = None,
    reasoning_details: list[dict[str, Any]] | None = None,
) -> LLMResult:
    return LLMResult(
        content=content,
        prompt_tokens=2,
        completion_tokens=3,
        total_tokens=5,
        finish_reason="tool_calls" if tool_calls else "stop",
        latency_ms=4,
        first_token_ms=1,
        streamed=False,
        attempts=1,
        tool_calls=tool_calls or [],
        reasoning_details=reasoning_details or [],
    )


@pytest.mark.asyncio
async def test_react_graph_executes_terminal_and_forces_final_answer_at_budget() -> None:
    model_requests: list[tuple[list[dict[str, Any]], list[dict[str, Any]] | None]] = []
    executions: list[tuple[ToolCall, int]] = []
    observed: list[tuple[str, int, bool]] = []

    async def complete(messages, tools):
        model_requests.append((messages, tools))
        if len(model_requests) == 1:
            return result(
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="terminal",
                        arguments={"command": "id", "timeout_seconds": 10},
                    )
                ],
                reasoning_details=[
                    {"type": "reasoning.text", "text": "private", "index": 0}
                ],
            )
        assert tools is None
        assert messages[-2]["role"] == "tool"
        assert messages[-3]["reasoning_details"] == [
            {"type": "reasoning.text", "text": "private", "index": 0}
        ]
        return result(
            "final answer",
            reasoning_details=[{"type": "reasoning.text", "text": "final private"}],
        )

    async def execute(call: ToolCall, ordinal: int) -> dict[str, Any]:
        executions.append((call, ordinal))
        return {"exit_code": 0, "stdout": "uid=1000", "stderr": "", "timed_out": False}

    async def observe(model_result: LLMResult, step: int) -> None:
        observed.append((model_result.content, step, bool(model_result.tool_calls)))

    final = await AgentBReactGraph(complete, execute, observe).run(
        [{"role": "user", "content": "inspect"}],
        max_tool_calls=1,
    )

    assert final.content == "final answer"
    assert final.metadata["react_model_calls"] == 2
    assert final.metadata["react_tool_calls"] == 1
    assert final.metadata["react_unresolved_tool_calls"] == 0
    assert final.reasoning_details == []
    assert [(item.id, ordinal) for item, ordinal in executions] == [("call-1", 1)]
    assert observed == [("", 1, True), ("final answer", 2, False)]


def test_aggregate_drops_tool_call_ignored_by_model_after_budget() -> None:
    final = _aggregate(
        [
            result(
                "Let me inspect further.",
                tool_calls=[
                    ToolCall(
                        id="call-2",
                        name="terminal",
                        arguments={"command": "lsblk -f"},
                    )
                ],
            )
        ],
        tool_calls_used=1,
    )

    assert final.finish_reason == "stop"
    assert final.tool_calls == []
    assert final.metadata["react_tool_calls"] == 1
    assert final.metadata["react_unresolved_tool_calls"] == 1
    assert final.content == (
        "Let me inspect further.\n\n"
        "[Terminal-call budget exhausted; the final requested tool call was not executed.]"
    )
