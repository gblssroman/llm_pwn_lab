import json
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from llm_convo.llm import LLMResult, ToolCall

TERMINAL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "terminal",
            "description": (
                "Execute one shell command inside the disposable scenario sandbox. "
                "The command cannot access the evaluator channel or control the host API."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute inside the sandbox.",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 7200,
                        "default": 300,
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    }
]

Completion = Callable[[list[dict[str, Any]], list[dict[str, Any]] | None], Awaitable[LLMResult]]
TerminalExecutor = Callable[[ToolCall, int], Awaitable[dict[str, Any]]]
ModelObserver = Callable[[LLMResult, int], Awaitable[None]]


class TerminalArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1, max_length=65_536)
    timeout_seconds: int = Field(default=300, ge=1, le=7200)


class ReactState(TypedDict, total=False):
    messages: list[dict[str, Any]]
    model_results: list[LLMResult]
    last_result: LLMResult
    tool_calls_used: int
    max_tool_calls: int
    next_ordinal: int


def _assistant_message(result: LLMResult) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": result.content or None}
    if result.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(
                        call.arguments, ensure_ascii=False, separators=(",", ":")
                    ),
                },
            }
            for call in result.tool_calls
        ]
    return message


def _bounded_observation(result: dict[str, Any]) -> str:
    bounded = dict(result)
    for key in ("stdout", "stderr"):
        value = bounded.get(key)
        if isinstance(value, str) and len(value) > 32_000:
            bounded[key] = value[:16_000] + "\n...[truncated]...\n" + value[-16_000:]
    return json.dumps(bounded, ensure_ascii=False, separators=(",", ":"), default=str)


def _aggregate(results: list[LLMResult], tool_calls_used: int) -> LLMResult:
    if not results:
        raise RuntimeError("Agent B graph completed without a model response")
    final = results[-1]
    unresolved_tool_calls = len(final.tool_calls)
    content = final.content
    if unresolved_tool_calls:
        notice = (
            "[Terminal-call budget exhausted; the final requested tool call was "
            "not executed.]"
        )
        content = f"{content.rstrip()}\n\n{notice}" if content.strip() else notice
    return LLMResult(
        content=content,
        prompt_tokens=(
            sum(item.prompt_tokens or 0 for item in results)
            if any(item.prompt_tokens is not None for item in results)
            else None
        ),
        completion_tokens=(
            sum(item.completion_tokens or 0 for item in results)
            if any(item.completion_tokens is not None for item in results)
            else None
        ),
        total_tokens=(
            sum(item.total_tokens or 0 for item in results)
            if any(item.total_tokens is not None for item in results)
            else None
        ),
        finish_reason="stop" if unresolved_tool_calls else final.finish_reason,
        latency_ms=sum(item.latency_ms for item in results),
        first_token_ms=next(
            (item.first_token_ms for item in results if item.first_token_ms is not None),
            None,
        ),
        streamed=any(item.streamed for item in results),
        attempts=sum(item.attempts for item in results),
        metadata={
            **final.metadata,
            "react_model_calls": len(results),
            "react_tool_calls": tool_calls_used,
            "react_unresolved_tool_calls": unresolved_tool_calls,
        },
    )


class AgentBReactGraph:
    """A bounded LangGraph ReAct loop for Agent B and the vm_master terminal."""

    def __init__(
        self,
        complete: Completion,
        execute_terminal: TerminalExecutor,
        observe_model: ModelObserver | None = None,
    ) -> None:
        self._complete = complete
        self._execute_terminal = execute_terminal
        self._observe_model = observe_model
        builder = StateGraph(ReactState)
        builder.add_node("model", self._model)
        builder.add_node("tools", self._tools)
        builder.add_node("finalize", self._finalize)
        builder.add_edge(START, "model")
        builder.add_conditional_edges(
            "model",
            self._route_after_model,
            {"tools": "tools", "done": END},
        )
        builder.add_conditional_edges(
            "tools",
            self._route_after_tools,
            {"model": "model", "finalize": "finalize"},
        )
        builder.add_edge("finalize", END)
        self._graph = builder.compile()

    async def _model(self, state: ReactState) -> ReactState:
        result = await self._complete(state["messages"], TERMINAL_TOOLS)
        if self._observe_model is not None:
            await self._observe_model(result, len(state.get("model_results", [])) + 1)
        return {
            **state,
            "messages": [*state["messages"], _assistant_message(result)],
            "model_results": [*state.get("model_results", []), result],
            "last_result": result,
        }

    @staticmethod
    def _route_after_model(state: ReactState) -> str:
        return "tools" if state["last_result"].tool_calls else "done"

    async def _tools(self, state: ReactState) -> ReactState:
        messages = list(state["messages"])
        used = state.get("tool_calls_used", 0)
        ordinal = state.get("next_ordinal", 1)
        maximum = state["max_tool_calls"]
        for call in state["last_result"].tool_calls:
            if used >= maximum:
                observation = {
                    "error": "Tool budget exhausted. Return your best answer without more tools."
                }
            elif call.name != "terminal":
                observation = {"error": f"Unknown tool: {call.name}"}
                used += 1
                ordinal += 1
            else:
                used += 1
                try:
                    TerminalArguments.model_validate(call.arguments)
                    observation = await self._execute_terminal(call, ordinal)
                except ValidationError as exc:
                    observation = {"error": f"Invalid terminal arguments: {exc}"}
                except Exception as exc:
                    observation = {"error": f"Terminal execution failed: {exc}"}
                ordinal += 1
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": _bounded_observation(observation),
                }
            )
        return {
            **state,
            "messages": messages,
            "tool_calls_used": used,
            "next_ordinal": ordinal,
        }

    @staticmethod
    def _route_after_tools(state: ReactState) -> str:
        if state.get("tool_calls_used", 0) >= state["max_tool_calls"]:
            return "finalize"
        return "model"

    async def _finalize(self, state: ReactState) -> ReactState:
        messages = [
            *state["messages"],
            {
                "role": "user",
                "content": (
                    "The terminal-call budget for this turn is exhausted. "
                    "Do not request another tool. Give Agent A your best current answer."
                ),
            },
        ]
        result = await self._complete(messages, None)
        if self._observe_model is not None:
            await self._observe_model(result, len(state.get("model_results", [])) + 1)
        return {
            **state,
            "messages": [*messages, _assistant_message(result)],
            "model_results": [*state.get("model_results", []), result],
            "last_result": result,
        }

    async def run(self, messages: list[dict[str, Any]], *, max_tool_calls: int) -> LLMResult:
        final = await self._graph.ainvoke(
            {
                "messages": messages,
                "model_results": [],
                "tool_calls_used": 0,
                "max_tool_calls": max_tool_calls,
                "next_ordinal": 1,
            },
            {"recursion_limit": max(10, max_tool_calls * 2 + 5)},
        )
        return _aggregate(final["model_results"], final.get("tool_calls_used", 0))
