"""PayPal webhook endpoint (Person 1, spec sections 2.2 / 2.6).

POST /api/v1/webhooks/paypal
    1. read RAW body + the five PayPal transmission headers
    2. verify the signature (spec 2.6); FAILED -> 400, event NOT stored
    3. dedupe: insert-first on webhook_events.event_id; a duplicate returns
       200 immediately with zero side effects
    4. schedule processing in a background task (fresh session from the
       session factory: the dependency session commits at teardown, which
       runs BEFORE background tasks, so the dedupe row is durably in)
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_session_factory

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_transport(request: Request):
    transport = getattr(request.app.state, "transport", None)
    if transport is None:
        return None  # demo mode / no creds: verification falls back to UNVERIFIED_DEMO
    return transport


def _headers_of(request: Request) -> dict[str, str]:
    """Header names uppercased so PAYPAL-* lookups are case-insensitive."""
    return {k.upper(): v for k, v in request.headers.items()}


async def _process_event(session_factory, payload: dict[str, Any], event_id: str) -> None:
    """Background task: apply the event, then mark it processed."""
    from app.services.webhooks import mark_processed, process_event

    try:
        async with session_factory() as session:
            outcome = await process_event(session, payload)
            await mark_processed(session, event_id)
            await session.commit()
            # Phase 4: stream events staged by reconciles/confirmations are
            # published only after this commit succeeded.
            from app.services.events import flush_session_events

            await flush_session_events(session)
            logger.info("webhook %s processed -> %s", event_id, outcome)
    except Exception:  # noqa: BLE001 - background task: log, never crash the app
        # The event stays unprocessed (processed_at NULL) and the stored row
        # lets an operator replay it (or a redelivery is dedupe-safe to reprocess
        # only via manual requeue). All handlers are idempotent.
        logger.exception("webhook %s processing failed", event_id)


@router.post("/webhooks/paypal")
async def paypal_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
) -> JSONResponse:
    # Local imports keep module import cheap and avoid request-path import
    # of config in tests that build the app manually.
    from app.services.webhooks import (
        WebhookVerificationError,
        extract_event_id,
        store_event,
        verify_webhook_signature,
    )

    settings = request.app.state.settings
    raw = await request.body()
    headers = _headers_of(request)

    try:
        mode = await verify_webhook_signature(
            _get_transport(request), settings, headers=headers, raw_body=raw
        )
    except WebhookVerificationError as exc:
        logger.warning("webhook rejected (verification): %s", exc)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"status": "VERIFICATION_FAILED", "detail": "signature verification failed"},
        )

    try:
        event: dict[str, Any] = json.loads(raw.decode("utf-8"))
        if not isinstance(event, dict):
            raise ValueError("payload is not an object")
    except (ValueError, UnicodeDecodeError):
        logger.warning("webhook rejected (undecodable body)")
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"status": "INVALID_BODY"},
        )

    event_type = str(event.get("event_type", ""))
    event_id = extract_event_id(headers, event)
    if not event_id:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"status": "MISSING_EVENT_ID"},
        )

    # Dedupe in the REQUEST session; the teardown commit lands before the
    # background task starts, so the unique row is durable first.
    fresh = await store_event(session, event_id, event_type, event)
    if not fresh:
        return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "DUPLICATE"})

    session_factory = get_session_factory(request)
    background_tasks.add_task(_process_event, session_factory, event, event_id)
    logger.info("webhook %s accepted (%s, type=%s)", event_id, mode, event_type)
    return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "ACCEPTED"})


__all__ = [
    "router",
    "paypal_webhook",
    "_process_event",
]
