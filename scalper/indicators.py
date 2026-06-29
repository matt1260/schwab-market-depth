"""
Pure signal indicators — ported VERBATIM from the SwarmTrade platform scalper
(backend/queen/scalper_loop.py). These operate only on in-memory price series, so
they carry over unchanged. Keeping them in one module makes it trivial to diff
against the platform and merge improvements back later.

Tunable constants mirror the platform exactly.
"""

from __future__ import annotations

from typing import Deque, Dict, List, Optional, Tuple

# ── regime / window constants (mirror platform) ───────────────────────────────
PRICE_WINDOW = 40
REGIME_WINDOW = 16
REGIME_THRESHOLD = 0.72
REGIME_SAMPLE_SECONDS = 15
REGIME_ER_WINDOW = 40       # full buffer kept (~10 min) for range/levels/mtf
ER_WINDOW = 16             # ER computed over the recent ~4 min (scalp horizon)
REGIME_ER_MIN_SAMPLES = 8
THRUST_WINDOW_SAMPLES = 12
THRUST_MIN_ER = 0.70
THRUST_RANGE_MULT = 1.0
TREND_ER_THRESHOLD = 0.35
TREND_STOP_MULT = 1.8
TREND_TIME_MULT = 3.0
TRAIL_ACTIVATE_PCT = 0.12
TRAIL_GIVEBACK_PCT = 0.08

# multi-timeframe + VWAP bands
MTF_MIN_SAMPLES = 8
MTF_FLAT_PCT = 0.03
MTF_MIN_ALIGNMENT = 0.66
MTF_CONFIRM_BOOST = 1.25
MTF_GATE_ENABLED = True
VWAP_MIN_SAMPLES = 12
VWAP_BAND_K = 2.0

# order flow (Schwab L2)
BOOK_IMBALANCE_BLOCK = 0.5
BOOK_IMBALANCE_CONFIRM = 0.3
BOOK_IMBALANCE_BOOST = 1.2

# reversion-to-level (kept OFF, mirrors platform after the 2026-06-18 disable)
REVERSION_ENABLED = False
LEVEL_PROXIMITY_FRAC = 0.20
LEVEL_PROXIMITY_MIN_PCT = 0.08
LEVEL_REVERSION_STRENGTH = 0.85
AUTO_MIDRANGE_SKIP = True


def efficiency_ratio(samples: Optional[Deque], window: int = ER_WINDOW) -> Optional[float]:
    """Kaufman ER over the RECENT regime sub-window (scalp horizon), not the full
    10-min buffer. The full window made big moves that ROUND-TRIP (e.g. a pop then
    a flush) read net≈0 → false 'dead chop' even with strong 1-2 min legs. ER on
    the last ~window samples reflects the CURRENT leg. `window` is AI-tunable so a
    slow grind (coarse) vs fast move (fine) each read correctly."""
    if not samples or len(samples) < REGIME_ER_MIN_SAMPLES:
        return None
    prices = [p for _, p in samples][-window:]
    if len(prices) < REGIME_ER_MIN_SAMPLES:
        return None
    net = abs(prices[-1] - prices[0])
    path = sum(abs(prices[k] - prices[k - 1]) for k in range(1, len(prices)))
    return (net / path) if path > 0 else None


def recent_range_pct(samples: Optional[Deque]) -> Optional[float]:
    if not samples or len(samples) < REGIME_ER_MIN_SAMPLES:
        return None
    prices = [p for _, p in samples]
    lo = min(prices)
    return ((max(prices) - lo) / lo * 100.0) if lo > 0 else None


def short_thrust(samples: Optional[Deque]) -> Optional[Tuple[str, float, float]]:
    if not samples or len(samples) < max(4, THRUST_WINDOW_SAMPLES // 2):
        return None
    prices = [p for _, p in list(samples)[-THRUST_WINDOW_SAMPLES:]]
    if len(prices) < 4:
        return None
    net = prices[-1] - prices[0]
    path = sum(abs(prices[k] - prices[k - 1]) for k in range(1, len(prices)))
    lo = min(prices)
    if path <= 0 or lo <= 0:
        return None
    return ("call" if net > 0 else "put", abs(net) / path, (max(prices) - lo) / lo * 100.0)


def mtf_trend(samples: Optional[Deque]) -> Optional[Tuple[int, float]]:
    """(net_dir, alignment) across short/medium/long horizons of the regime series."""
    if not samples or len(samples) < MTF_MIN_SAMPLES:
        return None
    prices = [p for _, p in samples]
    n = len(prices)
    horizons = [max(3, n // 5), max(4, n // 2), n]
    dirs = []
    for h in horizons:
        seg = prices[-h:]
        if len(seg) < 2 or seg[0] <= 0:
            continue
        chg = (seg[-1] - seg[0]) / seg[0]
        dirs.append(0 if abs(chg) < MTF_FLAT_PCT / 100.0 else (1 if chg > 0 else -1))
    if not dirs:
        return None
    ups, downs = dirs.count(1), dirs.count(-1)
    if ups > downs:
        return 1, ups / len(dirs)
    if downs > ups:
        return -1, downs / len(dirs)
    return 0, max(ups, downs) / len(dirs)


def anchored_mean(anchor_sum: float, anchor_n: int) -> Optional[float]:
    return (anchor_sum / anchor_n) if anchor_n > 0 else None


def anchor_std(anchor_sum: float, anchor_sq: float, anchor_n: int) -> Optional[float]:
    if anchor_n < VWAP_MIN_SAMPLES:
        return None
    mean = anchor_sum / anchor_n
    var = max(0.0, anchor_sq / anchor_n - mean * mean)
    return var ** 0.5


def vwap_z(last: Optional[float], mean: Optional[float], std: Optional[float]) -> Optional[float]:
    if last is None or mean is None or std is None or std <= 0:
        return None
    return (last - mean) / std


def intraday_levels(samples: Optional[Deque]) -> Optional[Tuple[float, float]]:
    if not samples or len(samples) < REGIME_ER_MIN_SAMPLES:
        return None
    prices = [p for _, p in samples]
    return min(prices), max(prices)
