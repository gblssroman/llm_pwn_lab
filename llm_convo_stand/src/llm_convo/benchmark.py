import asyncio
import hashlib
import json
import re
import shlex
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from llm_convo.escape_assessment import (
    REPORT_PROBE_SOURCE,
    assessment_exhausted,
    build_escape_assessment,
)
from llm_convo.llm import ToolCall
from llm_convo.loop_detection import repeated_tool_reason
from llm_convo.models import (
    BenchmarkRun,
    Conversation,
    ConversationEvent,
    ConversationStatus,
    Message,
    ToolExecution,
    utc_now,
)
from llm_convo.realtime import RealtimeBroker
from llm_convo.schemas import StartBenchmarkRequest, VmScenario
from llm_convo.service import IdempotencyConflictError
from llm_convo.vm_master import VmMasterClient, VmMasterJobError

FLAG_PATTERN = re.compile(r"flag\{[a-zA-Z0-9_-]+\}")


@dataclass(slots=True)
class BenchmarkCreateResult:
    conversation: Conversation
    benchmark: BenchmarkRun
    deduplicated: bool


def benchmark_fingerprint(body: StartBenchmarkRequest, scenario: VmScenario) -> str:
    canonical = body.model_dump_json(exclude_none=False) + "\n" + scenario.model_dump_json()
    return hashlib.sha256(canonical.encode()).hexdigest()


class BenchmarkService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self,
        body: StartBenchmarkRequest,
        scenario: VmScenario,
        idempotency_key: str,
    ) -> BenchmarkCreateResult:
        fingerprint = benchmark_fingerprint(body, scenario)
        request = body.conversation_request()
        persistent_config = request.persistent_config()
        persistent_config["benchmark_scenario_id"] = scenario.id
        async with self._sessions() as session:
            existing = await session.scalar(
                select(Conversation).where(Conversation.idempotency_key == idempotency_key)
            )
            if existing is not None:
                if existing.request_hash != fingerprint:
                    raise IdempotencyConflictError(
                        "Idempotency-Key was already used with a different request body"
                    )
                benchmark = await session.get(BenchmarkRun, existing.id)
                if benchmark is None:
                    raise IdempotencyConflictError(
                        "Idempotency-Key belongs to a non-benchmark conversation"
                    )
                return BenchmarkCreateResult(existing, benchmark, True)

            conversation = Conversation(
                id=uuid.uuid4(),
                status=ConversationStatus.queued.value,
                config=persistent_config,
                context_size=request.context_size,
                compression_threshold=request.compression_threshold,
                retain_recent_messages=request.retain_recent_messages,
                timeout_seconds=request.timeout_seconds,
                idempotency_key=idempotency_key,
                request_hash=fingerprint,
            )
            benchmark = BenchmarkRun(
                conversation_id=conversation.id,
                scenario_id=scenario.id,
                scenario_mode=scenario.mode,
                scenario=scenario.model_dump(mode="json"),
                stage="queued",
                max_tool_calls_per_b_turn=body.max_tool_calls_per_b_turn,
                max_rounds=body.max_rounds,
            )
            session.add(conversation)
            try:
                await session.flush()
                session.add_all(
                    [
                        benchmark,
                        ConversationEvent(
                            conversation=conversation,
                            event_type="conversation_queued",
                            payload={
                                "idempotency_key": idempotency_key,
                                "benchmark": True,
                                "scenario_id": scenario.id,
                            },
                        ),
                    ]
                )
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(Conversation).where(Conversation.idempotency_key == idempotency_key)
                )
                if existing is None:
                    raise
                if existing.request_hash != fingerprint:
                    raise IdempotencyConflictError(
                        "Idempotency-Key was already used with a different request body"
                    ) from None
                benchmark = await session.get(BenchmarkRun, existing.id)
                if benchmark is None:
                    raise IdempotencyConflictError(
                        "Idempotency-Key belongs to a non-benchmark conversation"
                    ) from None
                return BenchmarkCreateResult(existing, benchmark, True)
            await session.refresh(conversation)
            await session.refresh(benchmark)
            return BenchmarkCreateResult(conversation, benchmark, False)

    async def get(
        self, conversation_id: uuid.UUID
    ) -> tuple[BenchmarkRun, list[ToolExecution]] | None:
        async with self._sessions() as session:
            run = await session.get(BenchmarkRun, conversation_id)
            if run is None:
                return None
            tools = await session.scalars(
                select(ToolExecution)
                .where(ToolExecution.conversation_id == conversation_id)
                .order_by(
                    ToolExecution.message_sequence,
                    ToolExecution.ordinal,
                    ToolExecution.created_at,
                )
            )
            tools = list(tools)
            if run.scenario_mode == "escape":
                up_event = await session.scalar(
                    select(ConversationEvent)
                    .where(
                        ConversationEvent.conversation_id == conversation_id,
                        ConversationEvent.event_type == "vm_job_completed",
                        ConversationEvent.payload["action"].as_string() == "up",
                    )
                    .order_by(ConversationEvent.created_at.desc())
                    .limit(1)
                )
                report_event = await session.scalar(
                    select(ConversationEvent).where(
                        ConversationEvent.conversation_id == conversation_id,
                        ConversationEvent.event_type == "report_artifact_checked",
                    ).order_by(ConversationEvent.created_at.desc()).limit(1)
                )
                run.assessment = build_escape_assessment(
                    run, tools, up_event.payload.get("result") if up_event else None,
                    report_event.payload if report_event else None,
                )
            return run, tools

    async def run(self, conversation_id: uuid.UUID) -> BenchmarkRun | None:
        async with self._sessions() as session:
            return await session.get(BenchmarkRun, conversation_id)


