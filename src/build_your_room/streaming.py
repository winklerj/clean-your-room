import asyncio
from collections import defaultdict


class LogBuffer:
    """In-memory log buffer with pub/sub for SSE streaming."""

    def __init__(self):
        self._history: dict[int, list[str]] = defaultdict(list)
        self._subscribers: dict[int, list[asyncio.Queue]] = defaultdict(list)
        self._closed: set[int] = set()

    def append(self, job_id: int, message: str) -> None:
        """Append a message and notify all subscribers."""
        self._history[job_id].append(message)
        for queue in self._subscribers[job_id]:
            queue.put_nowait(message)

    def get_history(self, job_id: int) -> list[str]:
        """Get all historical messages for a job."""
        return list(self._history[job_id])

    async def subscribe(self, job_id: int):
        """Async generator that yields new messages for a job."""
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._subscribers[job_id].append(queue)
        try:
            while True:
                if job_id in self._closed and queue.empty():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=0.5)
                    if msg is None:
                        break
                    yield msg
                except asyncio.TimeoutError:
                    if job_id in self._closed:
                        break
        finally:
            self._subscribers[job_id].remove(queue)

    def close(self, job_id: int) -> None:
        """Signal that no more messages will be sent for this job."""
        self._closed.add(job_id)
        for queue in self._subscribers[job_id]:
            queue.put_nowait(None)
