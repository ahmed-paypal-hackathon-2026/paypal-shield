"""Claim Arbiter — 4-Gate Deterministic Payout Logic (Person 2).

Visual discrepancy:
    D_claim = clamp([0,1], 1 - cos(e_listing, e_received))

The 4 payout gates (all must pass for AUTO_APPROVED):
    Gate 1 — Discrepancy:   D_claim >= 0.65
                             D_claim <  0.35  → REJECTED (not AUTO_APPROVED)
    Gate 2 — Amount:        claim_amount <= 250.00
    Gate 3 — Velocity:      buyer_past_claims < 2 AND buyer_claim_ratio < 0.30
    Gate 4 — No dispute:    dispute_active is False

If Gate 1 fails with D < 0.35 → REJECTED.
If any other single gate fails → MANUAL_REVIEW.
If all 4 pass → AUTO_APPROVED.

Internal helper _apply_gates() is exported for unit-test direct access.
"""

from __future__ import annotations

import hashlib
import math
import struct
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Tuple

_FOUR_PLACES = Decimal("0.0001")
_CLAMP_MIN = Decimal("0.00")
_CLAMP_MAX = Decimal("1.00")

_D_AUTO_THRESHOLD = Decimal("0.65")
_D_REJECT_THRESHOLD = Decimal("0.35")
_AMOUNT_LIMIT = Decimal("250.00")
_VELOCITY_CLAIMS_LIMIT = 2
_VELOCITY_RATIO_LIMIT = Decimal("0.30")


def _clamp(v: Decimal) -> Decimal:
    return max(_CLAMP_MIN, min(_CLAMP_MAX, v))


