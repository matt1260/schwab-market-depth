"""
Earnings-date filter for single-stock premium selling. Selling a credit spread that
spans an earnings report = selling cheap insurance against a known binary gap — the
opposite of the rich-premium edge. We skip any stock whose next earnings falls before
the spread's expiry. ETFs have no earnings → always pass.

Source: yfinance (free). Cached daily per ticker (earnings dates barely change).
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class EarningsCalendar:
    def __init__(self):
        self._cache: Dict[str, tuple] = {}  # sym -> (date_fetched, next_earnings_date or None)

    def next_earnings(self, sym: str) -> Optional[date]:
        """Next earnings date for `sym`, or None (ETF / unknown / no upcoming). Cached
        for the day."""
        today = date.today()
        hit = self._cache.get(sym)
        if hit and hit[0] == today:
            return hit[1]
        ed = self._fetch(sym)
        self._cache[sym] = (today, ed)
        return ed

    @staticmethod
    def _fetch(sym: str) -> Optional[date]:
        try:
            import yfinance as yf
            cal = yf.Ticker(sym).calendar or {}
            ed = cal.get("Earnings Date")
            if isinstance(ed, list) and ed:
                ed = ed[0]
            if hasattr(ed, "date"):
                ed = ed.date()
            return ed if isinstance(ed, date) else None
        except Exception as e:
            logger.debug("earnings fetch failed %s: %s", sym, e)
            return None

    def clear_before(self, sym: str, expiry: date) -> bool:
        """True if it's SAFE to hold a spread to `expiry` (no earnings before then)."""
        ed = self.next_earnings(sym)
        if ed is None:
            return True  # ETF or unknown-no-upcoming → treat as clear
        return ed > expiry
