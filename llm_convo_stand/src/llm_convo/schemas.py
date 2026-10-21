import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from llm_convo.context import estimate_tokens


class StreamMode(StrEnum):
    auto = "auto"
    always = "always"
    never = "never"


class GenerationParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    temperature: float = Field(default=0.7, ge=0, le=2)
    max_tokens: int = Field(default=1024, ge=1)
    top_p: float = Field(default=1.0, gt=0, le=1)
    stop: str | list[str] | None = None
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    seed: int | None = None

    def to_openai_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)

    system_prompt: str = Field(min_length=1, max_length=100_000)
    model: str = Field(default="Qwen/Qwen3.5-4B", min_length=1)
    base_url: HttpUrl = "http://localhost:8004/v1"
    api_key_env: str | None = "OPENAI_API_KEY"
    parameters: GenerationParameters = Field(default_factory=GenerationParameters)
    extra_body: dict[str, Any] = Field(
        default_factory=dict,
        description="Provider-specific OpenAI-compatible request fields.",
    )
    stream: StreamMode = StreamMode.auto
    request_timeout_seconds: float = Field(default=120, gt=0, le=900)
    max_retries: int = Field(default=3, ge=0, le=10)
    context_size: int | None = Field(default=None, ge=1024)
    compression_threshold: float | None = Field(default=None, ge=0.5, le=0.98)
    retain_recent_messages: int | None = Field(default=None, ge=2, le=100)

    @field_validator("api_key_env")
    @classmethod
    def validate_env_name(cls, value: str | None) -> str | None:
        if value and not value.replace("_", "A").isalnum():
            raise ValueError("api_key_env must contain only letters, digits, and underscores")
        return value

    @field_validator("extra_body")
    @classmethod
    def protect_core_request_fields(cls, value: dict[str, Any]) -> dict[str, Any]:
        reserved = {
            "model",
            "messages",
            "stream",
            "stream_options",
            "temperature",
            "max_tokens",
            "top_p",
            "stop",
            "frequency_penalty",
            "presence_penalty",
            "seed",
            "tools",
            "tool_choice",
        }
        overlap = reserved.intersection(value)
        if overlap:
            raise ValueError(f"extra_body cannot override: {', '.join(sorted(overlap))}")
        return value

    def persistent_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class StartConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_prompt: str = Field(min_length=1, max_length=500_000)
    agent_a: AgentConfig
    agent_b: AgentConfig
    summarizer: AgentConfig | None = None
    context_size: int = Field(default=32768, ge=1024)
    compression_threshold: float = Field(default=0.9, ge=0.5, le=0.98)
    retain_recent_messages: int = Field(
        default=4,
        ge=2,
        le=100,
        description="Unsummarized recent messages kept in the working context.",
    )
    timeout_seconds: int = Field(default=1800, ge=1, le=86_400)

    @model_validator(mode="after")
    def validate_initial_prompt_budget(self) -> "StartConversationRequest":
        for name, agent in (("agent_a", self.agent_a), ("agent_b", self.agent_b)):
            context_size = agent.context_size or self.context_size
            compression_threshold = agent.compression_threshold or self.compression_threshold
            initial_tokens = (
                estimate_tokens(self.task_prompt)
                + estimate_tokens(agent.system_prompt)
                + agent.parameters.max_tokens
                + 64
            )
            if initial_tokens >= int(context_size * compression_threshold):
                raise ValueError(
                    f"{name} task/system prompt plus max_tokens does not fit the initial "
                    "context budget"
                )
        return self

    def persistent_config(self) -> dict[str, Any]:
        return {
            "agent_a": self.agent_a.persistent_dict(),
            "agent_b": self.agent_b.persistent_dict(),
            "summarizer": self.summarizer.persistent_dict() if self.summarizer else None,
        }


class ConversationCreated(BaseModel):
    conversation_id: uuid.UUID
    status: str
    deduplicated: bool = False


