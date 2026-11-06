"""Stream event bus (Person 1, contract /api/v1/stream).

SSE topics are exactly the frozen contract's: ``transaction | claim | merchant``
with envelope ``{"topic", "action", "record"}``. The frontend
(``frontend/src/api/stream.ts``) listens via ``EventSource.addEventListener``
on the topic name, so every event MUST be written as ``event: <topic>``.

Event-sourcing rules enforced here:

- Events are published ONLY after the producing transaction commits
  (``enqueue_event`` buffers on the session; ``flush_session_events`` /
  ``commit_and_emit`` publish post-commit). Nothing is emitted for work that
  rolled back -> the stream can never show an unpersisted capture/refund.
- Replay: a bounded in-memory history keyed by a monotonically increasing seq.
  The seq is the SSE ``id:`` line; clients reconnect with ``Last-Event-ID`` and
  receive every event with seq > cursor exactly once (deduped by the stream
  reader via a high-water mark).
- Backpressure: each subscriber has a bounded queue with drop-oldest relative
  to pending queue size (the newest state always wins), so slow clients can
  neither leak memory nor block producers.

NOTE on cross-user isolation: the frozen contract declares NO auth scheme and
NO per-user variant of ``/api/v1/stream`` (it is a live-ops board feed), while
the task asks for user-level event authorization. Implemented contract-faithfully
(single public feed of the three contract topics); per-topic filtering is
exposed as an OPTIONAL additive query parameter (documented deviation) and the
conflict is recorded in docs/phase4-handoff. No record field is invented or
re-shaped to add identity data.
"""

from __future__ import annotations

import asyncio
import inspect
import itertools
import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Union

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

TOPICS = ("transaction", "claim", "merchant")
ACTIONS = ("add", "update")

# Bounded replay history (Last-Event-ID window) and per-subscriber buffer.
HISTORY_MAX_EVENTS = 500
SUBSCRIBER_BUFFER_EVENTS = 200

RecordFactory = Callable[[AsyncSession], Union[BaseModel, Any]]


@dataclass(frozen=True)
class Event:
    """One published stream event; ``seq`` is the SSE id (cursor)."""

    seq: int
    topic: str
    action: str
    record: BaseModel
    at: datetime


@dataclass
class _PendingEvent:
    """Buffered pre-commit intent: (topic, action, record-or-factory)."""

    topic: str
    action: str
    record: Union[BaseModel, RecordFactory]


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------
class Subscriber:
    """Bounded, drop-oldest event queue for one SSE connection."""

    __slots__ = ("sid", "_loop", "_queue", "_available")

    def __init__(self, sid: int, loop: asyncio.AbstractEventLoop) -> None:
        self.sid = sid
        self._loop = loop
        self._queue: deque[Event] = deque(maxlen=SUBSCRIBER_BUFFER_EVENTS)
        self._available = asyncio.Event()

    def push(self, event: Event) -> None:
        """Scheduling target (runs INSIDE the subscriber's loop)."""
        if len(self._queue) == self._queue.maxlen:
            self._queue.popleft()  # drop-oldest: newest state always wins
        self._queue.append(event)
        self._available.set()

    async def receive(self, timeout: Optional[float] = None) -> Optional[Event]:
        """Next buffered event, or None on timeout (used for heartbeats)."""
        while True:
            if self._queue:
                return self._queue.popleft()
            self._available.clear()
            if self._queue:  # lost-wakeup guard
                return self._queue.popleft()
            try:
                if timeout is None:
                    await self._available.wait()
                else:
                    await asyncio.wait_for(self._available.wait(), timeout)
            except (asyncio.TimeoutError, TimeoutError):
                return None


class EventBus:
    """In-process pub/sub with seq-stamped replay history.

    Thread-safe publish (``call_soon_threadsafe`` per subscriber), so unit
    tests may publish from any thread while SSE generators stay loop-safe.
    """

    def __init__(self) -> None:
        self._seq = 0
        self._sids = itertools.count(1)
        self._lock = threading.Lock()
        self._history: deque[Event] = deque(maxlen=HISTORY_MAX_EVENTS)
        self._subscribers: dict[int, Subscriber] = {}

    # -- publish ------------------------------------------------------------
    def publish(
        self,
        topic: str,
        action: str,
        record: BaseModel,
    ) -> Event:
        if topic not in TOPICS:
            raise ValueError(f"unknown stream topic {topic!r}")
        if action not in ACTIONS:
            raise ValueError(f"unknown stream action {action!r}")

        with self._lock:
            self._seq += 1
            event = Event(
                seq=self._seq,
                topic=topic,
                action=action,
                record=record,
                at=datetime.now(timezone.utc),
            )
            self._history.append(event)
            subscribers = list(self._subscribers.values())

        for subscriber in subscribers:
            loop = subscriber._loop
            try:
                loop.call_soon_threadsafe(subscriber.push, event)
            except RuntimeError:  # pragma: no cover - loop closed mid-flight
                logger.debug("subscriber %s loop closed; event dropped", subscriber.sid)
        return event

    # -- subscription -------------------------------------------------------
    def subscribe(self) -> Subscriber:
        """Register a new subscriber bound to the caller's running loop."""
        loop = asyncio.get_running_loop()
        with self._lock:
            sid = next(self._sids)
            subscriber = Subscriber(sid, loop)
            self._subscribers[sid] = subscriber
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        with self._lock:
            self._subscribers.pop(subscriber.sid, None)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    # -- replay -------------------------------------------------------------
    def replay(self, after_seq: int = 0) -> list[Event]:
        """History events with seq strictly greater than ``after_seq``."""
        with self._lock:
            return [event for event in self._history if event.seq > after_seq]


_bus: Optional[EventBus] = None


def get_event_bus() -> EventBus:
    """Process-wide singleton (create_order/capture/claims all feed one board)."""
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus


def reset_event_bus() -> None:
    """Test isolation helper: drop every event and subscriber."""
    global _bus
    _bus = None


# ---------------------------------------------------------------------------
# Commit-boundary helpers (buffer pre-commit, publish post-commit)
# ---------------------------------------------------------------------------
def enqueue_event(
    session: AsyncSession,
    topic: str,
    action: str,
    record: Union[BaseModel, RecordFactory],
) -> None:
    """Stage a stream event on the session for publication AFTER its commit.

    ``record`` may be a ready BaseModel (built from already-loaded rows) or an
    async factory ``factory(session) -> BaseModel`` that re-reads persisted
    state post-commit (expire_on_commit=False keeps instances loaded).
    """
    session.info.setdefault("pending_events", []).append(
        _PendingEvent(topic=topic, action=action, record=record)
    )


async def flush_session_events(session: AsyncSession) -> None:
    """Publish everything staged on this session. Call only after a commit."""
    pending = session.info.pop("pending_events", None)
    if not pending:
        return
    bus = get_event_bus()
    for item in pending:
        try:
            record = item.record
            if callable(record):
                record = record(session)
            if inspect.isawaitable(record):
                record = await record
            bus.publish(item.topic, item.action, record)
        except Exception:  # noqa: BLE001 - the stream never breaks persistence
            logger.exception(
                "stream event build failed (%s/%s); event skipped",
                item.topic, item.action,
            )


async def commit_and_emit(session: AsyncSession) -> None:
    """Commit, then publish this session's staged events."""
    await session.commit()
    await flush_session_events(session)
