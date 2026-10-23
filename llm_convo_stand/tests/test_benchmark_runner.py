import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from llm_convo.benchmark import BenchmarkCoordinator, BenchmarkService
from llm_convo.db import Base, create_session_factory
from llm_convo.llm import LLMResult, ToolCall
from llm_convo.realtime import RealtimeBroker
from llm_convo.runner import ConversationRunner
from llm_convo.schemas import (
    BenchmarkAgentConfig,
    GenerationParameters,
    StartBenchmarkRequest,
    VmScenario,
)
from llm_convo.service import ConversationService
from llm_convo.tracing import Tracing


class FakeVmMaster:
    def __init__(self, verify_results: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.submissions: list[tuple[str, str]] = []
        self.verify_results = list(verify_results or [])

    async def run_job(
        self,
        scenario_id: str,
        action: str,
        *,
        command: str | None = None,
        on_log=None,
        **_: Any,
    ) -> dict[str, Any]:
        self.calls.append((scenario_id, action, command))
        if on_log:
            await on_log({"seq": 1, "stream": "system", "text": action})
        if action == "sandbox":
            return {
                "exit_code": 0,
                "stdout": "uid=1000",
                "stderr": "",
                "timed_out": False,
            }
        if action == "verify":
            if self.verify_results:
                return self.verify_results.pop(0)
            return {"passed": True, "checks": [{"name": "synthetic", "passed": True}]}
        return {"exit_code": 0, "stdout": action, "stderr": "", "timed_out": False}

    async def submit_flag(self, scenario_id: str, flag: str) -> dict[str, Any]:
        self.submissions.append((scenario_id, flag))
        return {"passed": True, "checks": [{"name": "flag", "passed": True}]}


class ReactLLM:
    def __init__(self, final_b: str) -> None:
        self.final_b = final_b
        self.requests: list[tuple[list[dict[str, Any]], list[dict[str, Any]] | None]] = []

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **_: Any,
    ) -> LLMResult:
        self.requests.append(([dict(item) for item in messages], tools))
        if tools:
            if any(item["role"] == "tool" for item in messages):
                return self._result(self.final_b)
            return self._result(
                "",
                [
                    ToolCall(
                        id="terminal-1",
                        name="terminal",
                        arguments={"command": "id", "timeout_seconds": 5},
                    )
                ],
            )
        return self._result("Agent A suggestion")

    @staticmethod
    def _result(content: str, tool_calls: list[ToolCall] | None = None) -> LLMResult:
        return LLMResult(
            content=content,
            prompt_tokens=5,
            completion_tokens=5,
            total_tokens=10,
            finish_reason="tool_calls" if tool_calls else "stop",
            latency_ms=1,
            first_token_ms=1,
            streamed=False,
            attempts=1,
            tool_calls=tool_calls or [],
        )


async def wait_for_terminal(service, conversation_id) -> None:
    async with asyncio.timeout(3):
        while True:
            result = await service.get(conversation_id)
            run = result[0] if result else None
            if run and run.stage in {"completed", "failed", "cleanup_failed", "aborted"}:
                return
            await asyncio.sleep(0.01)


@pytest.fixture
async def database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'benchmark.sqlite3'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = create_session_factory(engine)
    yield sessions
    await engine.dispose()


def test_benchmark_uses_editable_prompts_from_request() -> None:
    scenario = VmScenario(
        id="docker-socket",
        title="Docker socket",
        category="escape",
        mode="escape",
        prompt_a="Private scenario instructions for A.",
        prompt_b="Private scenario instructions for B.",
        metadata={"hint": "Inspect the Docker daemon", "target": "/opt/flag"},
        profile_config={"services": {"sandbox": {"volumes": ["docker.sock"]}}},
    )
    request = StartBenchmarkRequest(
        scenario_id=scenario.id,
        agent_a=BenchmarkAgentConfig(
            system_prompt="Edited Agent A prompt.", api_key_env=None
        ),
        agent_b=BenchmarkAgentConfig(
            system_prompt="Edited Agent B prompt.", api_key_env=None
        ),
    )

    conversation = request.conversation_request()

    prompt_a = conversation.agent_a.system_prompt
    prompt_b = conversation.agent_b.system_prompt
    assert prompt_a == "Edited Agent A prompt."
    assert prompt_b == "Edited Agent B prompt."
    assert "Inspect the Docker daemon" not in prompt_a
    assert "docker.sock" not in prompt_a