class ConversationView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    task_prompt: str
    config: dict[str, Any]
    context_size: int
    compression_threshold: float
    retain_recent_messages: int
    timeout_seconds: int
    turn_count: int
    next_speaker: str
    working_summary: str | None
    summary_through_sequence: int
    created_at: datetime
    started_at: datetime | None
    deadline_at: datetime | None
    stop_requested_at: datetime | None
    ended_at: datetime | None
    updated_at: datetime
    error: str | None
    langfuse_trace_id: str | None = None
    langfuse_url: str | None = None


class ConversationSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    task_prompt: str
    turn_count: int
    next_speaker: str
    created_at: datetime
    updated_at: datetime
    ended_at: datetime | None
    error: str | None
    agent_a_model: str
    agent_b_model: str


class ConversationPage(BaseModel):
    items: list[ConversationSummary]
    total: int
    limit: int
    offset: int


class MessageView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sequence: int
    speaker: str
    content: str
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    estimated_prompt_tokens: int
    latency_ms: int
    first_token_ms: int | None
    streamed: bool
    finish_reason: str | None
    attempts: int
    response_metadata: dict[str, Any]
    created_at: datetime


class MessagePage(BaseModel):
    items: list[MessageView]
    next_after_sequence: int | None


class EventView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


class SnapshotView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    version: int
    summary: str
    through_sequence: int
    source_from_sequence: int
    source_to_sequence: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    created_at: datetime


class LangfuseView(BaseModel):
    enabled: bool
    trace_id: str | None
    url: str | None
    observations: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


class EndConversationResponse(BaseModel):
    conversation_id: uuid.UUID
    status: str


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    database: Literal["ok", "error"]
    langfuse: Literal["ok", "error", "configured", "disabled"]


class VmScenarioSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    title: str
    category: str
    mode: Literal["agent", "escape"]


class VmScenario(VmScenarioSummary):
    prompt: str
    checks: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)


class StartBenchmarkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    agent_a: AgentConfig
    agent_b: AgentConfig
    summarizer: AgentConfig | None = None
    context_size: int = Field(default=32768, ge=1024)
    compression_threshold: float = Field(default=0.9, ge=0.5, le=0.98)
    retain_recent_messages: int = Field(default=4, ge=2, le=100)
    timeout_seconds: int = Field(default=3600, ge=1, le=86_400)
    max_tool_calls_per_b_turn: int = Field(default=5, ge=1, le=100)
    max_rounds: int = Field(default=40, ge=1, le=200)

    def conversation_request(self, task_prompt: str) -> StartConversationRequest:
        return StartConversationRequest(
            task_prompt=task_prompt,
            agent_a=self.agent_a,
            agent_b=self.agent_b,
            summarizer=self.summarizer,
            context_size=self.context_size,
            compression_threshold=self.compression_threshold,
            retain_recent_messages=self.retain_recent_messages,
            timeout_seconds=self.timeout_seconds,
        )


class ToolExecutionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    message_sequence: int
    ordinal: int
    tool_call_id: str
    name: str
    arguments: dict[str, Any]
    status: str
    result: dict[str, Any] | None
    error: str | None
    created_at: datetime
    completed_at: datetime | None


class BenchmarkRunView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    conversation_id: uuid.UUID
    scenario_id: str
    scenario_mode: Literal["agent", "escape"]
    scenario: dict[str, Any]
    stage: str
    max_tool_calls_per_b_turn: int
    max_rounds: int
    completion_reason: str | None
    cleanup_required: bool
    verdict: dict[str, Any] | None
    submit_result: dict[str, Any] | None
    error: str | None
    cleanup_error: str | None
    created_at: datetime
    updated_at: datetime
    ended_at: datetime | None
    tool_executions: list[ToolExecutionView] = Field(default_factory=list)


class VmMasterHealthResponse(BaseModel):
    configured: bool
    status: Literal["ok", "error", "disabled"]
    error: str | None = None
