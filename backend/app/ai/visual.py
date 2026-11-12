"""Pillar 1 — Visual Asset Integrity (Person 2).

Detects AI-generated/diffusion artifacts using image statistics.
Primary method: Laplacian variance + frequency domain analysis.
ONNX model path respected if MODEL_PATH env var is set.

Score S_visual ∈ [0.00, 1.00] (Decimal, clamped).
is_ai_generated = S_visual >= 0.65
Fallback: (0.50, False, VIS_FALLBACK_DEFAULT) on any error/timeout.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import struct
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_FALLBACK_SCORE = Decimal("0.50")
_AI_THRESHOLD = Decimal("0.65")
_REASON_AI = "VIS_AI_GENERATED"
_REASON_FALLBACK = "VIS_FALLBACK_DEFAULT"
_REASON_CLEAN = "VIS_CLEAN"

_TWO_PLACES = Decimal("0.01")
_CLAMP_MIN = Decimal("0.00")
_CLAMP_MAX = Decimal("1.00")


def _clamp(value: Decimal) -> Decimal:
    return max(_CLAMP_MIN, min(_CLAMP_MAX, value))


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Pure-Python image analysis (no heavy deps required)
# ---------------------------------------------------------------------------

def _decode_jpeg_dimensions(data: bytes) -> Tuple[int, int] | None:
    """Best-effort JPEG width/height extraction."""
    if not data.startswith(b"\xff\xd8"):
        return None
    i = 2
    while i < len(data) - 4:
        marker = data[i:i+2]
        i += 2
        if marker[0:1] != b"\xff":
            break
        length = struct.unpack(">H", data[i:i+2])[0]
        if marker in (b"\xff\xc0", b"\xff\xc2"):
            h = struct.unpack(">H", data[i+3:i+5])[0]
            w = struct.unpack(">H", data[i+5:i+7])[0]
            return w, h
        i += length
    return None


def _sample_gray_pixels(image_bytes: bytes, max_samples: int = 2048) -> list[int]:
    """Extract a representative sample of grayscale pixel values from raw image bytes.

    For images without PIL available, we fall back to sampling the raw bytes
    (works reasonably as a proxy for content entropy on compressed images).
    """
    try:
        from PIL import Image  # type: ignore[import]
        import io

        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        pixels = list(img.getdata())
        if len(pixels) > max_samples:
            step = len(pixels) // max_samples
            pixels = pixels[::step][:max_samples]
        return pixels
    except Exception:
        # Fall back to raw byte sampling
        raw = [b for b in image_bytes if b < 256]
        step = max(1, len(raw) // max_samples)
        return raw[::step][:max_samples]


def _laplacian_variance(pixels: list[int]) -> float:
    """Approximate Laplacian variance as a proxy for edge sharpness.

    AI-generated images tend to have unnaturally smooth gradients, yielding
    lower variance than real photos of physical products.
    """
    if len(pixels) < 3:
        return 0.0
    laplacian = []
    for i in range(1, len(pixels) - 1):
        lap = abs(pixels[i - 1] - 2 * pixels[i] + pixels[i + 1])
        laplacian.append(lap)
    if not laplacian:
        return 0.0
    mean = sum(laplacian) / len(laplacian)
    variance = sum((x - mean) ** 2 for x in laplacian) / len(laplacian)
    return variance


def _entropy(pixels: list[int]) -> float:
    """Shannon entropy of the pixel distribution."""
    if not pixels:
        return 0.0
    counts: dict[int, int] = {}
    for p in pixels:
        counts[p] = counts.get(p, 0) + 1
    n = len(pixels)
    ent = 0.0
    for c in counts.values():
        p = c / n
        if p > 0:
            ent -= p * math.log2(p)
    return ent


def _heuristic_ai_score(image_bytes: bytes) -> Decimal:
    """Compute a heuristic AI-generation likelihood score ∈ [0.00, 1.00].

    Low Laplacian variance + high uniformity → high score (more likely AI).
    The thresholds are calibrated for typical e-commerce product images.
    """
    pixels = _sample_gray_pixels(image_bytes)
    if len(pixels) < 10:
        return _FALLBACK_SCORE

    lap_var = _laplacian_variance(pixels)
    ent = _entropy(pixels)

    # Normalise: lap_var in [0, ~5000], entropy in [0, 8]
    # AI images: low lap_var, lower entropy variance
    lap_norm = min(1.0, lap_var / 3000.0)  # 0=very smooth, 1=very noisy
    ent_norm = min(1.0, ent / 8.0)         # 0=flat, 1=max entropy

    # AI score: smooth images with moderate entropy score high
    # Natural product photos have high lap_var
    smoothness = 1.0 - lap_norm
    # Mid-entropy (5-7 bits) typical for AI; very high entropy = noise/real
    ent_score = max(0.0, 1.0 - abs(ent - 6.0) / 2.5) if ent > 0 else 0.5

    raw = 0.6 * smoothness + 0.4 * ent_score
    return _clamp(_quantize(Decimal(str(round(raw, 4)))))


# ---------------------------------------------------------------------------
# ONNX model path (optional)
# ---------------------------------------------------------------------------

def _try_onnx_score(image_bytes: bytes) -> Decimal | None:
    """Attempt inference with ONNX model if available. Returns None on failure."""
    import os
    model_path = os.environ.get("VISUAL_MODEL_PATH", "")
    if not model_path:
        return None
    try:
        import onnxruntime as ort  # type: ignore[import]
        import numpy as np  # type: ignore[import]
        from PIL import Image  # type: ignore[import]
        import io

        sess = ort.InferenceSession(model_path)
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB").resize((224, 224))
        arr = np.array(img, dtype=np.float32) / 255.0
        arr = (arr - np.array([0.485, 0.456, 0.406])) / np.array([0.229, 0.224, 0.225])
        arr = arr.transpose(2, 0, 1)[None]  # NCHW
        input_name = sess.get_inputs()[0].name
        result = sess.run(None, {input_name: arr})[0]
        # Assume binary classifier: index 1 = AI probability
        score = float(result[0][1]) if result[0].shape[-1] >= 2 else float(result[0][0])
        return _clamp(_quantize(Decimal(str(round(score, 4)))))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def score_visual(
    image_bytes: bytes,
    timeout_s: float = 5.0,
) -> Tuple[Decimal, bool, str]:
    """Score image for AI-generation likelihood.

    Args:
        image_bytes: Raw bytes of the listing image.
        timeout_s: Maximum seconds to spend on analysis.

    Returns:
        (s_visual, is_ai_generated, reason_code)
        s_visual ∈ [0.00, 1.00] as Decimal
        is_ai_generated = s_visual >= 0.65
        reason_code ∈ {VIS_AI_GENERATED, VIS_FALLBACK_DEFAULT, VIS_CLEAN}
    """
    if not image_bytes:
        return (_FALLBACK_SCORE, False, _REASON_FALLBACK)

    try:
        loop = asyncio.get_event_loop()

        def _run() -> Decimal:
            onnx = _try_onnx_score(image_bytes)
            if onnx is not None:
                return onnx
            return _heuristic_ai_score(image_bytes)

        s_visual = await asyncio.wait_for(
            loop.run_in_executor(None, _run),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        return (_FALLBACK_SCORE, False, _REASON_FALLBACK)
    except Exception:
        return (_FALLBACK_SCORE, False, _REASON_FALLBACK)

    is_ai_generated = s_visual >= _AI_THRESHOLD
    reason = _REASON_AI if is_ai_generated else _REASON_CLEAN
    return (s_visual, is_ai_generated, reason)


class VisualArbiter:
    """Arbiter for visual asset integrity and AI-generation detection."""

    def __init__(self, model_path: Optional[str] = None) -> None:
        self.model_path = model_path or os.environ.get("VISUAL_MODEL_PATH", "")

    async def evaluate(
        self,
        image_bytes: bytes,
        timeout_s: float = 5.0,
    ) -> Tuple[Decimal, bool, str]:
        """Score image bytes for AI-generation likelihood using neural / statistical analysis.

        Returns:
            (s_visual, is_ai_generated, reason_code)
        """
        logger.info(
            "VisualArbiter: evaluating visual asset (%d bytes)",
            len(image_bytes) if image_bytes else 0,
        )
        s_vis, is_ai, reason = await score_visual(image_bytes, timeout_s=timeout_s)
        logger.info(
            "VisualArbiter output: s_visual=%s, is_ai_generated=%s, reason=%s",
            s_vis,
            is_ai,
            reason,
        )
        return (s_vis, is_ai, reason)

    async def score(
        self,
        image_bytes: bytes,
        timeout_s: float = 5.0,
    ) -> Tuple[Decimal, bool, str]:
        return await self.evaluate(image_bytes, timeout_s=timeout_s)
