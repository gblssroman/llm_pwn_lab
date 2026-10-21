import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from llm_convo.db import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Normalize timestamps from drivers that discard timezone metadata (notably SQLite)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class ConversationStatus(StrEnum):
    queued = "queued"
    running = "running"
    stopping = "stopping"
    stopped = "stopped"
    timed_out = "timed_out"
    completed = "completed"
    failed = "failed"
    evaluating = "evaluating"
    cleaning_up = "cleaning_up"


TERMINAL_STATUSES = {
    ConversationStatus.stopped,
    ConversationStatus.timed_out,
    ConversationStatus.completed,
    ConversationStatus.failed,
}


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(
        String(32), default=ConversationStatus.queued.value, index=True
    )
    task_prompt: Mapped[str] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    context_size: Mapped[int] = mapped_column(Integer)
    compression_threshold: Mapped[float]
    retain_recent_messages: Mapped[int] = mapped_column(Integer)
    timeout_seconds: Mapped[int] = mapped_column(Integer)

    turn_count: Mapped[int] = mapped_column(Integer, default=0)
    next_speaker: Mapped[str] = mapped_column(String(16), default="agent_a")
    working_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_through_sequence: Mapped[int] = mapped_column(Integer, default=0)

    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stop_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )
    snapshots: Mapped[list["ContextSnapshot"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )
    events: Mapped[list["ConversationEvent"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence", name="uq_message_conversation_sequence"),
        Index("ix_messages_conversation_sequence", "conversation_id", "sequence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    speaker: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(255))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_prompt_tokens: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer)
    first_token_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    streamed: Mapped[bool]
    finish_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    response_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class ContextSnapshot(Base):
    __tablename__ = "context_snapshots"
    __table_args__ = (
        UniqueConstraint("conversation_id", "version", name="uq_snapshot_conversation_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(Text)
    through_sequence: Mapped[int] = mapped_column(Integer)
    source_from_sequence: Mapped[int] = mapped_column(Integer)
    source_to_sequence: Mapped[int] = mapped_column(Integer)
    estimated_input_tokens: Mapped[int] = mapped_column(Integer)
    estimated_output_tokens: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    conversation: Mapped[Conversation] = relationship(back_populates="snapshots")


class ConversationEvent(Base):
    __tablename__ = "conversation_events"
    __table_args__ = (Index("ix_events_conversation_created", "conversation_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    conversation: Mapped[Conversation] = relationship(back_populates="events")


class BenchmarkRun(Base):
    __tablename__ = "benchmark_runs"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    scenario_id: Mapped[str] = mapped_column(String(64), index=True)
    scenario_mode: Mapped[str] = mapped_column(String(16))
    scenario: Mapped[dict[str, Any]] = mapped_column(JSON)
    stage: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    max_tool_calls_per_b_turn: Mapped[int] = mapped_column(Integer)
    max_rounds: Mapped[int] = mapped_column(Integer)
    cleanup_required: Mapped[bool] = mapped_column(Boolean, default=False)
    completion_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    final_conversation_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    flag_candidate: Mapped[str | None] = mapped_column(Text, nullable=True)
    verdict: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    submit_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cleanup_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ToolExecution(Base):
    __tablename__ = "tool_executions"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id", "message_sequence", "tool_call_id", name="uq_tool_call"
        ),
        Index("ix_tool_execution_conversation_created", "conversation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    message_sequence: Mapped[int] = mapped_column(Integer)
    ordinal: Mapped[int] = mapped_column(Integer)
    tool_call_id: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(64))
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="running")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
