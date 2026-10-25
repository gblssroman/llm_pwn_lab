import asyncio
import logging
import uuid
from contextlib import suppress
from datetime import timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from llm_convo.benchmark import BenchmarkCoordinator
from llm_convo.context import (
    WorkingContext,
    build_compression_messages,
    build_working_context,
    estimate_tokens,
    needs_compression,
)
from llm_convo.llm import LLMResult, OpenAICompatibleClient, ToolCall
from llm_convo.logging import log_event
from llm_convo.loop_detection import loop_reason
from llm_convo.models import (
    ContextSnapshot,
    Conversation,
    ConversationEvent,
    ConversationStatus,
    Message,
    as_utc,
    utc_now,
)
from llm_convo.react import AgentBReactGraph
from llm_convo.realtime import RealtimeBroker
from llm_convo.schemas import AgentConfig
from llm_convo.tracing import Tracing

logger = logging.getLogger(__name__)


class ContextLimitError(RuntimeError):
    pass


class ConversationRunner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        llm: OpenAICompatibleClient,
        tracing: Tracing,
        realtime: RealtimeBroker | None = None,
        benchmark: BenchmarkCoordinator | None = None,
        *,
        poll_seconds: float = 2.0,
        lease_seconds: int = 180,
        heartbeat_seconds: int = 30,
        max_concurrent_conversations: int = 10,
    ) -> None:
        self._sessions = session_factory
        self._llm = llm
        self._tracing = tracing
        self._realtime = realtime or RealtimeBroker()
        self._benchmark = benchmark
        self._poll_seconds = poll_seconds
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = min(heartbeat_seconds, max(5, lease_seconds // 3))
        self._slots = asyncio.Semaphore(max_concurrent_conversations)
        self._worker_id = uuid.uuid4().hex
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._poll_task: asyncio.Task[None] | None = None
        self._closing = False

    async def start(self) -> None:
        self._closing = False
        self._poll_task = asyncio.create_task(self._poll_loop(), name="conversation-poller")

    def enqueue(self, conversation_id: uuid.UUID) -> None:
        if self._closing or conversation_id in self._tasks:
            return
        task = asyncio.create_task(
            self._run_guarded(conversation_id), name=f"conversation-{conversation_id}"
        )
        self._tasks[conversation_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(conversation_id, None))

    def cancel(self, conversation_id: uuid.UUID) -> bool:
        task = self._tasks.get(conversation_id)
        if task and not task.done():
            task.cancel()
            return True
        return False

    async def shutdown(self, grace_seconds: float = 10.0) -> None:
        self._closing = True
        if self._poll_task:
            self._poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._poll_task
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=grace_seconds)
            for task in pending:
                task.cancel()
            if done:
                await asyncio.gather(*done, return_exceptions=True)

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self._discover_work()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event(logger, logging.ERROR, "worker_poll_failed", error=str(exc))
            await asyncio.sleep(self._poll_seconds)

    async def _discover_work(self) -> None:
        now = utc_now()
        async with self._sessions() as session:
            result = await session.scalars(
                select(Conversation.id)
                .where(
                    or_(
                        Conversation.status == ConversationStatus.queued.value,
                        (
                            (Conversation.status == ConversationStatus.running.value)
                            & or_(
                                Conversation.lease_expires_at.is_(None),
                                Conversation.lease_expires_at < now,
                            )
                        ),
                    )
                )
                .limit(100)
            )
            for conversation_id in result:
                self.enqueue(conversation_id)
        if self._benchmark:
            for conversation_id in await self._benchmark.pending_finalization_ids():
                self.enqueue(conversation_id)

    async def _claim(self, conversation_id: uuid.UUID) -> Conversation | None:
        now = utc_now()
        async with self._sessions() as session, session.begin():
            conversation = await session.scalar(
                select(Conversation)
                .where(Conversation.id == conversation_id)
                .with_for_update(skip_locked=True)
            )
            if conversation is None:
                return None
            if conversation.status not in {
                ConversationStatus.queued.value,
                ConversationStatus.running.value,
            }:
                return None
            if (
                conversation.status == ConversationStatus.running.value
                and conversation.worker_id != self._worker_id
                and conversation.lease_expires_at
                and as_utc(conversation.lease_expires_at) >= now
            ):
                return None
            conversation.status = ConversationStatus.running.value
            conversation.worker_id = self._worker_id
            conversation.lease_expires_at = now + timedelta(seconds=self._lease_seconds)
            conversation.started_at = conversation.started_at or now
            conversation.deadline_at = conversation.deadline_at or (
                conversation.started_at + timedelta(seconds=conversation.timeout_seconds)
            )
            conversation.updated_at = now
            session.add(
                ConversationEvent(
                    conversation_id=conversation.id,
                    event_type="conversation_started",
                    payload={"worker_id": self._worker_id},
                )
            )
            return conversation

    async def _run_guarded(self, conversation_id: uuid.UUID) -> None:
        try:
            async with self._slots:
                await self._run_claimed(conversation_id)
        finally:
            # A task can be cancelled before _run_claimed reaches its own cleanup block
            # (for example, aborting a queued run). Recovery also reaches this path when
            # _claim rejects an evaluating/cleaning_up conversation after a restart.
            if self._benchmark and await self._benchmark.should_finalize(conversation_id):
                try:
                    await self._benchmark.finalize(conversation_id)
                except Exception as exc:
                    log_event(
                        logger,
                        logging.ERROR,
                        "benchmark_finalization_failed",
                        conversation_id=str(conversation_id),
                        error=str(exc),
                    )

    async def _run_claimed(self, conversation_id: uuid.UUID) -> None:
        conversation = await self._claim(conversation_id)
        if conversation is None:
            return
        is_benchmark = bool(self._benchmark and await self._benchmark.is_benchmark(conversation_id))
        await self._realtime.publish(
            conversation_id,
            "conversation_started",
            {"status": ConversationStatus.running.value, "started_at": conversation.started_at},
        )
        heartbeat = asyncio.create_task(
            self._heartbeat(conversation_id, asyncio.current_task()),
            name=f"heartbeat-{conversation_id}",
        )
        log_event(
            logger,
            logging.INFO,
            "conversation_started",
            conversation_id=str(conversation_id),
            worker_id=self._worker_id,
        )
        try:
            with self._tracing.conversation(str(conversation_id)) as root_observation:
                with self._tracing.attributes(str(conversation_id)):
                    if is_benchmark and self._benchmark:
                        await self._benchmark.prepare(conversation_id)
                        conversation, _ = await self._load(conversation_id)
                    remaining = max(
                        0.0,
                        (as_utc(conversation.deadline_at) - utc_now()).total_seconds(),
                    )
                    try:
                        async with asyncio.timeout(remaining):
                            await self._run_loop(conversation_id, is_benchmark=is_benchmark)
                    except TimeoutError:
                        if is_benchmark and self._benchmark:
                            await self._benchmark.begin_evaluation(
                                conversation_id,
                                reason="timeout",
                                final_status=ConversationStatus.timed_out,
                            )
                        else:
                            await self._finish(conversation_id, ConversationStatus.timed_out)
                self._tracing.finish_conversation(
                    root_observation, await self._get_status(conversation_id)
                )
        except asyncio.CancelledError:
            await self._finish_cancelled(conversation_id)
        except Exception as exc:
            if is_benchmark and self._benchmark:
                if not await self._benchmark.should_finalize(conversation_id):
                    await self._benchmark.mark_failure(conversation_id, exc)
            else:
                await self._finish_failed(conversation_id, exc)
            log_event(
                logger,
                logging.ERROR,
                "conversation_failed",
                conversation_id=str(conversation_id),
                error=str(exc),
            )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            if is_benchmark and self._benchmark:
                if await self._benchmark.should_finalize(conversation_id):
                    await self._benchmark.finalize(conversation_id)
                else:
                    self._benchmark.release(conversation_id)
            await asyncio.to_thread(self._tracing.flush)

    async def _run_loop(self, conversation_id: uuid.UUID, *, is_benchmark: bool = False) -> None:
        while True:
            state, messages = await self._load(conversation_id)
            if state.status == ConversationStatus.stopping.value:
                await self._finish(conversation_id, ConversationStatus.stopped)
                return
            if state.status != ConversationStatus.running.value:
                return
            if state.deadline_at and utc_now() >= as_utc(state.deadline_at):
                if is_benchmark and self._benchmark:
                    await self._benchmark.begin_evaluation(
                        conversation_id,
                        reason="timeout",
                        final_status=ConversationStatus.timed_out,
                    )
                else:
                    await self._finish(conversation_id, ConversationStatus.timed_out)
                return

            agent = AgentConfig.model_validate(state.config[state.next_speaker])
            state, messages, prompt = await self._prepare_context(state, messages, agent)
            if state.status != ConversationStatus.running.value:
                continue
            if state.deadline_at and utc_now() >= as_utc(state.deadline_at):
                continue
            result = await self._generate_turn(state, agent, prompt.messages)
            saved = await self._save_turn(
                state,
                result=result,
                estimated_prompt_tokens=prompt.estimated_tokens,
            )
            if not saved:
                return
            if is_benchmark and self._benchmark:
                should_stop = await self._benchmark.after_turn(
                    conversation_id,
                    speaker=state.next_speaker,
                    content=result.content,
                    turn_count=state.turn_count + 1,
                )
                if should_stop:
                    return
            reason = await self._loop_reason(conversation_id)
            if reason:
                if is_benchmark and self._benchmark:
                    await self._benchmark.begin_evaluation(conversation_id, reason=reason)
                else:
                    await self._finish(conversation_id, ConversationStatus.completed, reason=reason)
                return

    async def _loop_reason(self, conversation_id: uuid.UUID) -> str | None:
        # Read durable recent turns independently of context compression and restarts.
        async with self._sessions() as session:
            result = await session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.sequence.desc())
                .limit(6)
            )
            return loop_reason(list(reversed(list(result))))

    async def _load(self, conversation_id: uuid.UUID) -> tuple[Conversation, list[Message]]:
        async with self._sessions() as session:
            conversation = await session.get(Conversation, conversation_id)
            if conversation is None:
                raise RuntimeError("Conversation disappeared while running")
            result = await session.scalars(
                select(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.sequence > conversation.summary_through_sequence,
                )
                .order_by(Message.sequence)
            )
            return conversation, list(result)

    async def _prepare_context(
        self, state: Conversation, messages: list[Message], agent: AgentConfig
    ) -> tuple[Conversation, list[Message], WorkingContext]:
        context_size = agent.context_size or state.context_size
        compression_threshold = agent.compression_threshold or state.compression_threshold
        observations = (
            await self._benchmark.terminal_observations(state.id) if self._benchmark else {}
        )
        for _ in range(3):
            prompt = build_working_context(
                system_prompt=agent.system_prompt,
                speaker=state.next_speaker,
                summary=state.working_summary,
                messages=messages,
                tool_observations=observations,
            )
            if not needs_compression(
                estimated_prompt_tokens=prompt.estimated_tokens,
                max_output_tokens=agent.parameters.max_tokens,
                context_size=context_size,
                threshold=compression_threshold,
            ):
                return state, messages, prompt
            await self._compress(state, messages, agent)
            state, messages = await self._load(state.id)
        raise ContextLimitError("Working context is still too large after three compression passes")

    async def _compress(
        self, state: Conversation, messages: list[Message], agent: AgentConfig
    ) -> None:
        retain = agent.retain_recent_messages or state.retain_recent_messages
        eligible = messages[:-retain] if len(messages) > retain else []
        if not eligible and not state.working_summary:
            raise ContextLimitError(
                "System prompt and retained messages exceed the context budget; "
                "there is no older dialogue available to summarize"
            )

        summarizer_data = state.config.get("summarizer") or state.config["agent_a"]
        summarizer = AgentConfig.model_validate(summarizer_data)
        context_size = agent.context_size or state.context_size
        target_tokens = max(256, min(2048, int(context_size * 0.1)))
        compression_messages = build_compression_messages(
            previous_summary=state.working_summary,
            messages=eligible,
            target_tokens=target_tokens,
        )
        source_from = eligible[0].sequence if eligible else state.summary_through_sequence
        source_to = eligible[-1].sequence if eligible else state.summary_through_sequence
        result = await self._traced_completion(
            name="context-compression",
            config=summarizer,
            messages=compression_messages,
            metadata={
                "conversation_id": str(state.id),
                "source_from_sequence": source_from,
                "source_to_sequence": source_to,
            },
            parameter_overrides={
                "temperature": 0,
                "max_tokens": min(target_tokens, summarizer.parameters.max_tokens),
            },
        )
        await self._save_summary(
            state=state,
            result=result,
            source_from=source_from,
            source_to=source_to,
            estimated_input_tokens=sum(
                estimate_tokens(item["content"]) for item in compression_messages
            ),
        )

    async def _save_summary(
        self,
        *,
        state: Conversation,
        result: LLMResult,
        source_from: int,
        source_to: int,
        estimated_input_tokens: int,
    ) -> None:
        async with self._sessions() as session, session.begin():
            conversation = await session.scalar(
                select(Conversation).where(Conversation.id == state.id).with_for_update()
            )
            if (
                conversation is None
                or conversation.status != ConversationStatus.running.value
                or conversation.worker_id != self._worker_id
            ):
                return
            if conversation.summary_through_sequence != state.summary_through_sequence:
                return
            version = (
                await session.scalar(
                    select(func.coalesce(func.max(ContextSnapshot.version), 0)).where(
                        ContextSnapshot.conversation_id == state.id
                    )
                )
            ) + 1
            conversation.working_summary = result.content
            conversation.summary_through_sequence = max(
                conversation.summary_through_sequence, source_to
            )
            conversation.updated_at = utc_now()
            session.add(
                ContextSnapshot(
                    conversation_id=state.id,
                    version=version,
                    summary=result.content,
                    through_sequence=conversation.summary_through_sequence,
                    source_from_sequence=source_from,
                    source_to_sequence=source_to,
                    estimated_input_tokens=estimated_input_tokens,
                    estimated_output_tokens=estimate_tokens(result.content),
                )
            )
            durable_event = ConversationEvent(
                conversation_id=state.id,
                event_type="context_compressed",
                payload={
                    "version": version,
                    "through_sequence": conversation.summary_through_sequence,
                    "estimated_input_tokens": estimated_input_tokens,
                    "estimated_output_tokens": estimate_tokens(result.content),
                },
            )
            session.add(durable_event)
            await session.flush()
        await self._realtime.publish(
            state.id,
            "context_compressed",
            {
                "version": version,
                "summary": result.content,
                "through_sequence": conversation.summary_through_sequence,
                "source_from_sequence": source_from,
                "source_to_sequence": source_to,
                "estimated_input_tokens": estimated_input_tokens,
                "estimated_output_tokens": estimate_tokens(result.content),
                "durable_event_id": str(durable_event.id),
            },
        )
        log_event(
            logger,
            logging.INFO,
            "context_compressed",
            conversation_id=str(state.id),
            source_from=source_from,
            source_to=source_to,
        )

    async def _generate_turn(
        self, state: Conversation, agent: AgentConfig, messages: list[dict[str, Any]]
    ) -> LLMResult:
        sequence = state.turn_count + 1
        speaker = state.next_speaker
        async with self._sessions() as session, session.begin():
            durable_event = ConversationEvent(
                conversation_id=state.id,
                event_type="turn_started",
                payload={"sequence": sequence, "speaker": speaker, "model": agent.model},
            )
            session.add(durable_event)
            await session.flush()
        await self._realtime.publish(
            state.id,
            "turn_started",
            {
                "sequence": sequence,
                "speaker": speaker,
                "model": agent.model,
                "durable_event_id": str(durable_event.id),
            },
        )

        async def on_stream_start() -> None:
            await self._realtime.publish(
                state.id,
                "turn_stream_started",
                {"sequence": sequence, "speaker": speaker},
            )

        async def on_token(content: str) -> None:
            await self._realtime.publish(
                state.id,
                "turn_delta",
                {"sequence": sequence, "speaker": speaker, "content": content},
            )

        if speaker == "agent_b" and self._benchmark:
            run = await self._benchmark.get_run(state.id)
            if run is not None:

                async def complete(
                    react_messages: list[dict[str, Any]],
                    tools: list[dict[str, Any]] | None,
                ) -> LLMResult:
                    return await self._traced_completion(
                        name="agent-b-react",
                        config=agent,
                        messages=react_messages,
                        metadata={
                            "conversation_id": str(state.id),
                            "speaker": speaker,
                            "turn": sequence,
                            "react": True,
                        },
                        tools=tools,
                        tool_choice="auto" if tools else None,
                        preserve_reasoning_details=True,
                    )

                async def execute_terminal(call: ToolCall, ordinal: int) -> dict[str, Any]:
                    return await self._benchmark.execute_terminal(state.id, sequence, call, ordinal)

                async def observe_model(result: LLMResult, step: int) -> None:
                    await self._benchmark.record_agent_step(
                        state.id,
                        sequence,
                        result,
                        step,
                    )

                result = await AgentBReactGraph(
                    complete,
                    execute_terminal,
                    observe_model,
                ).run(messages, max_tool_calls=run.max_tool_calls_per_b_turn)
                await on_stream_start()
                if result.content:
                    await on_token(result.content)
                return result

        return await self._traced_completion(
            name="conversation-turn",
            config=agent,
            messages=messages,
            metadata={
                "conversation_id": str(state.id),
                "speaker": state.next_speaker,
                "turn": state.turn_count + 1,
            },
            on_stream_start=on_stream_start,
            on_token=on_token,
        )

    async def _traced_completion(
        self,
        *,
        name: str,
        config: AgentConfig,
        messages: list[dict[str, Any]],
        metadata: dict[str, Any],
        parameter_overrides: dict[str, Any] | None = None,
        on_stream_start: Any | None = None,
        on_token: Any | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        preserve_reasoning_details: bool = False,
    ) -> LLMResult:
        parameters = config.parameters.to_openai_payload()
        if parameter_overrides:
            parameters.update(parameter_overrides)
        with self._tracing.generation(
            name=name,
            model=config.model,
            messages=[
                {key: value for key, value in message.items() if key != "reasoning_details"}
                for message in messages
            ],
            parameters=parameters,
            metadata=metadata,
        ) as observation:
            try:
                completion_kwargs: dict[str, Any] = {
                    "config": config,
                    "messages": messages,
                    "parameter_overrides": parameter_overrides,
                    "on_stream_start": on_stream_start,
                    "on_token": on_token,
                }
                if tools is not None:
                    completion_kwargs.update(tools=tools, tool_choice=tool_choice)
                if preserve_reasoning_details:
                    completion_kwargs["preserve_reasoning_details"] = True
                result = await self._llm.complete(**completion_kwargs)
            except Exception as exc:
                self._tracing.fail(observation, exc)
                raise
            self._tracing.finish_generation(observation, result=result)
            return result

    async def _save_turn(
        self, state: Conversation, *, result: LLMResult, estimated_prompt_tokens: int
    ) -> bool:
        now = utc_now()
        async with self._sessions() as session, session.begin():
            conversation = await session.scalar(
                select(Conversation).where(Conversation.id == state.id).with_for_update()
            )
            if (
                conversation is None
                or conversation.status != ConversationStatus.running.value
                or conversation.worker_id != self._worker_id
            ):
                return False
            if conversation.turn_count != state.turn_count:
                return False
            sequence = conversation.turn_count + 1
            message = Message(
                conversation_id=conversation.id,
                sequence=sequence,
                speaker=conversation.next_speaker,
                content=result.content,
                model=state.config[conversation.next_speaker]["model"],
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                estimated_prompt_tokens=estimated_prompt_tokens,
                latency_ms=result.latency_ms,
                first_token_ms=result.first_token_ms,
                streamed=result.streamed,
                finish_reason=result.finish_reason,
                attempts=result.attempts,
                response_metadata=result.metadata,
            )
            session.add(message)
            speaker = conversation.next_speaker
            conversation.turn_count = sequence
            conversation.next_speaker = "agent_b" if speaker == "agent_a" else "agent_a"
            conversation.updated_at = now
            conversation.lease_expires_at = now + timedelta(seconds=self._lease_seconds)
            durable_event = ConversationEvent(
                conversation_id=conversation.id,
                event_type="turn_completed",
                payload={
                    "sequence": sequence,
                    "speaker": speaker,
                    "latency_ms": result.latency_ms,
                    "streamed": result.streamed,
                },
            )
            session.add(durable_event)
            await session.flush()
        await self._realtime.publish(
            state.id,
            "turn_completed",
            {
                "message": {
                    "id": str(message.id),
                    "sequence": message.sequence,
                    "speaker": message.speaker,
                    "content": message.content,
                    "model": message.model,
                    "prompt_tokens": message.prompt_tokens,
                    "completion_tokens": message.completion_tokens,
                    "estimated_prompt_tokens": message.estimated_prompt_tokens,
                    "latency_ms": message.latency_ms,
                    "first_token_ms": message.first_token_ms,
                    "streamed": message.streamed,
                    "finish_reason": message.finish_reason,
                    "attempts": message.attempts,
                    "response_metadata": message.response_metadata,
                    "created_at": message.created_at.isoformat(),
                },
                "next_speaker": conversation.next_speaker,
                "durable_event_id": str(durable_event.id),
            },
        )
        log_event(
            logger,
            logging.INFO,
            "turn_completed",
            conversation_id=str(state.id),
            sequence=state.turn_count + 1,
            speaker=state.next_speaker,
            latency_ms=result.latency_ms,
            streamed=result.streamed,
        )
        return True

    async def _heartbeat(
        self, conversation_id: uuid.UUID, owner_task: asyncio.Task[Any] | None
    ) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            now = utc_now()
            try:
                async with self._sessions() as session, session.begin():
                    ownership = (
                        await session.execute(
                            select(Conversation.status, Conversation.worker_id).where(
                                Conversation.id == conversation_id
                            )
                        )
                    ).one_or_none()
                    if (
                        ownership is None
                        or ownership.status != ConversationStatus.running.value
                        or ownership.worker_id != self._worker_id
                    ):
                        if owner_task:
                            owner_task.cancel()
                        return
                    await session.execute(
                        update(Conversation)
                        .where(
                            Conversation.id == conversation_id,
                            Conversation.status == ConversationStatus.running.value,
                            Conversation.worker_id == self._worker_id,
                        )
                        .values(
                            lease_expires_at=now + timedelta(seconds=self._lease_seconds),
                            updated_at=now,
                        )
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event(
                    logger,
                    logging.ERROR,
                    "conversation_heartbeat_failed",
                    conversation_id=str(conversation_id),
                    error=str(exc),
                )
                if owner_task:
                    owner_task.cancel()
                return

    async def _finish(
        self, conversation_id: uuid.UUID, status: ConversationStatus, *, reason: str | None = None
    ) -> None:
        now = utc_now()
        async with self._sessions() as session, session.begin():
            conversation = await session.scalar(
                select(Conversation).where(Conversation.id == conversation_id).with_for_update()
            )
            if conversation is None:
                return
            current = ConversationStatus(conversation.status)
            if current in {
                ConversationStatus.stopped,
                ConversationStatus.timed_out,
                ConversationStatus.completed,
                ConversationStatus.failed,
            }:
                return
            if status is ConversationStatus.timed_out and current is not ConversationStatus.running:
                return
            conversation.status = status.value
            conversation.ended_at = now
            conversation.updated_at = now
            conversation.lease_expires_at = None
            conversation.worker_id = None
            session.add(
                ConversationEvent(
                    conversation_id=conversation_id,
                    event_type=f"conversation_{status.value}",
                    payload={"turn_count": conversation.turn_count, "reason": reason},
                )
            )
        await self._realtime.publish(
            conversation_id,
            f"conversation_{status.value}",
            {"status": status.value, "turn_count": conversation.turn_count, "reason": reason},
        )
        log_event(
            logger,
            logging.INFO,
            f"conversation_{status.value}",
            conversation_id=str(conversation_id),
        )

    async def _finish_cancelled(self, conversation_id: uuid.UUID) -> None:
        async with self._sessions() as session:
            status = await session.scalar(
                select(Conversation.status).where(Conversation.id == conversation_id)
            )
        if status in {
            ConversationStatus.evaluating.value,
            ConversationStatus.cleaning_up.value,
        }:
            return
        if status == ConversationStatus.stopping.value:
            await self._finish(conversation_id, ConversationStatus.stopped)
            return
        # Process shutdown or local task cancellation: make the work reclaimable.
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(Conversation)
                .where(
                    Conversation.id == conversation_id,
                    Conversation.status == ConversationStatus.running.value,
                    Conversation.worker_id == self._worker_id,
                )
                .values(
                    status=ConversationStatus.queued.value,
                    worker_id=None,
                    lease_expires_at=None,
                    updated_at=utc_now(),
                )
            )
        await self._realtime.publish(
            conversation_id,
            "conversation_queued",
            {"status": ConversationStatus.queued.value, "reason": "worker_shutdown"},
        )

    async def _get_status(self, conversation_id: uuid.UUID) -> str | None:
        async with self._sessions() as session:
            return await session.scalar(
                select(Conversation.status).where(Conversation.id == conversation_id)
            )

    async def _finish_failed(self, conversation_id: uuid.UUID, error: Exception) -> None:
        now = utc_now()
        async with self._sessions() as session, session.begin():
            conversation = await session.scalar(
                select(Conversation).where(Conversation.id == conversation_id).with_for_update()
            )
            if (
                conversation is None
                or conversation.status != ConversationStatus.running.value
                or conversation.worker_id != self._worker_id
            ):
                return
            conversation.status = ConversationStatus.failed.value
            conversation.error = str(error)[:10_000]
            conversation.ended_at = now
            conversation.updated_at = now
            conversation.worker_id = None
            conversation.lease_expires_at = None
            session.add(
                ConversationEvent(
                    conversation_id=conversation_id,
                    event_type="conversation_failed",
                    payload={"error": str(error)[:2_000]},
                )
            )
        await self._realtime.publish(
            conversation_id,
            "conversation_failed",
            {"status": ConversationStatus.failed.value, "error": str(error)[:2_000]},
        )
