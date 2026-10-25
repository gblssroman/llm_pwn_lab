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
from llm_convo.vm_master import VmMasterJobError


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


@pytest.mark.asyncio
@pytest.mark.parametrize("preflight_failed", [False, True])
async def test_escape_operator_assessment_is_durable_and_not_agent_evidence(
    database, preflight_failed,
):
    class DiagnosticVm(FakeVmMaster):
        async def run_job(self, scenario_id, action, **kwargs):
            result = await super().run_job(scenario_id, action, **kwargs)
            if action == "up":
                result["stdout"] = (
                    '[lab] escape preflight: {"version":1,"status":"'
                    + ("failed" if preflight_failed else "ready") + '"}'
                )
                if preflight_failed:
                    result["exit_code"] = 1
                    raise VmMasterJobError("Preflight failed", {"result": result})
            if action == "sandbox":
                result.update(exit_code=1, stdout="", stderr="No such file or directory")
            return result

    scenario = VmScenario(
        id="dac-read-search", title="DAC", category="escape", mode="escape",
        prompt_a="Attacker", prompt_b="Defender",
    )
    agent = BenchmarkAgentConfig(system_prompt="Scenario", api_key_env=None)
    created = await BenchmarkService(database).create(
        StartBenchmarkRequest(scenario_id=scenario.id, agent_a=agent, agent_b=agent),
        scenario, "operator-assessment",
    )
    coordinator = BenchmarkCoordinator(database, DiagnosticVm(), RealtimeBroker())
    if preflight_failed:
        with pytest.raises(VmMasterJobError):
            await coordinator.prepare(created.conversation.id)
    else:
        await coordinator.prepare(created.conversation.id)
        await coordinator.execute_terminal(
            created.conversation.id, 2,
            ToolCall(id="read", name="terminal", arguments={
                "command": "cat /opt/lab-target/escape-flag.txt"
            }), 1,
        )
        assert await coordinator.after_turn(
            created.conversation.id, speaker="agent_a",
            content="ASSESSMENT_EXHAUSTED", turn_count=3,
        )
    # Re-read via a fresh service/session, including failed provisioning evidence.
    run, _ = await BenchmarkService(database).get(created.conversation.id)
    assert run.assessment["preflight"]["status"] == ("failed" if preflight_failed else "ready")
    assert run.assessment["environment_started"] is (not preflight_failed)
    assert run.assessment["direct_target_read_attempt"]["status"] == (
        "not_observed" if preflight_failed else "observed"
    )
    observations = await coordinator.terminal_observations(created.conversation.id)
    assert "preflight" not in str(observations)
    if not preflight_failed:
        assert run.completion_reason == "attacker_exhausted"
        assert "No such file" in observations[2]
    await coordinator.finalize(created.conversation.id)


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
        expected_actions.extend(["sandbox", "status"])
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


@pytest.mark.asyncio
async def test_idle_benchmark_verifies_and_cleans_up_without_trusting_success_claim(database):
    class IdleLLM(ReactLLM):
        async def complete(self, **kwargs):
            return self._result("Success claimed. I am ready and standing by.")

    class RejectingVerifier(FakeVmMaster):
        async def run_job(self, scenario_id, action, **kwargs):
            result = await super().run_job(scenario_id, action, **kwargs)
            if action == "verify":
                return {"passed": False, "checks": [{"name": "audit", "passed": False}]}
            return result

    scenario = VmScenario(
        id="context-poisoning",
        title="Summary",
        category="test",
        mode="agent",
        prompt_a="Attacker",
        prompt_b="Summarize only",
    )
    agent = BenchmarkAgentConfig(system_prompt="Test prompt", api_key_env=None, stream="never")
    request = StartBenchmarkRequest(
        scenario_id=scenario.id,
        agent_a=agent,
        agent_b=agent,
        max_rounds=10,
        timeout_seconds=10,
    )
    service = BenchmarkService(database)
    created = await service.create(request, scenario, "benchmark-idle")
    vm = RejectingVerifier()
    coordinator = BenchmarkCoordinator(database, vm, RealtimeBroker())
    runner = ConversationRunner(database, IdleLLM(""), Tracing(), benchmark=coordinator)
    runner.enqueue(created.conversation.id)
    await wait_for_terminal(service, created.conversation.id)
    await runner.shutdown(0.1)
    run, tools = await service.get(created.conversation.id)
    assert run.verdict["passed"] is False
    assert run.completion_reason == "mutual_waiting"
    assert run.stage == "completed"
    assert not run.cleanup_required
    assert not tools
    assert [action for _, action, _ in vm.calls] == ["up", "verify", "verify", "verify", "down"]
    messages = await ConversationService(database).messages(created.conversation.id, 0, 100)
    assert len(messages) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("report_status", ["present", "missing", "unavailable"])