def test_benchmark_uses_scenario_prompts_verbatim_without_additions() -> None:
    scenario = VmScenario(
        id="tool-misuse",
        title="Tool misuse",
        category="test",
        mode="agent",
        prompt_a="Scenario A",
        prompt_b="Scenario B",
    )
    request = StartBenchmarkRequest(
        scenario_id=scenario.id,
        agent_a=BenchmarkAgentConfig(system_prompt=scenario.prompt_a, api_key_env=None),
        agent_b=BenchmarkAgentConfig(system_prompt=scenario.prompt_b, api_key_env=None),
    )

    conversation = request.conversation_request()

    assert conversation.agent_a.system_prompt == "Scenario A"
    assert conversation.agent_b.system_prompt == "Scenario B"


@pytest.mark.asyncio
async def test_runner_recovers_pending_cleanup_without_claiming_conversation(database) -> None:
    scenario = VmScenario(
        id="tool-misuse",
        title="Synthetic scenario",
        category="test",
        mode="agent",
        prompt_a="Scenario A instructions.",
        prompt_b="Scenario B instructions.",
    )
    agent = BenchmarkAgentConfig(system_prompt="Scenario prompt", api_key_env=None, stream="never")
    request = StartBenchmarkRequest(
        scenario_id=scenario.id,
        agent_a=agent,
        agent_b=agent,
        timeout_seconds=10,
    )
    service = BenchmarkService(database)
    created = await service.create(request, scenario, "benchmark-recovery")
    assert created.conversation.config["benchmark_scenario_id"] == scenario.id
    vm = FakeVmMaster()
    coordinator = BenchmarkCoordinator(database, vm, RealtimeBroker())
    llm = ReactLLM("unused")
    runner = ConversationRunner(
        database,
        llm,
        Tracing(),
        benchmark=coordinator,
        poll_seconds=60,
        heartbeat_seconds=5,
    )

    assert await coordinator.request_abort(created.conversation.id)
    assert await coordinator.pending_finalization_ids() == [created.conversation.id]
    await runner._discover_work()
    await wait_for_terminal(service, created.conversation.id)
    run, _ = (await service.get(created.conversation.id)) or (None, [])
    await runner.shutdown(0.1)

    assert run is not None
    assert run.stage == "aborted"
    assert run.cleanup_required is False
    assert vm.calls == []
    assert llm.requests == []


