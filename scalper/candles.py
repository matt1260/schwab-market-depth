"""
1-minute OHLC candle tracker + price-action reversal signals for the macross overlay.

The MA (8/21) LAGS sharp reversals. Large-body candles and W (double-bottom) /
M (double-top) structures LEAD them — they're the "the move just got real / just
reversed" tells the user reads off the chart. This module builds real 1-min OHLC
bars from ticks and exposes:

  - big_candle(): the just-CLOSED bar's body vs the recent average body (range
    expansion). Returns ('red'|'green', body_ratio) when body >= N x avg, else None.
  - is_W(): recent closed lows form a double-bottom (bull reversal).
  - is_M(): recent closed highs form a double-top (bear reversal).

Only CLOSED bars are used (chart-accurate; no intra-minute noise).
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Optional, Tuple

BIG_BODY_MULT = 1.8      # a "big" candle body is >= this x the recent avg body
BODY_AVG_BARS = 15       # window for the average body
MIN_BODY_PCT = 0.05      # AND body must be >= this % of price (avoids tiny-chop false
                         # positives where a 2-cent bar is "2x" a 1-cent average)
W_LOOKBACK = 12          # bars to scan for a W / M
W_LOW_TOL_PCT = 0.15     # the two lows must be within this % of each other


class Candle:
    __slots__ = ("o", "h", "l", "c", "ts")

    def __init__(self, px: float, ts: int = 0):
        self.o = self.h = self.l = self.c = px
        self.ts = ts   # epoch SECONDS of the bar's open (start of the 1-min window)

    def update(self, px: float):
        self.c = px
        if px > self.h:
            self.h = px
        if px < self.l:
            self.l = px

    @property
    def body(self) -> float:
        return abs(self.c - self.o)

    @property
    def is_green(self) -> bool:
        return self.c >= self.o


class CandleSeries:
    """Per-symbol 1-min OHLC series built from ticks."""

    def __init__(self, tf_seconds: int = 60, maxbars: int = 60):
        self.tf = tf_seconds
        self._bars: Deque[Candle] = deque(maxlen=maxbars)
        self._cur: Optional[Candle] = None
        self._bar_id: Optional[int] = None
        self._just_closed: Optional[Candle] = None  # the bar that closed on the last update

    def update(self, px: float, now) -> Optional[Candle]:
        """Feed a tick. Returns the just-CLOSED candle when a bar rolls over, else None."""
        bar = int(now.timestamp()) // self.tf
        self._just_closed = None
        if self._bar_id is None:
            self._bar_id = bar
            self._cur = Candle(px, bar * self.tf)
            return None
        if bar != self._bar_id:
            self._bars.append(self._cur)          # close the prior bar
            self._just_closed = self._cur
            self._bar_id = bar
            self._cur = Candle(px, bar * self.tf)
            return self._just_closed
        self._cur.update(px)
        return None

    def current(self) -> Optional[Candle]:
        """The still-forming (not yet closed) bar, so the chart can show a live
        last candle updating in real time."""
        return self._cur

    def ohlc_ts_rows(self, n: int = 60) -> list:
        """Last n CLOSED bars as [{t,o,h,l,c}] (oldest→newest) with epoch-second
        timestamps — the chart's candlestick data."""
        return [{"t": b.ts, "o": round(b.o, 2), "h": round(b.h, 2),
                 "l": round(b.l, 2), "c": round(b.c, 2)}
                for b in list(self._bars)[-n:]]

    def seed_bar(self, o, h, l, c, ts: int = 0):
        """Seed a fully-formed historical bar (for warmup from 1-min history)."""
        cd = Candle(o, ts); cd.h = max(h, o, c); cd.l = min(l, o, c); cd.c = c
        self._bars.append(cd)

    def recent_ohlc(self, n: int = 20):
        """Last n CLOSED bars as compact [o,h,l,c] rows (oldest→newest) for the AI's
        tape, so the supervisor has real 1-min OHLC to read structure from."""
        return [[round(b.o, 2), round(b.h, 2), round(b.l, 2), round(b.c, 2)]
                for b in list(self._bars)[-n:]]

    def _avg_body(self) -> Optional[float]:
        bars = list(self._bars)[-BODY_AVG_BARS - 1:-1]  # exclude the most recent (the candidate)
        bodies = [b.body for b in bars if b.body > 0]
        return (sum(bodies) / len(bodies)) if bodies else None

    def bull_pivot(self) -> Optional[str]:
        """Detect a Bull Pivot on the most-recently-CLOSED bar (ThinkScript port).
        Returns 'bull_pivot', 'bull_pivot_2', or None. `[N]` in the script = N bars ago;
        here b(N) reads that bar (b(0) = the just-closed bar). A pivot marks a bullish
        reversal of a 5-min downtrend; its LOW becomes a support level to play calls above.

          bullbar  = (close-open) >= .38  and close>close[2] and close<open[2] and open<close[1]
          bullbar2 = (close-open) >= .38  and close>close[2] and close<open[4]
                     and (open[2]-close[2]) > .20
                     and low[4]>low[1] and low[5]>low[3] and low[6]>low[4]  and !bullbar
        """
        bars = list(self._bars)
        if len(bars) < 7:                 # need up to [6]
            return None
        def b(n): return bars[-1 - n]     # b(0)=most recent closed, b(1)=one before, ...
        c0, o0 = b(0).c, b(0).o
        body = c0 - o0
        bullbar = (body >= 0.38 and c0 > b(2).c and c0 < b(2).o and o0 < b(1).c)
        if bullbar:
            return "bull_pivot"
        bullbar2 = (body >= 0.38
                    and c0 > b(2).c
                    and c0 < b(4).o
                    and (b(2).o - b(2).c) > 0.20
                    and b(4).l > b(1).l
                    and b(5).l > b(3).l
                    and b(6).l > b(4).l)
        if bullbar2:
            return "bull_pivot_2"
        return None

    def last_low(self) -> Optional[float]:
        """Low of the most-recently-CLOSED bar (the support level a pivot establishes)."""
        return self._bars[-1].l if self._bars else None

    def big_candle(self) -> Optional[Tuple[str, float, Candle]]:
        """If the most-recently-CLOSED bar is a big-body candle, return
        (color, body_ratio, candle). Else None. Call after a bar closes."""
        if not self._bars:
            return None
        last = self._bars[-1]
        avg = self._avg_body()
        if avg is None or avg <= 0 or last.body <= 0:
            return None
        ratio = last.body / avg
        big_enough = last.c > 0 and (last.body / last.c * 100.0) >= MIN_BODY_PCT
        if ratio >= BIG_BODY_MULT and big_enough:
            return ("green" if last.is_green else "red", ratio, last)
        return None

    def last_closed(self) -> Optional[Candle]:
        return self._bars[-1] if self._bars else None

    def ema_engulf(self, fast: float, slow: float) -> Optional[str]:
        """A conviction CONTINUATION candle that engulfs BOTH EMAs — the market
        decisively reclaims the moving averages in one bar:
          GREEN opens <= slow AND closes >= fast  → 'call' (strong up reclaim)
          RED   opens >= slow AND closes <= fast  → 'put'  (strong down reclaim)
        Returns the side, else None. (Caller also requires the regime to agree.)"""
        last = self._bars[-1] if self._bars else None
        if last is None or fast is None or slow is None:
            return None
        lo_ema, hi_ema = (slow, fast) if slow <= fast else (fast, slow)
        if last.is_green and last.o <= lo_ema and last.c >= hi_ema and last.body > 0:
            return "call"
        if (not last.is_green) and last.o >= hi_ema and last.c <= lo_ema and last.body > 0:
            return "put"
        return None

    def is_W(self) -> bool:
        """Double-bottom in the last W_LOOKBACK closed bars: two swing lows at ~the
        same level (2nd >= 1st, a higher-or-equal low) with a bounce between, and
        price now above the middle peak = bull reversal confirmed."""
        bars = list(self._bars)[-W_LOOKBACK:]
        if len(bars) < 5:
            return False
        lows = [b.l for b in bars]
        # find two lows within tolerance, separated by a higher bar (the middle peak)
        lo1 = min(lows)
        tol = lo1 * (W_LOW_TOL_PCT / 100.0)
        idxs = [i for i, lv in enumerate(lows) if abs(lv - lo1) <= tol]
        if len(idxs) < 2:
            return False
        a, b = idxs[0], idxs[-1]
        if b - a < 2:
            return False
        mid_peak_h = max(x.h for x in bars[a + 1:b])  # highest high between the two lows
        # confirmed if the middle peaked above the lows AND current price reclaimed the peak
        cur = bars[-1].c
        return mid_peak_h > lo1 + tol and cur >= mid_peak_h

    def is_M(self) -> bool:
        """Double-top (bear reversal): mirror of is_W on highs."""
        bars = list(self._bars)[-W_LOOKBACK:]
        if len(bars) < 5:
            return False
        highs = [b.h for b in bars]
        hi1 = max(highs)
        tol = hi1 * (W_LOW_TOL_PCT / 100.0)
        idxs = [i for i, hv in enumerate(highs) if abs(hv - hi1) <= tol]
        if len(idxs) < 2:
            return False
        a, b = idxs[0], idxs[-1]
        if b - a < 2:
            return False
        mid_trough = min(bars[a + 1:b], key=lambda x: x.l)
        cur = bars[-1].c
        return mid_trough.l < hi1 - tol and cur <= mid_trough.l