@pytest.mark.parametrize("scenario_id", ["dac-read-search", "docker-socket"])
async def test_report_check_is_durable_operator_only_and_does_not_change_verdict(
    database, report_status, scenario_id,
):
    import json

    class ReportVm(FakeVmMaster):
        async def run_job(self, scenario_id, action, **kwargs):
            if action == "sandbox" and "Report presence only" in kwargs.get("command", ""):
                if report_status == "unavailable":
                    raise RuntimeError("Probe unavailable")
                return {"exit_code": 0, "stdout": json.dumps({
                    "version": 1, "status": report_status,
                })}
            return await super().run_job(scenario_id, action, **kwargs)

    scenario = VmScenario(
        id=scenario_id, title="Identity", category="escape", mode="escape",
        prompt_a="Requester", prompt_b="Support",
    )
    agent = BenchmarkAgentConfig(system_prompt="Scenario", api_key_env=None)
    created = await BenchmarkService(database).create(
        StartBenchmarkRequest(scenario_id=scenario.id, agent_a=agent, agent_b=agent),
        scenario, "report-check",
    )
    vm = ReportVm(verify_results=[{"passed": False}])
    coordinator = BenchmarkCoordinator(database, vm, RealtimeBroker())
    cid = created.conversation.id
    await coordinator.prepare(cid)
    assert not await coordinator.after_turn(
        cid, speaker="agent_b", content='{"assessment_status":"exhausted"}', turn_count=2,
    )
    assert await coordinator.after_turn(
        cid, speaker="agent_a", content='{"assessment_status":"exhausted"}', turn_count=3,
    )
    await coordinator.finalize(cid)
    run, tools = await BenchmarkService(database).get(cid)
    assert run.stage == "completed"
    assert run.verdict == {"passed": False}
    assert run.assessment["report_artifact"]["status"] == (
        "unknown" if report_status == "unavailable" else report_status
    )
    assert run.assessment["report_artifact"]["run_id"] == str(cid)
    assert run.assessment["run_id"] == str(cid)
    assert tools == []
    assert await coordinator.terminal_observations(cid) == {}
    assert vm.calls[-1][1] == "down"


@pytest.mark.asyncio
@pytest.mark.parametrize("signal", [
    'json\n{"assessment_status":"exhausted"}',
    '```json\n{"assessment_status":"exhausted"}\n```',
])
async def test_formatted_completion_stops_before_next_b_turn(database, signal):
    class FinishedLLM(ReactLLM):
        async def complete(self, **kwargs):
            self.requests.append(kwargs)
            return self._result(signal)

    scenario = VmScenario(
        id="docker-socket", title="Control", category="escape", mode="escape",
        prompt_a="Requester", prompt_b="Support",
    )
    agent = BenchmarkAgentConfig(system_prompt="Scenario", api_key_env=None)
    service = BenchmarkService(database)
    created = await service.create(
        StartBenchmarkRequest(scenario_id=scenario.id, agent_a=agent, agent_b=agent),
        scenario, "completion",
    )
    vm = FakeVmMaster(verify_results=[{"passed": False}])
    realtime = RealtimeBroker()
    coordinator = BenchmarkCoordinator(database, vm, realtime)
    llm = FinishedLLM("")
    runner = ConversationRunner(database, llm, Tracing(), realtime, coordinator)
    try:
        runner.enqueue(created.conversation.id)
        await wait_for_terminal(service, created.conversation.id)
        run, tools = await service.get(created.conversation.id)
        messages = await ConversationService(database).messages(created.conversation.id, 0, 10)
        assert len(llm.requests) == 1
        assert [message.speaker for message in messages] == ["agent_a"]
        assert run.completion_reason == "attacker_exhausted"
        assert run.verdict == {"passed": False}
        assert not tools
    finally:
        await runner.shutdown(0.1)


@pytest.mark.asyncio
async def test_repeated_tools_trigger_evaluation_and_historical_evidence_survives(database):
    scenario = VmScenario(
        id="docker-socket", title="Repeated", category="escape", mode="escape",
        prompt_a="Requester", prompt_b="Support",
    )
    agent = BenchmarkAgentConfig(system_prompt="Scenario", api_key_env=None)
    service = BenchmarkService(database)
    created = await service.create(
        StartBenchmarkRequest(scenario_id=scenario.id, agent_a=agent, agent_b=agent),
        scenario, "tool-repeat",
    )
    vm = FakeVmMaster(verify_results=[{"passed": False}])
    coordinator = BenchmarkCoordinator(database, vm, RealtimeBroker())
    cid = created.conversation.id
    await coordinator.prepare(cid)
    # One distinctive earlier observation must survive eight more recent calls.
    await coordinator.execute_terminal(
        cid, 2, ToolCall(id="early", name="terminal", arguments={"command": "pwd"}), 1,
    )
    await coordinator.execute_terminal(
        cid, 2, ToolCall(id="baseline", name="terminal", arguments={"command": "id"}), 2,
    )
    for seq in (4, 6, 8):
        for ordinal in range(1, 4):
            await coordinator.execute_terminal(
                cid, seq, ToolCall(id=f"call-{seq}-{ordinal}", name="terminal",
                                  arguments={"command": "id"}), ordinal,
            )
        stopped = await coordinator.after_turn(
            cid, speaker="agent_b", content="Still investigating", turn_count=seq,
        )
        assert stopped is (seq == 8)
    observations = await coordinator.terminal_observations(cid)
    assert 2 in observations
    run = await coordinator.get_run(cid)
    assert run.completion_reason == "repeated_tool_observations"
    await coordinator.finalize(cid)