class BenchmarkCoordinator:
    """Owns the VM lifecycle while ConversationRunner owns model execution."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        client: VmMasterClient,
        realtime: RealtimeBroker,
    ) -> None:
        self._sessions = sessions
        self.client = client
        self._realtime = realtime
        self._vm_lock = asyncio.Lock()
        self._held: set[uuid.UUID] = set()

    async def is_benchmark(self, conversation_id: uuid.UUID) -> bool:
        async with self._sessions() as session:
            return await session.get(BenchmarkRun, conversation_id) is not None

    async def get_run(self, conversation_id: uuid.UUID) -> BenchmarkRun | None:
        async with self._sessions() as session:
            return await session.get(BenchmarkRun, conversation_id)

    async def _event(
        self,
        conversation_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        async with self._sessions() as session, session.begin():
            event = ConversationEvent(
                conversation_id=conversation_id,
                event_type=event_type,
                payload=payload,
            )
            session.add(event)
            await session.flush()
            event_id = str(event.id)
        await self._realtime.publish(
            conversation_id,
            event_type,
            {**payload, "durable_event_id": event_id},
        )

    async def _set_stage(
        self,
        conversation_id: uuid.UUID,
        stage: str,
        *,
        conversation_status: str | None = None,
        extra: dict[str, Any] | None = None,
        cleanup_required: bool | None = None,
    ) -> None:
        now = utc_now()
        async with self._sessions() as session, session.begin():
            run = await session.get(BenchmarkRun, conversation_id)
            conversation = await session.get(Conversation, conversation_id)
            if run is None or conversation is None:
                return
            run.stage = stage
            run.updated_at = now
            if conversation_status is not None:
                conversation.status = conversation_status
                conversation.updated_at = now
            if cleanup_required is not None:
                run.cleanup_required = cleanup_required
            event = ConversationEvent(
                conversation_id=conversation_id,
                event_type="benchmark_stage_changed",
                payload={"stage": stage, **(extra or {})},
            )
            session.add(event)
            await session.flush()
            event_id = str(event.id)
        await self._realtime.publish(
            conversation_id,
            "benchmark_stage_changed",
            {"stage": stage, **(extra or {}), "durable_event_id": event_id},
        )

    async def _job_log(
        self,
        conversation_id: uuid.UUID,
        action: str,
        event: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> None:
        await self._event(
            conversation_id,
            "vm_job_log",
            {
                "action": action,
                "tool_call_id": tool_call_id,
                "seq": event.get("seq"),
                "stream": event.get("stream"),
                "text": str(event.get("text") or "")[:4_096],
            },
        )

    async def prepare(self, conversation_id: uuid.UUID) -> None:
        if conversation_id not in self._held:
            await self._vm_lock.acquire()
            self._held.add(conversation_id)
        run = await self.get_run(conversation_id)
        if run is None:
            self.release(conversation_id)
            return
        if run.stage == "conversation":
            return
        await self._set_stage(conversation_id, "provisioning", cleanup_required=True)
        try:
            result = await self.client.run_job(
                run.scenario_id,
                "up",
                on_log=lambda event: self._job_log(conversation_id, "up", event),
            )
            await self._event(
                conversation_id,
                "vm_job_completed",
                {"action": "up", "result": result},
            )
            now = utc_now()
            async with self._sessions() as session, session.begin():
                current = await session.get(BenchmarkRun, conversation_id)
                conversation = await session.get(Conversation, conversation_id)
                if current is None or conversation is None:
                    return
                current.stage = "conversation"
                current.updated_at = now
                conversation.started_at = now
                conversation.deadline_at = now + timedelta(seconds=conversation.timeout_seconds)
                conversation.updated_at = now
            await self._set_stage(conversation_id, "conversation")
        except Exception as exc:
            if isinstance(exc, VmMasterJobError) and isinstance(exc.job.get("result"), dict):
                await self._event(
                    conversation_id,
                    "vm_job_completed",
                    {"action": "up", "result": exc.job["result"]},
                )
            await self.mark_failure(conversation_id, exc)
            raise

    async def execute_terminal(
        self,
        conversation_id: uuid.UUID,
        message_sequence: int,
        call: ToolCall,
        ordinal: int,
    ) -> dict[str, Any]:
        run = await self.get_run(conversation_id)
        if run is None:
            return {"error": "Benchmark run no longer exists"}
        command = call.arguments.get("command")
        timeout_seconds = call.arguments.get("timeout_seconds", 300)
        execution = ToolExecution(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            message_sequence=message_sequence,
            ordinal=ordinal,
            tool_call_id=call.id,
            name=call.name,
            arguments=call.arguments,
            status="running",
        )
        async with self._sessions() as session, session.begin():
            session.add(execution)
            event = ConversationEvent(
                conversation_id=conversation_id,
                event_type="tool_started",
                payload={
                    "tool_execution_id": str(execution.id),
                    "tool_call_id": call.id,
                    "message_sequence": message_sequence,
                    "ordinal": ordinal,
                    "name": call.name,
                    "arguments": call.arguments,
                },
            )
            session.add(event)
            await session.flush()
            event_id = str(event.id)
        await self._realtime.publish(
            conversation_id,
            "tool_started",
            {
                "tool_execution_id": str(execution.id),
                "tool_call_id": call.id,
                "message_sequence": message_sequence,
                "ordinal": ordinal,
                "name": call.name,
                "arguments": call.arguments,
                "durable_event_id": event_id,
            },
        )
        try:
            result = await self.client.run_job(
                run.scenario_id,
                "sandbox",
                command=str(command),
                timeout_seconds=int(timeout_seconds),
                job_timeout_seconds=float(timeout_seconds) + 60,
                on_log=lambda event: self._job_log(
                    conversation_id, "sandbox", event, tool_call_id=call.id
                ),
            )
        except Exception as exc:
            now = utc_now()
            async with self._sessions() as session, session.begin():
                current = await session.get(ToolExecution, execution.id)
                if current is not None:
                    current.status = "failed"
                    current.error = str(exc)[:10_000]
                    current.completed_at = now
            await self._event(
                conversation_id,
                "tool_completed",
                {
                    "tool_execution_id": str(execution.id),
                    "tool_call_id": call.id,
                    "status": "failed",
                    "error": str(exc)[:2_000],
                },
            )
            return {"error": str(exc), "timed_out": False}
        now = utc_now()
        status = "failed" if result.get("exit_code") or result.get("timed_out") else "completed"
        async with self._sessions() as session, session.begin():
            current = await session.get(ToolExecution, execution.id)
            if current is not None:
                current.status = status
                current.result = result
                current.completed_at = now
        await self._event(
            conversation_id,
            "tool_completed",
            {
                "tool_execution_id": str(execution.id),
                "tool_call_id": call.id,
                "status": status,
                "result": result,
            },
        )
        return result

    async def terminal_observations(self, conversation_id: uuid.UUID) -> dict[int, str]:
        """Share bounded shell evidence across turns, without evaluator data."""
        async with self._sessions() as session:
            rows = list(await session.scalars(
                select(ToolExecution)
                .where(
                    ToolExecution.conversation_id == conversation_id,
                    ToolExecution.status.in_(("completed", "failed")),
                )
                .order_by(ToolExecution.message_sequence.desc(), ToolExecution.ordinal.desc())
                .limit(200)
            ))
        # Keep the latest eight calls plus eight distinct earlier observations.
        # Selection uses tool evidence only and remains bounded after compression.
        selected = list(rows[:8])
        seen = {json.dumps([row.arguments, row.result], sort_keys=True) for row in selected}
        for row in reversed(rows[8:]):
            key = json.dumps([row.arguments, row.result], sort_keys=True)
            if key not in seen:
                selected.append(row)
                seen.add(key)
            if len(selected) >= 16:
                break
        selected.sort(key=lambda row: (row.message_sequence, row.ordinal))
        observations: dict[int, str] = {}
        for row in selected:
            evidence = {
                "command": row.arguments.get("command", "")[:1000],
                **(row.result or {"error": row.error}),
            }
            for key in ("stdout", "stderr"):
                value = evidence.get(key)
                if isinstance(value, str) and len(value) > 1000:
                    evidence[key] = value[:500] + "\n...[truncated]...\n" + value[-500:]
            observations[row.message_sequence] = (
                observations.get(row.message_sequence, "")
                + json.dumps(evidence, ensure_ascii=False) + "\n"
            )
        return observations

    async def record_agent_step(
        self,
        conversation_id: uuid.UUID,
        message_sequence: int,
        result: Any,
        step: int,
    ) -> None:
        """Persist one internal Agent B ReAct response before any requested tools run."""
        await self._event(
            conversation_id,
            "agent_react_step",
            {
                "message_sequence": message_sequence,
                "step": step,
                "content": result.content or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "name": call.name,
                        "arguments": call.arguments,
                    }
                    for call in result.tool_calls
                ],
                "final": not bool(result.tool_calls),
                "model": result.metadata.get("model") if result.metadata else None,
            },
        )

    async def after_turn(
        self,
        conversation_id: uuid.UUID,
        *,
        speaker: str,
        content: str,
        turn_count: int,
    ) -> bool:
        run = await self.get_run(conversation_id)
        if run is None or run.stage != "conversation":
            return False
        if speaker == "agent_a":
            if run.scenario_mode == "escape" and assessment_exhausted(content):
                return await self.begin_evaluation(conversation_id, reason="attacker_exhausted")
            return False
        if speaker != "agent_b":
            return False
        turn_verdict: dict[str, Any] | None = None
        if run.scenario_mode == "agent":
            try:
                turn_verdict = await self._verify(conversation_id, run)
                await self._event(
                    conversation_id,
                    "benchmark_progress_checked",
                    {"verdict": turn_verdict},
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._event(
                    conversation_id,
                    "benchmark_progress_check_failed",
                    {"error": str(exc)[:2_000]},
                )
            else:
                if turn_verdict.get("passed") is True:
                    started = await self.begin_evaluation(
                        conversation_id,
                        reason="checks_passed",
                        final_status=ConversationStatus.completed,
                        verdict=turn_verdict,
                    )
                    if started:
                        await self._event(
                            conversation_id,
                            "benchmark_verified",
                            {"verdict": turn_verdict, "automatic": True},
                        )
                    return started
        if run.scenario_mode == "escape":
            candidates = FLAG_PATTERN.findall(content)
            if candidates:
                async with self._sessions() as session, session.begin():
                    current = await session.get(BenchmarkRun, conversation_id)
                    if current is not None:
                        current.flag_candidate = candidates[-1]
                        current.updated_at = utc_now()
                await self.begin_evaluation(
                    conversation_id,
                    reason="flag_detected",
                    final_status=ConversationStatus.completed,
                )
                return True
        if run.scenario_mode == "escape":
            async with self._sessions() as session:
                tools = list(await session.scalars(
                    select(ToolExecution).where(
                        ToolExecution.conversation_id == conversation_id,
                    ).order_by(ToolExecution.message_sequence.desc(), ToolExecution.ordinal.desc())
                    .limit(200)
                ))
            reason = repeated_tool_reason(list(reversed(tools)))
            if reason:
                return await self.begin_evaluation(conversation_id, reason=reason)
        if turn_count // 2 >= run.max_rounds:
            started = await self.begin_evaluation(
                conversation_id,
                reason="max_rounds",
                final_status=ConversationStatus.completed,
                verdict=turn_verdict,
            )
            if started and turn_verdict is not None:
                await self._event(
                    conversation_id,
                    "benchmark_verified",
                    {"verdict": turn_verdict, "automatic": True},
                )
            return True
        return False

    async def _verify(
        self,
        conversation_id: uuid.UUID,
        run: BenchmarkRun,
    ) -> dict[str, Any]:
        await self._event(
            conversation_id,
            "benchmark_verification_started",
            {"automatic": run.stage == "conversation"},
        )
        evidence = None
        if any(
            check.get("type") == "protected_disclosure"
            for check in run.scenario.get("checks", [])
        ):
            async with self._sessions() as session:
                replies = await session.scalars(
                    select(Message.content).where(
                        Message.conversation_id == conversation_id,
                        Message.speaker == "agent_b",
                    ).order_by(Message.sequence)
                )
                evidence = {"agent_b_messages": list(replies)}
        verdict = await self.client.run_job(
            run.scenario_id,
            "verify",
            on_log=lambda event: self._job_log(conversation_id, "verify", event),
            wait_if_busy=True,
            verification_evidence=evidence,
        )
        await self._event(
            conversation_id,
            "vm_job_completed",
            {"action": "verify", "result": verdict},
        )
        return verdict

    async def begin_evaluation(
        self,
        conversation_id: uuid.UUID,
        *,
        reason: str,
        final_status: ConversationStatus = ConversationStatus.completed,
        verdict: dict[str, Any] | None = None,
    ) -> bool:
        now = utc_now()
        async with self._sessions() as session, session.begin():
            run = await session.get(BenchmarkRun, conversation_id)
            conversation = await session.get(Conversation, conversation_id)
            if run is None or conversation is None:
                return False
            if run.stage != "conversation":
                return False
            run.stage = "evaluating"
            run.completion_reason = reason
            run.final_conversation_status = final_status.value
            if verdict is not None:
                run.verdict = verdict
            run.updated_at = now
            conversation.status = ConversationStatus.evaluating.value
            conversation.updated_at = now
            event = ConversationEvent(
                conversation_id=conversation_id,
                event_type="benchmark_stage_changed",
                payload={"stage": "evaluating", "reason": reason},
            )
            session.add(event)
            await session.flush()
            event_id = str(event.id)
        await self._realtime.publish(
            conversation_id,
            "benchmark_stage_changed",
            {
                "stage": "evaluating",
                "reason": reason,
                "durable_event_id": event_id,
            },
        )

        return True

    async def _check_report_artifact(self, conversation_id: uuid.UUID, run: BenchmarkRun) -> None:
        """Operator-only presence check before cleanup; never changes the flag verdict."""
        observation = {"version": 1, "status": "unknown", "run_id": str(conversation_id)}
        try:
            result = await self.client.run_job(
                run.scenario_id, "sandbox",
                command="python3 -c " + shlex.quote(REPORT_PROBE_SOURCE),
                timeout_seconds=5, job_timeout_seconds=15, wait_if_busy=True,
            )
            if result.get("exit_code") == 0 and not result.get("timed_out"):
                value = json.loads(result.get("stdout", ""))
                if isinstance(value, dict) and value.get("version") == 1:
                    if value.get("status") in {"present", "missing", "unknown"}:
                        observation["status"] = value["status"]
                        size = value.get("size_bytes")
                        if isinstance(size, int) and size >= 0:
                            observation["size_bytes"] = size
        except Exception:
            # A diagnostic failure must not prevent verification or VM cleanup.
            pass
        await self._event(conversation_id, "report_artifact_checked", observation)

    async def request_abort(self, conversation_id: uuid.UUID) -> bool:
        now = utc_now()
        async with self._sessions() as session, session.begin():
            run = await session.get(BenchmarkRun, conversation_id)
            conversation = await session.get(Conversation, conversation_id)
            if run is None or conversation is None:
                return False
            if run.stage in {"completed", "failed", "aborted", "cleanup_failed"}:
                return True
            run.stage = "cleaning_up"
            run.completion_reason = "aborted"
            run.final_conversation_status = ConversationStatus.stopped.value
            run.updated_at = now
            conversation.status = ConversationStatus.cleaning_up.value
            conversation.updated_at = now
        await self._event(
            conversation_id,
            "benchmark_stage_changed",
            {"stage": "cleaning_up", "reason": "aborted"},
        )
        return True

    async def mark_failure(self, conversation_id: uuid.UUID, error: Exception) -> None:
        now = utc_now()
        async with self._sessions() as session, session.begin():
            run = await session.get(BenchmarkRun, conversation_id)
            conversation = await session.get(Conversation, conversation_id)
            if run is None or conversation is None:
                return
            run.stage = "cleaning_up"
            run.error = str(error)[:10_000]
            run.completion_reason = "failed"
            run.final_conversation_status = ConversationStatus.failed.value
            run.updated_at = now
            conversation.status = ConversationStatus.cleaning_up.value
            conversation.error = str(error)[:10_000]
            conversation.updated_at = now
        await self._event(
            conversation_id,
            "benchmark_stage_changed",
            {"stage": "cleaning_up", "reason": "failed", "error": str(error)[:2_000]},
        )

    async def pending_finalization_ids(self) -> list[uuid.UUID]:
        """Return interrupted evaluation/cleanup runs for worker recovery."""
        async with self._sessions() as session:
            result = await session.scalars(
                select(BenchmarkRun.conversation_id).where(
                    BenchmarkRun.stage.in_({"evaluating", "cleaning_up"})
                )
            )
            return list(result)

    async def should_finalize(self, conversation_id: uuid.UUID) -> bool:
        run = await self.get_run(conversation_id)
        return bool(run and run.stage in {"evaluating", "cleaning_up"})

    async def finalize(self, conversation_id: uuid.UUID) -> None:
        run = await self.get_run(conversation_id)
        if run is None:
            self.release(conversation_id)
            return
        evaluation_error: Exception | None = None
        if (
            run.cleanup_required or run.stage == "evaluating"
        ) and conversation_id not in self._held:
            await self._vm_lock.acquire()
            self._held.add(conversation_id)
            run = await self.get_run(conversation_id) or run

        if run.stage == "evaluating":
            if run.scenario_mode == "escape":
                await self._check_report_artifact(conversation_id, run)
            try:
                if run.scenario_mode == "escape" and run.flag_candidate:
                    barrier = await self.client.run_job(
                        run.scenario_id,
                        "status",
                        on_log=lambda event: self._job_log(conversation_id, "status", event),
                        wait_if_busy=True,
                    )
                    await self._event(
                        conversation_id,
                        "vm_job_completed",
                        {"action": "status", "result": barrier},
                    )
                    submit = await self.client.submit_flag(run.scenario_id, run.flag_candidate)
                    async with self._sessions() as session, session.begin():
                        current = await session.get(BenchmarkRun, conversation_id)
                        if current is not None:
                            current.submit_result = submit
                            current.updated_at = utc_now()
                    await self._event(
                        conversation_id,
                        "benchmark_flag_submitted",
                        {"submitted": True, "passed": submit.get("passed")},
                    )
                if run.verdict is None:
                    verdict = await self._verify(conversation_id, run)
                    async with self._sessions() as session, session.begin():
                        current = await session.get(BenchmarkRun, conversation_id)
                        if current is not None:
                            current.verdict = verdict
                            current.updated_at = utc_now()
                    await self._event(
                        conversation_id,
                        "benchmark_verified",
                        {"verdict": verdict},
                    )
            except Exception as exc:
                evaluation_error = exc
                async with self._sessions() as session, session.begin():
                    current = await session.get(BenchmarkRun, conversation_id)
                    if current is not None:
                        current.error = str(exc)[:10_000]
                        current.final_conversation_status = ConversationStatus.failed.value
                        current.updated_at = utc_now()
                await self._event(
                    conversation_id,
                    "benchmark_evaluation_failed",
                    {"error": str(exc)[:2_000]},
                )

        await self._set_stage(
            conversation_id,
            "cleaning_up",
            conversation_status=ConversationStatus.cleaning_up.value,
        )
        cleanup_error: Exception | None = None
        if run.cleanup_required:
            try:
                down_result = await self.client.run_job(
                    run.scenario_id,
                    "down",
                    on_log=lambda event: self._job_log(conversation_id, "down", event),
                    wait_if_busy=True,
                )
                await self._event(
                    conversation_id,
                    "vm_job_completed",
                    {"action": "down", "result": down_result},
                )
            except Exception as exc:
                cleanup_error = exc
                await self._event(
                    conversation_id,
                    "benchmark_cleanup_failed",
                    {"error": str(exc)[:2_000]},
                )

        now = utc_now()
        async with self._sessions() as session, session.begin():
            current = await session.get(BenchmarkRun, conversation_id)
            conversation = await session.get(Conversation, conversation_id)
            if current is None or conversation is None:
                self.release(conversation_id)
                return
            if cleanup_error is not None:
                current.cleanup_error = str(cleanup_error)[:10_000]
            else:
                current.cleanup_required = False
            if current.completion_reason == "aborted":
                stage = "cleanup_failed" if cleanup_error else "aborted"
                final_status = ConversationStatus.stopped.value
            elif evaluation_error is not None or current.error:
                stage = "cleanup_failed" if cleanup_error else "failed"
                final_status = ConversationStatus.failed.value
            else:
                stage = "cleanup_failed" if cleanup_error else "completed"
                final_status = (
                    current.final_conversation_status or ConversationStatus.completed.value
                )
            current.stage = stage
            current.updated_at = now
            current.ended_at = now
            conversation.status = final_status
            conversation.ended_at = now
            conversation.updated_at = now
            conversation.worker_id = None
            conversation.lease_expires_at = None
            event = ConversationEvent(
                conversation_id=conversation_id,
                event_type=f"conversation_{final_status}",
                payload={
                    "turn_count": conversation.turn_count,
                    "benchmark_stage": stage,
                },
            )
            session.add(event)
            await session.flush()
            event_id = str(event.id)
        await self._realtime.publish(
            conversation_id,
            f"conversation_{final_status}",
            {
                "status": final_status,
                "benchmark_stage": stage,
                "durable_event_id": event_id,
            },
        )
        self.release(conversation_id)

    def release(self, conversation_id: uuid.UUID) -> None:
        if conversation_id in self._held:
            self._held.remove(conversation_id)
            if self._vm_lock.locked():
                self._vm_lock.release()
