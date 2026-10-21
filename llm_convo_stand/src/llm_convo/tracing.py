import logging
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from llm_convo.logging import log_event

logger = logging.getLogger(__name__)


class _SafeContext(AbstractContextManager[Any]):
    def __init__(self, inner: AbstractContextManager[Any]) -> None:
        self._inner = inner
        self._entered = False

    def __enter__(self) -> Any:
        try:
            value = self._inner.__enter__()
            self._entered = True
            return value
        except Exception as exc:  # pragma: no cover - external SDK failure path
            log_event(logger, logging.WARNING, "langfuse_span_start_failed", error=str(exc))
            return None

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        if not self._entered:
            return False
        try:
            return bool(self._inner.__exit__(exc_type, exc_value, traceback))
        except Exception as exc:  # pragma: no cover - external SDK failure path
            log_event(logger, logging.WARNING, "langfuse_span_end_failed", error=str(exc))
            return False


class Tracing:
    """Small optional adapter around the Langfuse v4 observation API."""

    def __init__(
        self,
        *,
        public_key: str | None = None,
        secret_key: str | None = None,
        base_url: str | None = None,
        public_url: str | None = None,
        project_id: str | None = None,
        environment: str = "development",
    ) -> None:
        self._client: Any | None = None
        self._propagate: Any | None = None
        self._base_url = base_url
        self._public_url = public_url or base_url
        self._project_id = project_id
        if not (public_key and secret_key):
            return
        try:
            from langfuse import Langfuse, propagate_attributes

            self._client = Langfuse(
                public_key=public_key,
                secret_key=secret_key,
                base_url=base_url,
                environment=environment,
            )
            self._propagate = propagate_attributes
        except Exception as exc:  # pragma: no cover - depends on optional external SDK
            log_event(logger, logging.WARNING, "langfuse_initialization_failed", error=str(exc))

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def health(self) -> str:
        if not self._client or not self._base_url:
            return "disabled"
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(
                    f"{self._base_url.rstrip('/')}/api/public/health",
                    params={"failIfDatabaseUnavailable": "true"},
                )
            return "ok" if response.is_success else "error"
        except httpx.HTTPError:
            return "error"

    def trace_id(self, conversation_id: str) -> str | None:
        if not self._client:
            return None
        return str(self._client.create_trace_id(seed=conversation_id))

    def trace_url(self, conversation_id: str) -> str | None:
        trace_id = self.trace_id(conversation_id)
        if not (trace_id and self._public_url):
            return None
        base = self._public_url.rstrip("/")
        if self._project_id:
            return f"{base}/project/{self._project_id}/traces/{trace_id}"
        return base

    async def observations(
        self, conversation_id: str, *, limit: int = 100
    ) -> tuple[list[dict[str, Any]], str | None]:
        if not self._client:
            return [], None
        trace_id = self.trace_id(conversation_id)
        try:
            response = await self._client.async_api.observations.get_many(
                trace_id=trace_id,
                fields="core,basic,usage,io",
                limit=limit,
            )
            items = []
            for observation in response.data:
                if hasattr(observation, "model_dump"):
                    items.append(observation.model_dump(mode="json", exclude_none=True))
                else:  # pragma: no cover - compatibility with future SDK response types
                    items.append(dict(observation))
            return items, None
        except Exception as exc:  # pragma: no cover - external API failure path
            log_event(logger, logging.WARNING, "langfuse_query_failed", error=str(exc))
            return [], str(exc)

    def conversation(self, conversation_id: str) -> AbstractContextManager[Any]:
        if not self._client:
            return nullcontext(None)
        try:
            trace_id = self.trace_id(conversation_id)
            context = self._client.start_as_current_observation(
                trace_context={"trace_id": trace_id},
                name="llm-conversation",
                as_type="agent",
                metadata={"conversation_id": conversation_id},
            )
            return _SafeContext(context)
        except Exception as exc:  # pragma: no cover - external SDK failure path
            log_event(logger, logging.WARNING, "langfuse_span_create_failed", error=str(exc))
            return nullcontext(None)

    def attributes(self, conversation_id: str) -> AbstractContextManager[Any]:
        if not self._propagate:
            return nullcontext(None)
        try:
            return _SafeContext(
                self._propagate(
                    session_id=conversation_id,
                    trace_name="llm-conversation",
                    tags=["two-agent-conversation"],
                    metadata={"conversation_id": conversation_id},
                )
            )
        except Exception as exc:  # pragma: no cover - external SDK failure path
            log_event(logger, logging.WARNING, "langfuse_attributes_failed", error=str(exc))
            return nullcontext(None)

    def generation(
        self,
        *,
        name: str,
        model: str,
        messages: list[dict[str, str]],
        parameters: dict[str, Any],
        metadata: dict[str, Any],
    ) -> AbstractContextManager[Any]:
        if not self._client:
            return nullcontext(None)
        try:
            context = self._client.start_as_current_observation(
                name=name,
                as_type="generation",
                input=messages,
                model=model,
                model_parameters=parameters,
                metadata=metadata,
            )
            return _SafeContext(context)
        except Exception as exc:  # pragma: no cover - external SDK failure path
            log_event(logger, logging.WARNING, "langfuse_span_create_failed", error=str(exc))
            return nullcontext(None)

    @staticmethod
    def finish_generation(observation: Any, *, result: Any) -> None:
        if observation is None:
            return
        usage = {
            key: value
            for key, value in {
                "input": result.prompt_tokens,
                "output": result.completion_tokens,
                "total": result.total_tokens,
            }.items()
            if value is not None
        }
        completed_at = datetime.now(UTC)
        completion_start_time = None
        if result.first_token_ms is not None:
            request_started_at = completed_at - timedelta(milliseconds=result.latency_ms)
            completion_start_time = request_started_at + timedelta(
                milliseconds=result.first_token_ms
            )
        try:
            observation.update(
                output=result.content,
                usage_details=usage or None,
                completion_start_time=completion_start_time,
                metadata={
                    **result.metadata,
                    "latency_ms": result.latency_ms,
                    "first_token_ms": result.first_token_ms,
                    "streamed": result.streamed,
                    "attempts": result.attempts,
                    "finish_reason": result.finish_reason,
                },
            )
        except Exception as exc:  # pragma: no cover - external SDK failure path
            log_event(logger, logging.WARNING, "langfuse_generation_update_failed", error=str(exc))

    @staticmethod
    def fail(observation: Any, error: Exception) -> None:
        if observation is not None:
            try:
                observation.update(level="ERROR", status_message=str(error))
            except Exception as exc:  # pragma: no cover - external SDK failure path
                log_event(logger, logging.WARNING, "langfuse_error_update_failed", error=str(exc))

    @staticmethod
    def finish_conversation(observation: Any, status: str | None) -> None:
        if observation is None:
            return
        try:
            observation.update(output={"status": status})
        except Exception as exc:  # pragma: no cover - external SDK failure path
            log_event(logger, logging.WARNING, "langfuse_agent_update_failed", error=str(exc))

    def flush(self) -> None:
        if self._client:
            try:
                self._client.flush()
            except Exception as exc:  # pragma: no cover - external best-effort path
                log_event(logger, logging.WARNING, "langfuse_flush_failed", error=str(exc))

    def shutdown(self) -> None:
        if self._client:
            try:
                self._client.shutdown()
            except Exception as exc:  # pragma: no cover
                log_event(logger, logging.WARNING, "langfuse_shutdown_failed", error=str(exc))
