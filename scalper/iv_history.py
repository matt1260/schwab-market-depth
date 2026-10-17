"""
Self-computed IV Rank for ANY ticker (no VIX-style index needed) — the scanner's
edge signal. For each ticker we read the current ATM ~30-45 DTE implied vol from the
options chain and rank it against that ticker's OWN trailing IV history, which we
accumulate daily to SQLite.

Bootstrap problem: a true IV rank needs ~months of IV history we don't have yet. So
until enough IV samples accrue, we fall back to an **IV-vs-realized-vol** estimate
(current IV relative to the stock's recent realized volatility from daily price
history) — a direct read of the volatility-risk-premium that's available on day one.
As IV history builds, we transition to the real self-computed IV rank.

  rank 0  = IV at/below its own range floor (cheap — DON'T sell)
  rank 100 = IV at its range ceiling (rich — SELL)
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
import time
from datetime import date
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "iv_history.db")

MIN_SAMPLES_FOR_RANK = 20   # need this many daily IV samples before trusting self-rank
_CACHE_TTL_S = 1800         # 30 min — IV doesn't move that fast intraday for ranking


class IVHistory:
    """Accumulates daily ATM IV per ticker and computes a self-referential IV rank,
    with a realized-vol bootstrap for the cold-start period."""

    def __init__(self):
        self._init_db()
        self._cache: Dict[str, tuple] = {}  # sym -> (ts, result)

    def _init_db(self):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        c = sqlite3.connect(DB_PATH)
        c.execute("""CREATE TABLE IF NOT EXISTS iv_samples (
            sym TEXT, d TEXT, iv REAL, PRIMARY KEY (sym, d))""")
        c.commit(); c.close()

    # ── daily accumulation ────────────────────────────────────────────────
    def record(self, sym: str, atm_iv: float):
        """Store today's ATM IV for `sym` (one row per day, last write wins)."""
        if not atm_iv or atm_iv <= 0:
            return
        try:
            c = sqlite3.connect(DB_PATH)
            c.execute("INSERT OR REPLACE INTO iv_samples (sym,d,iv) VALUES (?,?,?)",
                      (sym, date.today().isoformat(), float(atm_iv)))
            c.commit(); c.close()
        except Exception as e:
            logger.debug("iv record failed %s: %s", sym, e)

    def _history(self, sym: str) -> List[float]:
        try:
            c = sqlite3.connect(DB_PATH)
            rows = c.execute("SELECT iv FROM iv_samples WHERE sym=? ORDER BY d", (sym,)).fetchall()
            c.close()
            return [r[0] for r in rows if r[0]]
        except Exception:
            return []

    # ── rank computation ──────────────────────────────────────────────────
    def rank(self, sym: str, current_iv: float, realized_vol: Optional[float] = None) -> Optional[Dict]:
        """Return {rank, method, current_iv, ...}. Records the sample, then ranks:
        - if >= MIN_SAMPLES_FOR_RANK daily IV samples → true self IV-rank (range-based)
        - else → realized-vol bootstrap: IV vs realized (premium ratio mapped to 0-100)."""
        if not current_iv or current_iv <= 0:
            return None
        self.record(sym, current_iv)
        hist = self._history(sym)
        if len(hist) >= MIN_SAMPLES_FOR_RANK:
            lo, hi = min(hist), max(hist)
            rank = ((current_iv - lo) / (hi - lo) * 100.0) if hi > lo else 50.0
            return {"rank": round(rank, 1), "method": "iv_history", "current_iv": round(current_iv, 4),
                    "iv_low": round(lo, 4), "iv_high": round(hi, 4), "n": len(hist)}
        # bootstrap: IV vs realized vol. IV/RV ~1 = fairly priced; >1.3 = rich premium.
        if realized_vol and realized_vol > 0:
            ratio = current_iv / realized_vol
            # map ratio 0.9..1.6 → rank 0..100 (premium gets rich as IV exceeds RV)
            rank = max(0.0, min(100.0, (ratio - 0.9) / (1.6 - 0.9) * 100.0))
            return {"rank": round(rank, 1), "method": "iv_vs_rv", "current_iv": round(current_iv, 4),
                    "realized_vol": round(realized_vol, 4), "iv_rv_ratio": round(ratio, 2), "n": len(hist)}
        # no history, no RV → can't rank
        return {"rank": None, "method": "insufficient_data", "current_iv": round(current_iv, 4), "n": len(hist)}


def realized_vol_from_closes(closes: List[float], window: int = 20) -> Optional[float]:
    """Annualized realized volatility from daily closes (close-to-close stdev)."""
    if not closes or len(closes) < window + 1:
        return None
    rets = []
    for i in range(len(closes) - window, len(closes)):
        if i <= 0 or closes[i - 1] <= 0:
            continue
        rets.append(math.log(closes[i] / closes[i - 1]))
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252)  # annualized
