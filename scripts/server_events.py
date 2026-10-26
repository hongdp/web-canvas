"""Per-book change events (docs/features/backend_authority.md §2.2).

Every write to a book's chapters publishes an event here, and every open
tab of that book subscribes over server-sent events. Another tab or device
learns that a chapter changed when it happens, instead of on its next focus
(which reloaded the whole book and discarded unsynced edits).

In-process: the API server runs as one uvicorn process, so a dict of
asyncio queues is the whole broker. Events carry the writer's client id, so
a tab can ignore the echo of its own write.
"""

import asyncio
import itertools
import json
from typing import Any, AsyncIterator, Dict, Optional, Set, Tuple

# A subscriber that stops reading must not grow without bound; it is
# dropped and reconnects (EventSource does so on its own). Sized for a run's
# live deltas (one event per token), which a reading tab drains far faster.
QUEUE_LIMIT = 4096
HEARTBEAT_SECONDS = 15.0

_Key = Tuple[str, str]


class BookEventHub:
    def __init__(self) -> None:
        self._subscribers: Dict[_Key, Set[asyncio.Queue]] = {}
        self._ids = itertools.count(1)

    def subscribe(self, username: str, book_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self._subscribers.setdefault((username, book_id), set()).add(queue)
        return queue

    def unsubscribe(self, username: str, book_id: str, queue: asyncio.Queue) -> None:
        subs = self._subscribers.get((username, book_id))
        if not subs:
            return
        subs.discard(queue)
        if not subs:
            del self._subscribers[(username, book_id)]

    def publish(self, username: str, book_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
        """Hand an event to every subscriber of the book; returns it with its id."""
        stamped = {"id": next(self._ids), **event}
        for queue in list(self._subscribers.get((username, book_id), ())):
            try:
                queue.put_nowait(stamped)
            except asyncio.QueueFull:
                self.unsubscribe(username, book_id, queue)
        return stamped

    def subscriber_count(self, username: str, book_id: str) -> int:
        return len(self._subscribers.get((username, book_id), ()))


hub = BookEventHub()


def client_id_of(request: Any) -> Optional[str]:
    """The writing tab's id (X-Client-Id), echoed in its events."""
    value = request.headers.get("x-client-id") if request is not None else None
    return value[:64] if value else None


async def event_stream(username: str, book_id: str, is_disconnected) -> AsyncIterator[str]:
    """Server-sent events for one book until the client goes away."""
    queue = hub.subscribe(username, book_id)
    try:
        yield "retry: 3000\n\n"
        while True:
            if await is_disconnected():
                return
            try:
                event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                # A comment line: keeps proxies from closing an idle stream.
                yield ": heartbeat\n\n"
                continue
            yield f"id: {event['id']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
    finally:
        hub.unsubscribe(username, book_id, queue)
