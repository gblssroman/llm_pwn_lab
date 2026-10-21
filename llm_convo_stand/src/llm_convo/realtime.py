import asyncio
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(slots=True)
class RealtimeEvent:
    id: str
    event: str
    data: dict[str, Any]
    created_at: str


class RealtimeBroker:
    """Process-local fan-out for transient token events.

    Durable conversation state remains in PostgreSQL. SSE clients periodically
    reconcile with the database, so dropped events or reconnects do not lose
    completed turns.
    """

    def __init__(self, queue_size: int = 2048) -> None:
        self._subscribers: dict[uuid.UUID, set[asyncio.Queue[RealtimeEvent]]] = defaultdict(set)
        self._queue_size = queue_size

    async def publish(self, conversation_id: uuid.UUID, event: str, data: dict[str, Any]) -> None:
        item = RealtimeEvent(
            id=uuid.uuid4().hex,
            event=event,
            data=data,
            created_at=datetime.now(UTC).isoformat(),
        )
        for queue in tuple(self._subscribers.get(conversation_id, ())):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(item)

    @asynccontextmanager
    async def subscribe(
        self, conversation_id: uuid.UUID
    ) -> AsyncIterator[asyncio.Queue[RealtimeEvent]]:
        queue: asyncio.Queue[RealtimeEvent] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers[conversation_id].add(queue)
        try:
            yield queue
        finally:
            subscribers = self._subscribers.get(conversation_id)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    self._subscribers.pop(conversation_id, None)
