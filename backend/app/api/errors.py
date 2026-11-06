"""Safe error handling (Person 1).

Rules (spec section 2.8):
- Never leak internals: responses carry a stable ``code`` plus a safe message.
- Full detail goes to the (redacted) log only.
- ``QuoteError`` subclasses map to 400/409 depending on cause.
- Anything unexpected is 500 ``internal_error`` — never a raw traceback.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.services.paypal.errors import (
    IllegalOrderTransition,
    OrderNotFoundError,
    OrderPayloadError,
    PayPalClientError,
    PayPalTransientError,
)
from app.services.quotes import (
    QuoteConsumedError,
    QuoteError,
    QuoteExpiredError,
    QuoteSignatureError,
)
from app.services.claims import (
    ClaimAmountError,
    ClaimNotCapturedError,
    ClaimNotFoundError,
    ClaimUploadError,
    ClaimWindowError,
    IllegalClaimTransition,
)
from app.services.webhooks import WebhookVerificationError

logger = logging.getLogger(__name__)


class SafeHTTPException(Exception):
    """HTTP error with a stable machine code and already-safe message."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(SafeHTTPException)
    async def _safe_handler(request: Request, exc: SafeHTTPException):
        return JSONResponse(
            status_code=exc.status_code, content=_error_body(exc.code, exc.message)
        )

    # ---- Phase 2: PayPal order flow --------------------------------------
    @app.exception_handler(PayPalTransientError)
    async def _transient_handler(request: Request, exc: PayPalTransientError):
        # Attempts exhausted on 429/5xx/timeout. The flow already marked the
        # object PENDING_RECONCILIATION; surface it instead of a bare 503.
        logger.warning("PayPal transient exhaustion: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=_error_body(
                "RECONCILIATION_PENDING",
                "PayPal is temporarily unavailable; the order is safe and will be reconciled",
            ),
        )

    @app.exception_handler(PayPalClientError)
    async def _client_handler(request: Request, exc: PayPalClientError):
        # PayPal rejected the request (400/422). debug_id is safe to surface
        # (PayPal uses it for support correlation); issue names are stable.
        logger.warning("PayPal client error: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_error_body(
                "PAYPAL_CLIENT_ERROR",
                f"PayPal rejected the request (debug_id={exc.debug_id or 'n/a'})",
            ),
        )

    @app.exception_handler(OrderPayloadError)
    async def _payload_handler(request: Request, exc: OrderPayloadError):
        # Shield-side bug class: never retried, never sent; page via log.
        logger.critical("order payload invariant failed: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_body(
                "INTERNAL_ERROR", "internal_error"
            ),
        )

    @app.exception_handler(OrderNotFoundError)
    async def _order_not_found_handler(request: Request, exc: OrderNotFoundError):
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=_error_body("ORDER_NOT_FOUND", "unknown order"),
        )

    @app.exception_handler(IllegalOrderTransition)
    async def _transition_handler(request: Request, exc: IllegalOrderTransition):
        logger.warning("illegal order transition: %s -> %s", exc.from_state, exc.to_state)
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=_error_body(
                "ILLEGAL_ORDER_TRANSITION", "order state does not allow this action"
            ),
        )

    @app.exception_handler(QuoteSignatureError)
    async def _sig_handler(request: Request, exc: QuoteSignatureError):
        logger.warning("quote rejected: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_error_body("QUOTE_INVALID", "quote signature verification failed"),
        )

    @app.exception_handler(QuoteExpiredError)
    async def _expired_handler(request: Request, exc: QuoteExpiredError):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_error_body("QUOTE_EXPIRED", "quote expired"),
        )

    @app.exception_handler(QuoteConsumedError)
    async def _consumed_handler(request: Request, exc: QuoteConsumedError):
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=_error_body("QUOTE_CONSUMED", "quote already used"),
        )

    # ---- Phase 3: claims + webhooks ---------------------------------------
    @app.exception_handler(ClaimNotFoundError)
    async def _claim_not_found_handler(request: Request, exc: ClaimNotFoundError):
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=_error_body("CLAIM_NOT_FOUND", "unknown claim"),
        )

    @app.exception_handler(ClaimWindowError)
    async def _claim_window_handler(request: Request, exc: ClaimWindowError):
        logger.warning("claim rejected: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_error_body("CLAIM_WINDOW_PASSED", "claim window has passed"),
        )

    @app.exception_handler(ClaimNotCapturedError)
    async def _claim_not_captured_handler(request: Request, exc: ClaimNotCapturedError):
        logger.warning("claim rejected: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_error_body("ORDER_NOT_CLAIM_ELIGIBLE", "order is not eligible for a claim"),
        )

    @app.exception_handler(ClaimAmountError)
    async def _claim_amount_handler(request: Request, exc: ClaimAmountError):
        logger.warning("claim rejected: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_error_body("INVALID_CLAIM_AMOUNT", "claim amount is invalid"),
        )

    @app.exception_handler(ClaimUploadError)
    async def _claim_upload_handler(request: Request, exc: ClaimUploadError):
        logger.warning("claim rejected: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_error_body("INVALID_UPLOAD", "photo upload is invalid"),
        )

    @app.exception_handler(IllegalClaimTransition)
    async def _claim_transition_handler(request: Request, exc: IllegalClaimTransition):
        logger.warning("illegal claim transition: %s -> %s", exc.from_state, exc.to_state)
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=_error_body(
                "ILLEGAL_CLAIM_TRANSITION", "claim state does not allow this action"
            ),
        )

    @app.exception_handler(WebhookVerificationError)
    async def _webhook_verify_handler(request: Request, exc: WebhookVerificationError):
        logger.warning("webhook verification failed: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_error_body("WEBHOOK_VERIFICATION_FAILED", "webhook signature not verified"),
        )
    # ---- generic quote fallback (keep last among quote handlers) ----------
    @app.exception_handler(QuoteError)
    async def _quote_handler(request: Request, exc: QuoteError):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_error_body("QUOTE_INVALID", str(exc)),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_error_body("VALIDATION_ERROR", "request failed validation"),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_handler(request: Request, exc: StarletteHTTPException):
        # HTTPException raised by routers keeps its detail (messages are written safe by design).
        detail = exc.detail if isinstance(exc.detail, str) else "request error"
        return JSONResponse(status_code=exc.status_code, content={"detail": detail})

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception):
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_body("INTERNAL_ERROR", "internal_error"),
        )
