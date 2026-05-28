"""Per-job event log backed by a Redis Stream.

Why Redis Streams over Pub/Sub:
- Persistent: a late subscriber can replay from any point (Last-Event-ID).
- Native message IDs map 1:1 to SSE event IDs.
- Naturally bounded with MAXLEN ~ to prevent unbounded growth.
"""

from __future__ import annotations

import json
from typing import AsyncIterator, Final

from redis.asyncio import Redis


def _stream_key(job_id: str) -> str:
    return f"job:{job_id}:events"


class EventLog:
    def __init__(self, redis: Redis, *, stream_max_len: int = 1000) -> None:
        self._r: Final[Redis] = redis
        self._max_len = stream_max_len

    async def publish(self, job_id: str, type: str, data: dict | None = None) -> str:
        """Append an event; returns the stream-assigned ID."""
        fields = {"type": type, "data": json.dumps(data or {})}
        # MAXLEN ~ N: approximate trim, cheaper than exact.
        event_id = await self._r.xadd(
            _stream_key(job_id),
            fields,
            maxlen=self._max_len,
            approximate=True,
        )
        return event_id

    async def subscribe(
        self,
        job_id: str,
        *,
        since: str | None,
        idle_timeout_s: float = 20.0,
    ) -> AsyncIterator[tuple[str, dict]]:
        """Yield (event_id, event) tuples. `since` is the last-seen stream ID
        (resume point); None means from the start. On idle, yields a keepalive
        event with type='keepalive' so the SSE layer can push a keep-alive comment."""
        cursor = since or "0"
        block_ms = int(idle_timeout_s * 1000)
        while True:
            result = await self._r.xread(
                {_stream_key(job_id): cursor},
                block=block_ms,
                count=100,
            )
            if not result:
                yield ("keepalive", {"type": "keepalive", "data": {}})
                continue
            # result = [(stream_key, [(id, {fields}), ...])]
            for _stream, entries in result:
                for entry_id, fields in entries:
                    cursor = entry_id
                    yield (
                        entry_id,
                        {
                            "type": fields["type"],
                            "data": json.loads(fields["data"]),
                        },
                    )
