"""
IV Rank via the VIX/VXN proxy — the entry edge for premium selling.

The documented edge in selling options is the Volatility Risk Premium: only sell
when implied vol is EXPENSIVE relative to its own recent history. IV Rank does
that — (current - 1yr_low) / (1yr_high - 1yr_low) * 100. We don't have a year of
per-option IV, so we proxy the underlying's IV with its volatility index:
  SPY → $VIX   (VIX *is* SPY's 30-day implied vol)
  QQQ → $VXN   (Nasdaq-100 equivalent)

Data comes from Schwab (verified reachable): client.quote('$VIX') for the current
level, client.price_history('$VIX', year/1/daily) for the 1-yr range. Daily data,
so we cache for an hour — no need to hammer the REST API every tick.

Convention: rank 0 = at 1-yr low (cheap, DON'T sell), 100 = at 1-yr high (rich,
SELL). The premium-selling gate wants rank ABOVE a threshold (e.g. 30-50).
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# underlying → its volatility index symbol (plain '$VIX' works; '$VIX.X' errors)
VOL_INDEX = {"SPY": "$VIX", "QQQ": "$VXN"}

_CACHE_TTL_S = 3600  # daily data — refresh hourly is plenty


class IVRank:
    """Computes + caches IV Rank for each underlying off its vol index.

    Holds no Schwab client of its own — the engine passes in the feed's live
    schwabdev client (REST quote/price_history are safe alongside the stream)."""

    def __init__(self):
        # sym -> (computed_at_epoch, dict result)
        self._cache: Dict[str, Tuple[float, Dict]] = {}

    def get(self, sym: str, client) -> Optional[Dict]:
        """Return {index, current, low, high, rank, percentile} for `sym`, or None.
        `client` is a schwabdev Client. Cached for _CACHE_TTL_S."""
        idx = VOL_INDEX.get(sym)
        if idx is None or client is None:
            return None
        now = time.time()
        hit = self._cache.get(sym)
        if hit and (now - hit[0]) < _CACHE_TTL_S:
            return hit[1]
        res = self._compute(idx, client)
        if res is not None:
            self._cache[sym] = (now, res)
        elif hit:
            return hit[1]  # serve stale on a transient fetch failure
        return res

    def _compute(self, idx: str, client) -> Optional[Dict]:
        try:
            r = client.price_history(idx, periodType="year", period="1",
                                     frequencyType="daily", frequency="1").json()
            closes = [c.get("close") for c in r.get("candles", []) if c.get("close") is not None]
            if len(closes) < 30:
                logger.debug("IVRank %s: too few candles (%d)", idx, len(closes))
                return None
            # current level: prefer the live quote, but only if it's SANE vs the last
            # daily close (guards against a stale/bad index print falsely spiking the
            # rank and triggering a sell). Otherwise fall back to the last close.
            last_close = closes[-1]
            q = self._quote_level(idx, client)
            if q is not None and last_close > 0 and abs(q - last_close) / last_close <= 0.25:
                current = q
            else:
                if q is not None:
                    logger.warning("IVRank %s: quote %.2f deviates >25%% from last close %.2f — using close",
                                   idx, q, last_close)
                current = last_close
            lo, hi = min(closes), max(closes)
            rank = ((current - lo) / (hi - lo) * 100.0) if hi > lo else None
            # IV percentile = % of days trading BELOW current (the other common metric)
            below = sum(1 for c in closes if c < current)
            pct = below / len(closes) * 100.0
            return {
                "index": idx,
                "current": round(current, 2),
                "low": round(lo, 2),
                "high": round(hi, 2),
                "rank": round(rank, 1) if rank is not None else None,
                "percentile": round(pct, 1),
                "n": len(closes),
            }
        except Exception as e:
            logger.warning("IVRank %s compute failed: %s", idx, e)
            return None

    @staticmethod
    def _quote_level(idx: str, client) -> Optional[float]:
        try:
            r = client.quote(idx).json()
            q = (r.get(idx, {}) or {}).get("quote", {}) or {}
            v = q.get("lastPrice") or q.get("mark") or q.get("closePrice")
            return float(v) if v else None
        except Exception:
            return None
