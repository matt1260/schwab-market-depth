"""
Directional bias for premium selling — sell premium on the side the TREND protects.

A put credit spread is a bullish/neutral bet (profits if price stays UP). Selling it
into a downtrend = selling puts under a falling knife. A call credit spread is the
mirror (profits if price stays DOWN). So:

  UPTREND   (price > 20MA > 50MA)  → PUT credit spread  (sell puts below)
  DOWNTREND (price < 20MA < 50MA)  → CALL credit spread (sell calls above)
  unclear / conflicting             → SKIP (no directional cushion)

This is the directional filter the IV-only scanner was missing: IV richness says
WHETHER premium is worth selling; trend says WHICH SIDE is safe to sell.
"""

from __future__ import annotations

from typing import List, Optional


def _sma(xs: List[float], n: int) -> Optional[float]:
    return sum(xs[-n:]) / n if len(xs) >= n else None


def trend_side(closes: List[float], fast: int = 20, slow: int = 50) -> dict:
    """Return {side: 'put'|'call'|None, trend: str, last, ma_fast, ma_slow}.
    side = which credit spread the trend supports (None = skip)."""
    if not closes or len(closes) < slow + 1:
        return {"side": None, "trend": "insufficient_history", "last": None,
                "ma_fast": None, "ma_slow": None}
    last = closes[-1]
    ma_f = _sma(closes, fast)
    ma_s = _sma(closes, slow)
    if last > ma_f > ma_s:
        side, trend = "put", "uptrend"          # bullish → sell puts below
    elif last < ma_f < ma_s:
        side, trend = "call", "downtrend"        # bearish → sell calls above
    else:
        side, trend = None, "no_clear_trend"     # chop / conflicting → skip
    return {"side": side, "trend": trend, "last": round(last, 2),
            "ma_fast": round(ma_f, 2), "ma_slow": round(ma_s, 2)}
