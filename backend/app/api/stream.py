"""Server-Sent Events endpoint (Person 1, frozen contract /api/v1/stream).

Contract is explicit: ``GET /api/v1/stream`` returns ``text/event-stream`` with
event types ``transaction | claim | merchant`` and a JSON envelope
``{topic, action, record}``. SSE — not WebSockets — so any client (the
frontend's native ``EventSource``) reconnects automatically.

Frame format per event:

    id: <seq>            event id; clients send it back as Last-Event-ID
    event: <topic>       what EventSource.addEventListener(topic) dispatches
    data: <envelope>     {"topic","action","record"} exactly as mocked

Wire behavior:
- ``Last-Event-ID`` replay: history events with seq > cursor stream first,
  deduped against the live queue by a high-water mark -> no duplicates on
  reconnect, nothing missed while subscribed.
- Heartbeat: ``: ping`` comment lines every ``settings.stream_heartbeat_seconds``
  keep proxies from idling the connection out.
- Disconnect: the generator is cancelled by the ASGI layer, and the ``finally``
  unsubscribes -> no leaked subscriber, no unbounded queue.

AUTH NOTE (documented conflict): the frozen contract declares no security
scheme for this endpoint and the app has no identity system (no user table, no
JWT/OAuth dependency in Phase 1-3). It is therefore implemented contract-
faithfully as the single live-ops feed of the three contract topics. The task
asks for user-level event authorization; that is recorded as a contract-vs-task
conflict in docs/phase4-handoff and needs a Person 4 contract decision (e.g. a
``?buyer_ref=`` filter + bearer scheme) before it can be implemented without
inventing an auth mechanism the API surface does not declare.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.api.deps import get_settings
from app.config import Settings
from app.services.events import Event, get_event_bus

logger = logging.getLogger(__name__)

router = APIRouter()

_SSE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Accel-Buffering": "no",  # nginx: stream through, don't buffer
}


def _frame(event: Event) -> str:
    """Render one SSE event (topic is the dispatched event name)."""
    record_json = event.record.model_dump_json()
    return (
        f"event: {event.topic}\n"
        f"id: {event.seq}\n"
        f'data: {{"topic":"{event.topic}","action":"{event.action}",'
        f'"record":{record_json}}}\n\n'
    )


_SSE_OPENAPI = {  # exact frozen contract response for /api/v1/stream
    "description": "text/event-stream",
    "content": {
        "text/event-stream": {
            "schema": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "action": {"type": "string"},
                    "record": {},
                },
            }
        }
    },
}


@router.get(
    "/stream",
    operation_id="streamEvents",
    description=(
        "Server-Sent Events: event types transaction|claim|merchant; data is "
        "a JSON envelope {topic, action, record}."
    ),
    responses={200: _SSE_OPENAPI},
)
async def stream_events(
    settings: Settings = Depends(get_settings),
    last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
    topics: Optional[str] = Query(
        default=None,
        description="Optional comma-separated filter (additive deviation), "
        "e.g. topics=transaction,claim",
    ),
) -> StreamingResponse:
    """Live event feed. Returns ``text/event-stream`` (frozen contract)."""
    bus = get_event_bus()

    # Additive topic filter (contract declares none; default = all topics).
    allowed = None
    if topics:
        wanted = {t.strip() for t in topics.split(",") if t.strip()}
        if not wanted <= {"transaction", "claim", "merchant"}:
            raise HTTPException(status_code=422, detail="unknown topic in topics")
        allowed = wanted

    try:
        cursor = int(last_event_id) if last_event_id else 0
    except ValueError:
        cursor = 0  # tolerate a garbage cursor: treat as a fresh connection

    heartbeat = float(settings.stream_heartbeat_seconds)

    async def event_stream() -> AsyncIterator[str]:
        subscriber = bus.subscribe()
        try:
            yield "retry: 3000\n\n"
            # Subscribe FIRST, then snapshot history: an event published in
            # between lands in both the queue and the replay snapshot and is
            # deduped below by the high-water mark - never lost, never doubled.
            replayed = bus.replay(cursor)
            high_water = max((ev.seq for ev in replayed), default=cursor)
            for ev in replayed:
                if allowed is None or ev.topic in allowed:
                    yield _frame(ev)

            while True:
                ev = await subscriber.receive(timeout=heartbeat)
                if ev is None:
                    yield ": ping\n\n"
                    continue
                if ev.seq <= high_water:
                    continue  # already delivered via replay
                if allowed is not None and ev.topic not in allowed:
                    continue
                yield _frame(ev)
        finally:
            bus.unsubscribe(subscriber)
            logger.debug(
                "SSE subscriber %s detached (%d remaining)",
                subscriber.sid,
                bus.subscriber_count,
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
