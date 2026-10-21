import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from llm_convo.models import (
    TERMINAL_STATUSES,
    ContextSnapshot,
    Conversation,
    ConversationEvent,
    ConversationStatus,
    Message,
    utc_now,
)
from llm_convo.schemas import StartConversationRequest


class IdempotencyConflictError(RuntimeError):
    pass


@dataclass(slots=True)
class CreateResult:
    conversation: Conversation
    deduplicated: bool


def request_fingerprint(request: StartConversationRequest) -> str:
    canonical = request.model_dump_json(exclude_none=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ConversationService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def create(self, request: StartConversationRequest, idempotency_key: str) -> CreateResult:
        fingerprint = request_fingerprint(request)
        async with self._sessions() as session:
            existing = await self._by_idempotency_key(session, idempotency_key)
            if existing:
                self._verify_fingerprint(existing, fingerprint)
                return CreateResult(existing, True)

            conversation = Conversation(
                config=request.persistent_config(),
                context_size=request.context_size,
                compression_threshold=request.compression_threshold,
                retain_recent_messages=request.retain_recent_messages,
                timeout_seconds=request.timeout_seconds,
                idempotency_key=idempotency_key,
                request_hash=fingerprint,
            )
            session.add(conversation)
            session.add(
                ConversationEvent(
                    conversation=conversation,
                    event_type="conversation_queued",
                    payload={"idempotency_key": idempotency_key},
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await self._by_idempotency_key(session, idempotency_key)
                if existing is None:
                    raise
                self._verify_fingerprint(existing, fingerprint)
                return CreateResult(existing, True)
            await session.refresh(conversation)
            return CreateResult(conversation, False)

    @staticmethod
    async def _by_idempotency_key(
        session: AsyncSession, idempotency_key: str
    ) -> Conversation | None:
        return await session.scalar(
            select(Conversation).where(Conversation.idempotency_key == idempotency_key)
        )

    @staticmethod
    def _verify_fingerprint(conversation: Conversation, fingerprint: str) -> None:
        if conversation.request_hash != fingerprint:
            raise IdempotencyConflictError(
                "Idempotency-Key was already used with a different request body"
            )

    async def get(self, conversation_id: uuid.UUID) -> Conversation | None:
        async with self._sessions() as session:
            return await session.get(Conversation, conversation_id)

    async def list_conversations(
        self, *, status: ConversationStatus | None, limit: int, offset: int
    ) -> tuple[list[Conversation], int]:
        filters = []
        if status is not None:
            filters.append(Conversation.status == status.value)
        async with self._sessions() as session:
            total = await session.scalar(select(func.count(Conversation.id)).where(*filters))
            result = await session.scalars(
                select(Conversation)
                .where(*filters)
                .order_by(Conversation.created_at.desc(), Conversation.id.desc())
                .offset(offset)
                .limit(limit)
            )
            return list(result), int(total or 0)

    async def request_stop(self, conversation_id: uuid.UUID) -> Conversation | None:
        async with self._sessions() as session, session.begin():
            conversation = await session.scalar(
                select(Conversation).where(Conversation.id == conversation_id).with_for_update()
            )
            if conversation is None:
                return None
            status = ConversationStatus(conversation.status)
            if status not in TERMINAL_STATUSES:
                now = utc_now()
                conversation.status = ConversationStatus.stopped.value
                conversation.stop_requested_at = now
                conversation.ended_at = now
                conversation.updated_at = now
                conversation.worker_id = None
                conversation.lease_expires_at = None
                session.add(
                    ConversationEvent(
                        conversation_id=conversation.id,
                        event_type="conversation_stopped",
                        payload={"turn_count": conversation.turn_count},
                    )
                )
            return conversation

    async def messages(
        self, conversation_id: uuid.UUID, after_sequence: int, limit: int
    ) -> list[Message] | None:
        async with self._sessions() as session:
            if await session.get(Conversation, conversation_id) is None:
                return None
            result = await session.scalars(
                select(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.sequence > after_sequence,
                )
                .order_by(Message.sequence)
                .limit(limit)
            )
            return list(result)

    async def events(
        self, conversation_id: uuid.UUID, limit: int
    ) -> list[ConversationEvent] | None:
        async with self._sessions() as session:
            if await session.get(Conversation, conversation_id) is None:
                return None
            result = await session.scalars(
                select(ConversationEvent)
                .where(ConversationEvent.conversation_id == conversation_id)
                .order_by(ConversationEvent.created_at)
                .limit(limit)
            )
            return list(result)

    async def recent_events(
        self, conversation_id: uuid.UUID, limit: int = 1000
    ) -> list[ConversationEvent] | None:
        async with self._sessions() as session:
            if await session.get(Conversation, conversation_id) is None:
                return None
            result = await session.scalars(
                select(ConversationEvent)
                .where(ConversationEvent.conversation_id == conversation_id)
                .order_by(ConversationEvent.created_at.desc(), ConversationEvent.id.desc())
                .limit(limit)
            )
            return list(reversed(list(result)))

    async def snapshots(self, conversation_id: uuid.UUID) -> list[ContextSnapshot] | None:
        async with self._sessions() as session:
            if await session.get(Conversation, conversation_id) is None:
                return None
            result = await session.scalars(
                select(ContextSnapshot)
                .where(ContextSnapshot.conversation_id == conversation_id)
                .order_by(ContextSnapshot.version)
            )
            return list(result)