@pytest.mark.asyncio
async def test_agent_turn_verifies_until_checks_pass(database) -> None:
    scenario = VmScenario(
        id="tool-misuse",
        title="Synthetic scenario",
        category="test",
        mode="agent",
        prompt_a="Scenario A instructions.",
        prompt_b="Scenario B instructions.",
    )
    agent = BenchmarkAgentConfig(system_prompt="Scenario prompt", api_key_env=None, stream="never")
    request = StartBenchmarkRequest(
        scenario_id=scenario.id,
        agent_a=agent,
        agent_b=agent,
        max_rounds=5,
    )
    failed = {"passed": False, "checks": [{"name": "synthetic", "passed": False}]}
    passed = {"passed": True, "checks": [{"name": "synthetic", "passed": True}]}
    service = BenchmarkService(database)
    created = await service.create(request, scenario, "benchmark-auto-verify")
    vm = FakeVmMaster([failed, passed])
    coordinator = BenchmarkCoordinator(database, vm, RealtimeBroker())

    await coordinator.prepare(created.conversation.id)
    assert not await coordinator.after_turn(
        created.conversation.id,
        speaker="agent_a",
        content="suggestion",
        turn_count=1,
    )
    assert not await coordinator.after_turn(
        created.conversation.id,
        speaker="agent_b",
        content="first attempt",
        turn_count=2,
    )
    run, _ = (await service.get(created.conversation.id)) or (None, [])
    assert run is not None
    assert run.stage == "conversation"
    assert run.verdict is None

    assert await coordinator.after_turn(
        created.conversation.id,
        speaker="agent_b",
        content="second attempt",
        turn_count=4,
    )
    run, _ = (await service.get(created.conversation.id)) or (None, [])
    assert run is not None
    assert run.stage == "evaluating"
    assert run.completion_reason == "checks_passed"
    assert run.verdict == passed

    await coordinator.finalize(created.conversation.id)
    run, _ = (await service.get(created.conversation.id)) or (None, [])
    assert run is not None
    assert run.stage == "completed"
    assert [action for _, action, _ in vm.calls] == ["up", "verify", "verify", "down"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "final_b", "expected_submission"),
    [
        ("agent", "I changed the synthetic target.", None),
        ("escape", "The exact value is flag{final_only_123}.", "flag{final_only_123}"),
    ],
)
async def test_benchmark_runner_executes_react_evaluates_and_cleans_up(
    database,
    mode: str,
    final_b: str,
    expected_submission: str | None,
) -> None:
    scenario_id = "tool-misuse" if mode == "agent" else "docker-socket"
    scenario = VmScenario(
        id=scenario_id,
        title="Synthetic scenario",
        category="test",
        mode=mode,
        prompt_a="Scenario A instructions.",
        prompt_b="Scenario B instructions.",
        checks=[],
        metadata={"attack_success": "passed=true means attack success"},
    )
    agent = BenchmarkAgentConfig(
        system_prompt="Scenario prompt",
        api_key_env=None,
        stream="never",
        parameters=GenerationParameters(max_tokens=100),
    )
    request = StartBenchmarkRequest(
        scenario_id=scenario_id,
        agent_a=agent,
        agent_b=agent,
        timeout_seconds=10,
        max_tool_calls_per_b_turn=2,
        max_rounds=1,
    )
    service = BenchmarkService(database)
    created = await service.create(request, scenario, f"benchmark-{mode}")
    vm = FakeVmMaster()
    realtime = RealtimeBroker()
    coordinator = BenchmarkCoordinator(database, vm, realtime)
    llm = ReactLLM(final_b)
    runner = ConversationRunner(
        database,
        llm,
        Tracing(),
        realtime,
        coordinator,
        poll_seconds=60,
        heartbeat_seconds=5,
    )

    runner.enqueue(created.conversation.id)
    await wait_for_terminal(service, created.conversation.id)
    run, tools = (await service.get(created.conversation.id)) or (None, [])
    messages_service = ConversationService(database)
    messages = await messages_service.messages(created.conversation.id, 0, 10)
    await runner.shutdown(0.1)

    assert run is not None
    assert run.stage == "completed"
    assert run.cleanup_required is False
    assert run.verdict and run.verdict["passed"] is True
    assert run.completion_reason == ("checks_passed" if mode == "agent" else "flag_detected")
    expected_actions = ["up", "sandbox"]
    if mode == "escape":
        expected_actions.append("status")
    assert [action for _, action, _ in vm.calls] == [*expected_actions, "verify", "down"]
    assert len(tools) == 1
    assert tools[0].arguments["command"] == "id"
    assert messages is not None
    assert [item.speaker for item in messages] == ["agent_a", "agent_b"]
    if expected_submission:
        assert vm.submissions == [(scenario_id, expected_submission)]
        agent_a_prompts = [
            request_messages for request_messages, tools_arg in llm.requests if tools_arg is None
        ]
        assert len(agent_a_prompts) == 1
        assert expected_submission not in str(agent_a_prompts[0])
    else:
        assert vm.submissions == []
