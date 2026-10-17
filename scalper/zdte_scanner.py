"""0DTE credit-spread scanner — ranked by PROBABILITY OF EXPIRING OTM.

For a credit seller, the probability the SHORT strike finishes OUT of the money is the
probability of profit. An option's delta closely approximates its probability of
finishing in the money, so:

        P(expire OTM)  ≈  1 − |delta of the short strike|

For each symbol and each side (put / call), we pick the short strike CLOSEST to the
money that still meets the target P(OTM) — that strike collects the MOST premium for
the desired win probability — then buy a long leg `width` beyond it for defined risk.
Each candidate is ranked by P(OTM) (primary) then credit-as-%-of-width.

Scanner ONLY — it ranks and displays; it does not place trades.
"""

from __future__ import annotations

import time
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("zdte_scanner")

# ETFs/indices with reliable DAILY (0DTE) expirations + deep option liquidity.
DEFAULT_UNIVERSE = ["SPY", "QQQ", "IWM", "DIA"]


class ZdteScanner:
    def __init__(self, alpaca, schwab_client_getter=None, universe: Optional[List[str]] = None):
        self.alpaca = alpaca
        self._client_getter = schwab_client_getter or (lambda: None)
        self.universe = universe or list(DEFAULT_UNIVERSE)
        # tunables
        self.pop_min = 0.75        # min probability of expiring OTM (1 − |delta|)
        self.width = 1.0           # strike width $ (defined risk; 0DTE strikes are $1)
        self.max_dte = 1           # search window: 0 (today) + this. We then use the
                                   # NEAREST expiration found — so it works pre-market /
                                   # after a same-day expiry passes ("0DTE" = shortest-dated).
        self.min_credit = 0.05     # skip spreads that collect less than this ($/share)
        self.min_credit_pct = 5.0  # skip if credit < this % of width (bad risk/reward)
        self.sides = ("put", "call")
        self._results: List[Dict[str, Any]] = []
        self._last_scan_ts: float = 0.0
        self._note: Optional[str] = None

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

    def scan_once(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for sym in self.universe:
            spot = self._spot(sym)
            for side in self.sides:
                try:
                    row = self._best_spread(sym, side, spot)
                except Exception as e:
                    logger.debug("zdte %s %s failed: %s", sym, side, e)
                    row = None
                if row:
                    out.append(row)
        # rank: highest probability of profit first, then best credit/width
        out.sort(key=lambda r: (r["pop"], r["credit_pct_of_width"]), reverse=True)
        self._results = out
        self._last_scan_ts = time.time()
        self._note = None if out else "no 0DTE spreads met the P(OTM)/credit filters"
        return out

    def _best_spread(self, sym: str, side: str, spot: Optional[float]) -> Optional[Dict[str, Any]]:
        """Best 0DTE credit spread for `side`: short = closest-to-money strike with
        P(OTM) ≥ pop_min (max credit at the target win probability); long = `width`
        further OTM for defined risk."""
        get = self.alpaca.get_call_chain if side == "call" else self.alpaca.get_put_chain
        chain = get(sym, 0, self.max_dte) or []          # today + a small window
        if not chain:
            return None
        # use the NEAREST expiration present (0DTE if it exists, else the soonest)
        nearest = min(c.get("dte", 999) for c in chain)
        legs = sorted((c for c in chain if c.get("dte") == nearest), key=lambda c: c["strike"])
        if not legs:
            return None
        max_short_delta = 1.0 - self.pop_min             # |delta| ceiling for the short

        # short candidates: OUT of the money with P(OTM) ≥ pop_min.
        # put: OTM = strike < spot; call: OTM = strike > spot. Delta sign differs by side,
        # so gate on |delta|. Among those, take the LARGEST |delta| (closest to the money
        # = most premium) — that's the highest-credit strike still meeting the win prob.
        cands = []
        for c in legs:
            d = abs(c.get("delta") or 0)
            if d <= 0 or d > max_short_delta:
                continue
            if spot is not None:
                if side == "put" and c["strike"] >= spot:      # puts must be below spot
                    continue
                if side == "call" and c["strike"] <= spot:     # calls must be above spot
                    continue
            cands.append(c)
        if not cands:
            return None
        short = max(cands, key=lambda c: abs(c["delta"]))       # closest to money → most credit

        # long leg: `width` further OTM (put: below short; call: above short)
        target = short["strike"] - self.width if side == "put" else short["strike"] + self.width
        wing = [c for c in legs if (c["strike"] < short["strike"] if side == "put"
                                    else c["strike"] > short["strike"])]
        if not wing:
            return None
        longp = min(wing, key=lambda c: abs(c["strike"] - target))
        width = abs(short["strike"] - longp["strike"])
        if width <= 0:
            return None
        credit = round((short.get("mid") or 0) - (longp.get("mid") or 0), 2)
        cpw = round(credit / width * 100.0, 1) if width else 0.0
        if credit < self.min_credit or cpw < self.min_credit_pct:
            return None
        pop = round(1.0 - abs(short["delta"]), 3)                # probability of expiring OTM
        short_mid = short.get("mid") or 0
        spr_pct = ((short["ask"] - short["bid"]) / short_mid * 100.0) if (
            short_mid > 0 and short.get("ask") and short.get("bid")) else 999
        # breakeven for the short-strike seller
        breakeven = (short["strike"] - credit) if side == "put" else (short["strike"] + credit)
        return {
            "symbol": sym, "side": side, "spot": round(spot, 2) if spot else None,
            "expiration": short.get("expiration"),
            "short_strike": short["strike"], "long_strike": longp["strike"],
            "short_delta": round(short["delta"], 3),
            "pop": pop, "pop_pct": round(pop * 100, 1),
            "width": width, "credit": credit, "credit_pct_of_width": cpw,
            "max_win": round(credit * 100, 0), "max_loss": round((width - credit) * 100, 0),
            "breakeven": round(breakeven, 2),
            "short_iv": round(short.get("iv"), 3) if short.get("iv") else None,
            "liquidity_pct": round(spr_pct, 1),
        }

    def set_params(self, **kw) -> Dict[str, Any]:
        applied = {}
        for k in ("pop_min", "width", "min_credit", "min_credit_pct"):
            if k in kw and kw[k] is not None:
                try:
                    setattr(self, k, float(kw[k])); applied[k] = getattr(self, k)
                except (TypeError, ValueError):
                    pass
        return applied

    def results(self) -> Dict[str, Any]:
        return {
            "last_scan_age_s": round(time.time() - self._last_scan_ts, 0) if self._last_scan_ts else None,
            "universe": self.universe,
            "params": {"pop_min": self.pop_min, "width": self.width,
                       "min_credit": self.min_credit, "min_credit_pct": self.min_credit_pct},
            "note": self._note,
            "opportunities": self._results,
        }