def _q4(v: Decimal) -> Decimal:
    return v.quantize(_FOUR_PLACES, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Visual discrepancy helpers
# ---------------------------------------------------------------------------

def _cosine_similarity_bytes(a: bytes, b: bytes) -> Decimal:
    """Pixel-level cosine similarity between two grayscale byte arrays.

    Returns Decimal cosine similarity ∈ [0, 1].
    Uses integer arithmetic where possible; only converts to Decimal at the end.
    """
    if not a or not b:
        return Decimal("0")
    # Align lengths by sampling
    n = min(len(a), len(b), 2048)
    step_a = max(1, len(a) // n)
    step_b = max(1, len(b) // n)
    va = list(a[::step_a][:n])  # list of int (0-255)
    vb = list(b[::step_b][:n])
    dot = sum(x * y for x, y in zip(va, vb))
    norm_a_sq = sum(x * x for x in va)
    norm_b_sq = sum(y * y for y in vb)
    if norm_a_sq == 0 or norm_b_sq == 0:
        return Decimal("0")
    import math as _math
    denom = _math.sqrt(norm_a_sq * norm_b_sq)
    return Decimal(str(round(dot / denom, 6)))


def _try_clip_cosine(listing_image: bytes, received_image: bytes) -> Decimal | None:
    """Attempt CLIP embedding-based cosine similarity. Returns None on failure."""
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
        from PIL import Image  # type: ignore
        import io
        import numpy as np  # type: ignore

        model = SentenceTransformer("clip-ViT-B-32")
        img_a = Image.open(io.BytesIO(listing_image)).convert("RGB")
        img_b = Image.open(io.BytesIO(received_image)).convert("RGB")
        emb_a = model.encode(img_a, convert_to_numpy=True)
        emb_b = model.encode(img_b, convert_to_numpy=True)
        dot = float(np.dot(emb_a, emb_b))
        norm = float(np.linalg.norm(emb_a) * np.linalg.norm(emb_b))
        cos = dot / norm if norm > 0 else Decimal("0")
        return Decimal(str(round(float(cos), 6)))
    except Exception:
        return None


def _compute_discrepancy(listing_image: bytes, received_image: bytes) -> Decimal:
    """D_claim = clamp([0,1], 1 - cos(e_listing, e_received))."""
    cos_dec = _try_clip_cosine(listing_image, received_image)
    if cos_dec is None:
        cos_dec = _cosine_similarity_bytes(listing_image, received_image)
    d_raw = Decimal("1") - cos_dec
    return _clamp(d_raw)


# ---------------------------------------------------------------------------
# Gate evaluation (exported for unit tests)
# ---------------------------------------------------------------------------

def _apply_gates(
    d_claim: Decimal,
    ctx: "ArbiterContext",  # type: ignore[name-defined]  # noqa: F821
) -> Tuple["ClaimState", Dict[str, bool]]:  # type: ignore[name-defined]  # noqa: F821
    """Apply the 4 deterministic payout gates.

    Returns:
        (outcome, gate_results)
        outcome: ClaimState enum — AUTO_APPROVED | MANUAL_REVIEW | REJECTED
        gate_results: dict with gate_discrepancy_ok, gate_amount_ok,
                      gate_velocity_ok, gate_no_dispute
    """
    from app.schemas.contracts import ClaimState  # lazy import

    # Gate 1: Discrepancy
    if d_claim < _D_REJECT_THRESHOLD:
        return (ClaimState.REJECTED, {
            "gate_discrepancy_ok": False,
            "gate_amount_ok": False,
            "gate_velocity_ok": False,
            "gate_no_dispute": False,
        })

    gate_discrepancy_ok = d_claim >= _D_AUTO_THRESHOLD

    # Gate 2: Claim amount
    gate_amount_ok = ctx.claim_amount <= _AMOUNT_LIMIT

    # Gate 3: Buyer velocity
    gate_velocity_ok = (
        ctx.buyer_past_claims < _VELOCITY_CLAIMS_LIMIT
        and ctx.buyer_claim_ratio < _VELOCITY_RATIO_LIMIT
    )

    # Gate 4: No active dispute
    gate_no_dispute = not ctx.dispute_active

    all_pass = (
        gate_discrepancy_ok
        and gate_amount_ok
        and gate_velocity_ok
        and gate_no_dispute
    )

    outcome = ClaimState.AUTO_APPROVED if all_pass else ClaimState.MANUAL_REVIEW

    return (outcome, {
        "gate_discrepancy_ok": gate_discrepancy_ok,
        "gate_amount_ok": gate_amount_ok,
        "gate_velocity_ok": gate_velocity_ok,
        "gate_no_dispute": gate_no_dispute,
    })


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def arbitrate_claim(
    claim: "ClaimRecord",        # type: ignore[name-defined]  # noqa: F821
    listing_image: bytes,
    received_image: bytes,
    ctx: "ArbiterContext",       # type: ignore[name-defined]  # noqa: F821
) -> "ArbiterDecision":          # type: ignore[name-defined]  # noqa: F821
    """Run the 4-gate claim arbiter and return an ArbiterDecision.

    Args:
        claim:          The submitted ClaimRecord from the database.
        listing_image:  Raw bytes of the original listing image.
        received_image: Raw bytes of the buyer's received-item image.
        ctx:            ArbiterContext supplying buyer history & claim metadata.

    Returns:
        ArbiterDecision with outcome, gate flags, discrepancy score,
        and canonical reason codes.
    """
    from app.schemas.contracts import ArbiterDecision, ClaimState

    d_claim = _compute_discrepancy(listing_image, received_image)
    outcome_enum, gates = _apply_gates(d_claim, ctx)

    reason_codes: List[str] = []
    if not gates["gate_discrepancy_ok"]:
        reason_codes.append("DISCREPANCY_LOW" if d_claim >= _D_REJECT_THRESHOLD else "DISCREPANCY_MATCH")
    if not gates["gate_amount_ok"]:
        reason_codes.append("AMOUNT_EXCEEDS_LIMIT")
    if not gates["gate_velocity_ok"]:
        reason_codes.append("BUYER_VELOCITY_EXCEEDED")
    if not gates["gate_no_dispute"]:
        reason_codes.append("ACTIVE_DISPUTE_PRESENT")
    if outcome_enum == ClaimState.AUTO_APPROVED:
        reason_codes.append("ALL_GATES_PASSED")

    return ArbiterDecision(
        claim_id=claim.claim_id,
        discrepancy_score=_q4(d_claim),
        gate_amount_ok=gates["gate_amount_ok"],
        gate_velocity_ok=gates["gate_velocity_ok"],
        gate_no_dispute=gates["gate_no_dispute"],
        gate_discrepancy_ok=gates["gate_discrepancy_ok"],
        outcome=outcome_enum,
        reason_codes=reason_codes,
    )
