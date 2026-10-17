"""
Premium-selling SCANNER — finds the richest put-credit-spread opportunities across a
universe of liquid ETFs + large-caps, so the spread engine always deploys into the
BEST available premium rather than just watching SPY/QQQ.

Per ticker each cycle:
  1. spot (Schwab quote) + realized vol (Schwab 1yr daily history)
  2. ATM IV (Alpaca chain) → self-computed IV RANK (IVHistory, RV-bootstrap)
  3. best ~30-45 DTE put spread: short ~short_delta, long width below; credit/width
  4. liquidity (bid/ask width on the short leg) + earnings filter (skip stock spreads
     spanning earnings; ETFs always pass)
  5. score = IV-rank weighted, gated by liquidity + earnings + min credit

Output: a ranked list the SpreadEngine can trade (top-N) and the UI can display.
Runs on its own slow cadence (IV/earnings are daily-ish; no need to hammer).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date
from typing import Any, Dict, List, Optional

from .iv_history import IVHistory, realized_vol_from_closes
from .earnings import EarningsCalendar
from .trend import trend_side
from .schwab_feed import occ_to_schwab

logger = logging.getLogger(__name__)

# Curated liquid universe. ETFs = no earnings risk (the safe core); large-caps add
# opportunity but carry the earnings-gap risk the filter guards against.
DEFAULT_ETFS = ["SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "TLT", "XLE", "XLF", "EEM", "FXI", "HYG"]
DEFAULT_STOCKS = ["AAPL", "MSFT", "NVDA", "AMD", "AMZN", "GOOGL", "META", "TSLA", "NFLX", "JPM"]


class PremiumScanner:
    def __init__(self, alpaca, schwab_client_getter, etfs=None, stocks=None):
        self.alpaca = alpaca
        self._client_getter = schwab_client_getter
        self.etfs = etfs if etfs is not None else list(DEFAULT_ETFS)
        self.stocks = stocks if stocks is not None else list(DEFAULT_STOCKS)
        self.iv = IVHistory()
        self.earn = EarningsCalendar()
        # gates / params
        self.short_delta = 0.30
        self.width = 5.0
        self.dte_min, self.dte_max, self.target_dte = 30, 45, 35
        self.min_credit = 0.20
        self.min_ivr = 30.0
        self.max_spread_width_pct = 8.0   # short-leg bid/ask width as % of mid (liquidity)
        self.min_realized_vol = 0.10      # skip ultra-low-RV names: tiny RV inflates the
                                          # IV-vs-RV ratio to junk-high IVR (HYG etc.) and
                                          # the tiny premium isn't worth the tail risk
        self.min_credit_pct_of_width = 8.0  # credit must be >= this % of width (real edge)
        self._results: List[Dict[str, Any]] = []
        self._last_scan_ts = 0.0
        self._scanning = False

    @property
    def universe(self) -> List[str]:
        return self.etfs + self.stocks

    def _spot(self, sym: str) -> Optional[float]:
        c = self._client_getter()
        if c is None:
            return None
        try:
            r = c.quote(sym).json()
            q = (r.get(sym, {}) or {}).get("quote", {}) or {}
            v = q.get("lastPrice") or q.get("mark") or q.get("closePrice")
            return float(v) if v else None
        except Exception:
            return None

    def _closes(self, sym: str) -> list:
        """1yr daily closes (Schwab) — used for BOTH realized vol and the trend bias."""
        c = self._client_getter()
        if c is None:
            return []
        try:
            r = c.price_history(sym, periodType="year", period="1",
                                frequencyType="daily", frequency="1").json()
            return [x.get("close") for x in r.get("candles", []) if x.get("close")]
        except Exception:
            return []

    async def start(self, interval_s: int = 600):
        """Background loop: rescan the universe every interval (IV/earnings move slowly,
        and it accumulates one IV-history sample per ticker per day)."""
        self._interval_s = interval_s
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="premium-scanner")

    async def stop(self):
        self._running = False
        t = getattr(self, "_task", None)
        if t:
            t.cancel()

    async def _loop(self):
        while getattr(self, "_running", False):
            try:
                await asyncio.to_thread(self.scan_once)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("scanner loop failed: %s", e)
            try:
                await asyncio.sleep(getattr(self, "_interval_s", 600))
            except asyncio.CancelledError:
                break

    def scan_once(self) -> List[Dict[str, Any]]:
        """Score the whole universe; return ranked opportunities (best first)."""
        out = []
        for sym in self.universe:
            try:
                row = self._score_symbol(sym)
                if row:
                    out.append(row)
            except Exception as e:
                logger.debug("scan %s failed: %s", sym, e)
        out.sort(key=lambda r: r.get("score", 0), reverse=True)
        self._results = out
        self._last_scan_ts = time.time()
        return out

    def _score_symbol(self, sym: str) -> Optional[Dict[str, Any]]:
        is_etf = sym in self.etfs
        spot = self._spot(sym)
        if not spot:
            return None
        atm_iv = self.alpaca.atm_iv(sym, spot, self.dte_min, self.dte_max)
        if not atm_iv:
            return None
        closes = self._closes(sym)
        rv = realized_vol_from_closes(closes, 20)
        ivr = self.iv.rank(sym, atm_iv, rv)
        rank = (ivr or {}).get("rank")
        # DIRECTIONAL FILTER: sell premium only on the side the TREND protects.
        tr = trend_side(closes)
        side = tr["side"]   # 'put' (uptrend) | 'call' (downtrend) | None (skip)
        # build the trend-aligned spread (put below in uptrend, call above in downtrend)
        spread = None
        if side == "put":
            chain = self.alpaca.get_put_chain(sym, self.dte_min, self.dte_max)
            spread = self._best_spread(chain, "put") if chain else None
        elif side == "call":
            chain = self.alpaca.get_call_chain(sym, self.dte_min, self.dte_max)
            spread = self._best_spread(chain, "call") if chain else None
        # earnings: skip stocks whose earnings fall before the spread's expiry
        earnings_ok = True
        next_earn = None
        if not is_etf:
            next_earn = self.earn.next_earnings(sym)
            if spread and next_earn:
                earnings_ok = next_earn > date.fromisoformat(spread["expiration"])
        # tradeable requires: a trend side, IVR>=min, liquidity, credit, earnings, AND
        # a real-vol floor (skip ultra-low-RV junk-high-IVR) + a credit/width floor
        # (the spread must actually pay enough of its width to be worth the risk).
        score = 0.0
        tradeable = False
        rv_ok = (rv is not None and rv >= self.min_realized_vol)
        if rank is not None and spread is not None and side is not None:
            liq_ok = spread["short_spread_pct"] <= self.max_spread_width_pct
            cred_ok = spread["credit"] >= self.min_credit
            cpw_ok = spread["credit_pct_of_width"] >= self.min_credit_pct_of_width
            ivr_ok = rank >= self.min_ivr
            tradeable = liq_ok and cred_ok and cpw_ok and ivr_ok and earnings_ok and rv_ok
            if tradeable:
                score = rank + (5 if is_etf else 0) + spread["credit_pct_of_width"] * 0.2
        return {
            "symbol": sym, "is_etf": is_etf, "spot": round(spot, 2),
            "atm_iv": round(atm_iv, 3), "ivr": rank, "ivr_method": (ivr or {}).get("method"),
            "side": side, "trend": tr["trend"], "ma_fast": tr["ma_fast"], "ma_slow": tr["ma_slow"],
            "realized_vol": round(rv, 3) if rv is not None else None,
            "next_earnings": next_earn.isoformat() if next_earn else None,
            "earnings_ok": earnings_ok,
            "spread": spread, "tradeable": tradeable, "score": round(score, 1),
        }

    def _best_spread(self, chain: List[Dict], side: str) -> Optional[Dict[str, Any]]:
        """Best ~target_dte credit spread for `side`. PUT: short ~delta put, long
        `width` BELOW. CALL: short ~delta call, long `width` ABOVE."""
        by_exp: Dict[str, List[Dict]] = {}
        for c in chain:
            if c.get("expiration"):
                by_exp.setdefault(c["expiration"], []).append(c)
        if not by_exp:
            return None
        exp = min(by_exp, key=lambda e: abs((by_exp[e][0].get("dte") or 999) - self.target_dte))
        legs = sorted(by_exp[exp], key=lambda c: c["strike"])
        short = min(legs, key=lambda c: abs(abs(c["delta"]) - self.short_delta))
        if side == "put":
            wing = [c for c in legs if c["strike"] < short["strike"]]   # long BELOW
            target = short["strike"] - self.width
        else:
            wing = [c for c in legs if c["strike"] > short["strike"]]   # long ABOVE
            target = short["strike"] + self.width
        if not wing:
            return None
        longp = min(wing, key=lambda c: abs(c["strike"] - target))
        width = abs(short["strike"] - longp["strike"])
        if width <= 0:
            return None
        credit = round((short["mid"] or 0) - (longp["mid"] or 0), 2)
        short_mid = short["mid"] or 0
        short_spread_pct = ((short["ask"] - short["bid"]) / short_mid * 100.0) if (short_mid > 0 and short["ask"] and short["bid"]) else 999
        return {
            "side": side, "expiration": exp, "dte": short.get("dte"),
            "short_strike": short["strike"], "long_strike": longp["strike"],
            "short_delta": round(short["delta"], 3), "width": width, "credit": credit,
            "credit_pct_of_width": round(credit / width * 100.0, 1) if width else 0,
            "max_loss": round((width - credit) * 100, 0), "max_win": round(credit * 100, 0),
            "short_spread_pct": round(short_spread_pct, 1),
        }

    def results(self) -> Dict[str, Any]:
        return {
            "last_scan_age_s": round(time.time() - self._last_scan_ts, 0) if self._last_scan_ts else None,
            "universe_size": len(self.universe),
            "opportunities": self._results,
            "top_tradeable": [r for r in self._results if r.get("tradeable")][:5],
        }
