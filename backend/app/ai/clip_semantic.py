"""Pillar 2 — Semantic Image-to-Text Alignment (Person 2).

Embeds listing image and combined text (title + description) using
sentence-transformers (clip-ViT-B-32) and computes cosine similarity c.

Mapping:
    c >= 0.32  → S_semantic = 0.00  (good alignment, no risk)
    c <  0.32  → S_semantic = clamp((0.32 - c) / 0.20, 0, 1)

Fallback: heuristic text overlap when sentence-transformers unavailable.
"""

from __future__ import annotations

import asyncio
import math
from decimal import Decimal, ROUND_HALF_UP
from typing import Tuple

_TWO_PLACES = Decimal("0.01")
_CLAMP_MIN = Decimal("0.00")
_CLAMP_MAX = Decimal("1.00")
_ALIGNMENT_THRESHOLD = 0.32
_SCALE_FACTOR = 0.20
_REASON_LOW = "SEM_LOW_ALIGNMENT"
_REASON_OK = "SEM_ALIGNED"


def _clamp_decimal(value: Decimal) -> Decimal:
    return max(_CLAMP_MIN, min(_CLAMP_MAX, value))


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Cosine similarity → risk score mapping (pure math, no deps)
# ---------------------------------------------------------------------------

def _cosine_to_risk(c: float) -> Decimal:
    if c >= _ALIGNMENT_THRESHOLD:
        return _CLAMP_MIN
    raw = (_ALIGNMENT_THRESHOLD - c) / _SCALE_FACTOR
    raw = max(0.0, min(1.0, raw))
    return _clamp_decimal(_quantize(Decimal(str(round(raw, 4)))))


# ---------------------------------------------------------------------------
# Heuristic fallback: token overlap similarity
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> set[str]:
    import re
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _text_heuristic_cosine(title: str, description: str) -> float:
    """Token Jaccard similarity as a cosine-similarity proxy."""
    a = _tokenize(title)
    b = _tokenize(description)
    if not a and not b:
        return 0.32  # neutral — no text, no risk
    if not a or not b:
        return 0.20  # one side empty → low alignment
    intersection = len(a & b)
    union = len(a | b)
    jaccard = intersection / union if union > 0 else 0.0
    # Scale Jaccard ∈ [0,1] → cosine-proxy ∈ [0, 0.6] (empirical calibration)
    return min(0.6, jaccard * 0.8)


def _image_text_clip_cosine(image_bytes: bytes, title: str, description: str) -> float:
    """Compute CLIP cosine similarity between image and text."""
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore[import]
        from PIL import Image  # type: ignore[import]
        import io
        import numpy as np  # type: ignore[import]

        model = SentenceTransformer("clip-ViT-B-32")
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        combined_text = f"{title} {description}".strip()

        img_emb = model.encode(img, convert_to_numpy=True)
        txt_emb = model.encode(combined_text, convert_to_numpy=True)

        # Cosine similarity
        dot = float(np.dot(img_emb, txt_emb))
        norm = float(np.linalg.norm(img_emb) * np.linalg.norm(txt_emb))
        return dot / norm if norm > 0 else 0.0
    except Exception:
        return _text_heuristic_cosine(title, description)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def score_semantic(
    image_bytes: bytes,
    title: str,
    description: str,
    timeout_s: float = 10.0,
) -> Tuple[Decimal, str]:
    """Score image-text semantic alignment.

    Args:
        image_bytes: Raw listing image bytes.
        title: Listing title string.
        description: Listing description string.
        timeout_s: Max seconds for CLIP inference.

    Returns:
        (s_semantic, reason_code)
        s_semantic ∈ [0.00, 1.00] as Decimal.
        reason_code ∈ {SEM_LOW_ALIGNMENT, SEM_ALIGNED}
    """
    try:
        loop = asyncio.get_event_loop()

        def _run() -> float:
            if image_bytes:
                return _image_text_clip_cosine(image_bytes, title, description)
            return _text_heuristic_cosine(title, description)

        c = await asyncio.wait_for(
            loop.run_in_executor(None, _run),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        # On timeout, use text-only heuristic (non-blocking)
        c = _text_heuristic_cosine(title, description)
    except Exception:
        c = _text_heuristic_cosine(title, description)

    s_semantic = _cosine_to_risk(c)
    reason = _REASON_LOW if s_semantic > _CLAMP_MIN else _REASON_OK
    return (s_semantic, reason)
