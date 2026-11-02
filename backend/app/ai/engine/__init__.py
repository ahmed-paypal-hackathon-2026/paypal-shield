"""engine package — re-exports for actuarial computations (Person 2)."""

from .actuarial import (
    # Private helpers (exported for unit tests)
    _compute_r_base,
    _compute_r_final,
    _gate_decision,
    _compute_fee_amount,
    _sign_payload,
    _build_sign_payload,
    # Public API (used by ShieldRiskEngine in app.ai.__init__)
    compute_risk,
    compute_fee,
    sign_quote,
    # Constants
    W_VIS,
    W_SEM,
    W_REV,
    W_PRICE,
    W_MER,
    R_BLOCKED_THRESHOLD,
)

__all__ = [
    "_compute_r_base",
    "_compute_r_final",
    "_gate_decision",
    "_compute_fee_amount",
    "_sign_payload",
    "_build_sign_payload",
    "compute_risk",
    "compute_fee",
    "sign_quote",
    "W_VIS",
    "W_SEM",
    "W_REV",
    "W_PRICE",
    "W_MER",
    "R_BLOCKED_THRESHOLD",
]
