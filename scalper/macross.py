"""
High-frequency MA-crossover scalp strategy (the 'macross' signal_mode).

Goal: 60+ trades/day. Mechanical, always-participating:
  ENTRY  — fast EMA crosses slow EMA on a 1-min timeframe → enter with the cross
           (up-cross = call, down-cross = put). Fires both directions, frequently.
  EXIT   — tight target normally, but TRAIL runners with a trail that DYNAMICALLY
           TIGHTENS as price extends from VWAP / momentum accelerates, so we exit
           just before the EMAs cross back the other way (capture the move, bail
           before the reversal gives it back).

State is per-symbol EMAs updated every tick; the cross is detected on the EMA
spread sign-flip. Pure + cheap — no LLM in the hot path (AI is a light overlay).
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

# Defaults (all operator/AI-tunable via the engine).
FAST_EMA = 8        # ~ minutes-equivalent on the tick stream (see alpha calc)
SLOW_EMA = 21
EMA_TF_SECONDS = 60  # the "1-min timeframe" — EMAs are scaled to this horizon
MIN_SPREAD_PCT = 0.0  # min |fast-slow|/price to count a cross (0 = any cross)


class EmaPair:
    """Classic N-period EMA pair on CLOSED tf-second (1-min) bars — computed exactly
    like a charting platform's 8/21 so the engine's cross matches what you see on the
    chart. Ticks aggregate into a forming bar; on each bar CLOSE the EMAs advance via
    the standard formula ema += k*(close-ema), k = 2/(N+1). The regime/cross only
    change on closed bars (no intra-minute wobble), which is the whole point."""

    def __init__(self, fast: int, slow: int, tf_seconds: int):
        self.fast_n = fast
        self.slow_n = slow
        self.tf = tf_seconds
        self.fast: Optional[float] = None       # fast EMA on CLOSED bars
        self.slow: Optional[float] = None       # slow EMA on CLOSED bars
        self._k_fast = 2.0 / (fast + 1)
        self._k_slow = 2.0 / (slow + 1)
        self._bar_id: Optional[int] = None      # epoch // tf of the forming bar
        self._bar_close: Optional[float] = None # latest price in the forming bar
        self._prev_spread: Optional[float] = None  # fast-slow on the prior CLOSED bar
        from collections import deque
        self._slow_hist = deque(maxlen=32)      # slow EMA over recent CLOSED bars (for slope);
                                                # 32 so slow_slope(N) works up to N~30 (APEX trend)

    def seed_from_closes(self, closes, now=None) -> None:
        """Warm the EMAs instantly from historical 1-min bar CLOSES (Schwab history)
        so the engine matches the chart from the first tick — no ~20-min warmup. Uses
        the same classic EMA recursion, seeded with an SMA of the first `slow` bars."""
        closes = [c for c in (closes or []) if c]
        if len(closes) < self.slow_n:
            return
        self.fast = self.slow = sum(closes[:self.slow_n]) / self.slow_n
        for px in closes[self.slow_n:]:
            self.fast += self._k_fast * (px - self.fast)
            self.slow += self._k_slow * (px - self.slow)
            self._slow_hist.append(self.slow)   # warm the slope history from history
        self._prev_spread = self.fast - self.slow
        # align the forming-bar id to now so live ticks continue cleanly
        if now is not None:
            self._bar_id = int(now.timestamp()) // self.tf
            self._bar_close = closes[-1]

    def update(self, price: float, now) -> None:
        """Feed a tick. When the tf-second bar rolls over, close the prior bar into
        the EMAs (chart-accurate). `now` is a datetime."""
        bar = int(now.timestamp()) // self.tf
        if self._bar_id is None:
            self._bar_id = bar
            self._bar_close = price
            return
        if bar != self._bar_id:
            # the prior bar just CLOSED at self._bar_close → advance the EMAs
            close = self._bar_close
            if self.fast is None:
                self.fast = self.slow = close
            else:
                self.fast += self._k_fast * (close - self.fast)
                self.slow += self._k_slow * (close - self.slow)
            self._slow_hist.append(self.slow)   # record slow EMA per closed bar (slope)
            self._bar_id = bar
        self._bar_close = price  # keep updating the forming bar's last price

    def slow_slope(self, bars: int = 5) -> Optional[str]:
        """Dominant-trend bias from the SLOPE of the SLOW (21) EMA over the last `bars`
        closed bars: 'call' if rising (uptrend), 'put' if falling (downtrend), None if
        flat/insufficient. Used so macross only trades WITH the trend and ignores the
        counter-trend 8/21 flickers on intra-trend bounces."""
        h = list(self._slow_hist)
        if len(h) < bars + 1:
            return None
        change = h[-1] - h[-1 - bars]
        if change > 0:
            return "call"
        if change < 0:
            return "put"
        return None

    def spread(self) -> Optional[float]:
        if self.fast is None or self.slow is None:
            return None
        return self.fast - self.slow

    def cross(self, price: float) -> Optional[str]:
        """Return 'call' on an up-cross, 'put' on a down-cross, else None.
        Detected as the sign flip of (fast-slow) between consecutive ticks."""
        sp = self.spread()
        if sp is None:
            return None
        out = None
        if self._prev_spread is not None and price > 0:
            min_sp = MIN_SPREAD_PCT / 100.0 * price
            if self._prev_spread <= 0 < sp and abs(sp) >= min_sp:
                out = "call"
            elif self._prev_spread >= 0 > sp and abs(sp) >= min_sp:
                out = "put"
        self._prev_spread = sp
        return out

    def spread_dir(self) -> Optional[str]:
        """Current EMA regime: 'call' if fast>slow, 'put' if fast<slow."""
        sp = self.spread()
        if sp is None:
            return None
        return "call" if sp > 0 else "put"

    def spread_pct(self) -> Optional[float]:
        """|fast-slow| as a % of the slow EMA — the CONVICTION of the regime. A tiny
        value means the EMAs are kissing (a marginal cross that easily reverts); a
        larger value means real separation (an established trend)."""
        sp = self.spread()
        if sp is None or not self.slow:
            return None
        return abs(sp) / self.slow * 100.0


class EmaTriple:
    """Triple-EMA (9/21/50) system on CLOSED tf-second (1-min) bars — the mechanical
    MA scalper's signal engine.

      ENTRY (9/21/50 confluence):
        - CALL: EMA21 crosses UP through EMA50  AND  EMA9 > EMA21
        - PUT : EMA21 crosses DOWN through EMA50 AND  EMA9 < EMA21
      EXIT (9/21 opposite cross):
        - a CALL exits when EMA9 crosses BELOW EMA21
        - a PUT  exits when EMA9 crosses ABOVE EMA21

    All EMAs advance only on CLOSED bars (chart-accurate). The 21×50 and 9×21 cross
    events are computed ONCE per bar-close and cached, so multiple callers in a tick
    read the same event (calling a live cross-detector twice would miss the second)."""

    def __init__(self, tf_seconds: int, n_fast: int = 9, n_mid: int = 21, n_slow: int = 50):
        self.tf = tf_seconds
        self.n_fast, self.n_mid, self.n_slow = n_fast, n_mid, n_slow
        self.ema9: Optional[float] = None
        self.ema21: Optional[float] = None
        self.ema50: Optional[float] = None
        self._k9 = 2.0 / (n_fast + 1)
        self._k21 = 2.0 / (n_mid + 1)
        self._k50 = 2.0 / (n_slow + 1)
        self._bar_id: Optional[int] = None
        self._bar_close: Optional[float] = None
        self._prev_2150: Optional[float] = None   # (ema21-ema50) on the prior closed bar
        self._prev_921: Optional[float] = None     # (ema9-ema21) on the prior closed bar
        # cross events detected on the most recent bar CLOSE ('call'/'put'/None):
        self.cross_2150: Optional[str] = None      # 21×50 direction of the just-closed cross
        self.cross_921: Optional[str] = None       # 9×21 direction of the just-closed cross
        # leg tracking (restart-robust entry): the current 21×50 regime is a "leg"; we
        # allow ONE entry per leg on the first confirmed confluence tick.
        self._leg_dir: Optional[str] = None        # '_call'/'put' of the current 21×50 leg
        self._leg_entered = False                  # have we already entered this leg?
        # min |21-50| separation (% of price) for a regime to count as a real leg — a
        # kiss below this is chop, not a trend, and must NOT reset the leg / fire entries.
        self.min_sep_pct = 0.03
        # Per-closed-bar (9,21,50) snapshots, aligned with CandleSeries closed bars,
        # for the chart's EMA overlay lines.
        from collections import deque as _dq
        self._tri_hist = _dq(maxlen=120)

    def _advance(self, close: float) -> None:
        if self.ema9 is None:
            self.ema9 = self.ema21 = self.ema50 = close
        else:
            self.ema9 += self._k9 * (close - self.ema9)
            self.ema21 += self._k21 * (close - self.ema21)
            self.ema50 += self._k50 * (close - self.ema50)
        # detect crosses vs the prior closed bar's spreads
        s2150 = self.ema21 - self.ema50
        s921 = self.ema9 - self.ema21
        self.cross_2150 = None
        if self._prev_2150 is not None:
            if self._prev_2150 <= 0 < s2150:
                self.cross_2150 = "call"
            elif self._prev_2150 >= 0 > s2150:
                self.cross_2150 = "put"
        self.cross_921 = None
        if self._prev_921 is not None:
            if self._prev_921 <= 0 < s921:
                self.cross_921 = "call"
            elif self._prev_921 >= 0 > s921:
                self.cross_921 = "put"
        self._prev_2150 = s2150
        self._prev_921 = s921
        self._tri_hist.append((self.ema9, self.ema21, self.ema50))

    def seed_from_closes(self, closes, now=None) -> None:
        """Warm all three EMAs from 1-min history (SMA seed of the first n_slow bars).

        The current 21×50 LEG is left UN-entered so entry_signal can take it once after
        a (re)seed — this is what makes entries survive a restart that would otherwise
        eat the one-bar cross event. A stale/exhausted trend entered this way is capped
        quickly by the engine's EMA-50 stop, so we don't need a 'how recent was the
        cross' heuristic here."""
        closes = [c for c in (closes or []) if c]
        if len(closes) < self.n_slow:
            return
        seed = sum(closes[:self.n_slow]) / self.n_slow
        self.ema9 = self.ema21 = self.ema50 = seed
        self._tri_hist.clear()
        for px in closes[self.n_slow:]:
            self.ema9 += self._k9 * (px - self.ema9)
            self.ema21 += self._k21 * (px - self.ema21)
            self.ema50 += self._k50 * (px - self.ema50)
            self._tri_hist.append((self.ema9, self.ema21, self.ema50))
        self._prev_2150 = self.ema21 - self.ema50
        self._prev_921 = self.ema9 - self.ema21
        self.cross_2150 = None
        self.cross_921 = None
        self._leg_dir = self.regime_2150()   # know the current leg…
        self._leg_entered = False            # …but leave it enterable once (restart-safe)
        if now is not None:
            self._bar_id = int(now.timestamp()) // self.tf
            self._bar_close = closes[-1]

    def update(self, price: float, now) -> None:
        """Feed a tick; on bar rollover, close the prior bar into the EMAs. Cross
        events are refreshed on that close and stay set until the NEXT bar close."""
        bar = int(now.timestamp()) // self.tf
        if self._bar_id is None:
            self._bar_id = bar
            self._bar_close = price
            return
        if bar != self._bar_id:
            self._advance(self._bar_close)   # advance + refresh cross events
            self._bar_id = bar
        else:
            # same bar still forming — no new closed bar, so no fresh cross event
            self.cross_2150 = None
            self.cross_921 = None
        self._bar_close = price

    def entry_signal(self) -> Optional[str]:
        """9/21/50 confluence entry — the FAST-9-LED STACK (operator definition).

        The 9 is the responsive line, so we trigger on IT stacking, not on the slow
        21×50 cross (which lags a fast pop by minutes — it confirms a move that already
        ran). The confluence is a full EMA STACK led by the 9:
          CALL: 9 > 21 > 50   (the 9 has crossed up through BOTH the 21 and the 50)
          PUT : 9 < 21 < 50   (the 9 has crossed down through both)
        This catches the move as the 9 leads it (e.g. the QQQ 10:24-10:25 pop the old
        21×50 logic missed by 3 min). Fires ONCE per stack 'leg' — resets when the stack
        breaks (9 no longer on the leading side of both). Restart-robust (a re-seed
        reconstructs the current stack as a leg). `leg_traded()` marks it entered.

        Separation gate: require the 9 to be meaningfully clear of the 50 (|9-50| >=
        min_sep_pct of price) so a KISS (all three EMAs coiled together in chop) doesn't
        fire — that was the machine-gun churn on low-ER days."""
        if self.ema9 is None or self.ema21 is None or self.ema50 is None:
            return None
        # 9 LEADS through BOTH the 21 and the 50 (the operator's definition — the 9
        # crossing the 50 then the 21). We do NOT require 21>50: the slow 21×50 order
        # lags a fast pop by minutes; the 9 clearing both lines IS the early confluence.
        if self.ema9 > self.ema21 and self.ema9 > self.ema50:
            stack = "call"
        elif self.ema9 < self.ema21 and self.ema9 < self.ema50:
            stack = "put"
        else:
            stack = None                     # 9 not clear of both → no confluence yet
        # SEPARATION GATE — keyed to the leading 9-vs-50 distance; a coiled kiss (all
        # three EMAs on top of each other in chop) must NOT fire the machine-gun.
        sep_pct = abs(self.ema9 - self.ema50) / self.ema50 * 100.0 if self.ema50 else 0.0
        if stack is None or sep_pct < self.min_sep_pct:
            # stack broke or coiled — reset the leg so the NEXT clean stack can fire
            if stack != self._leg_dir:
                self._leg_dir = stack
                self._leg_entered = False
            return None
        # a real, separated stack: detect a NEW leg and fire once
        if stack != self._leg_dir:
            self._leg_dir = stack
            self._leg_entered = False
        if self._leg_entered:
            return None                      # already took this stack-leg
        return stack

    def leg_traded(self) -> None:
        """Mark the current 21×50 leg as entered (call after opening a position) so it
        won't re-fire until 21 crosses 50 the other way (a fresh leg)."""
        self._leg_entered = True

    def exit_signal(self, direction: str) -> bool:
        """9/21 opposite cross closes the trade: call exits on 9-below-21 (cross_921
        'put'); put exits on 9-above-21 (cross_921 'call')."""
        if direction == "call":
            return self.cross_921 == "put"
        if direction == "put":
            return self.cross_921 == "call"
        return False

    def regime_2150(self) -> Optional[str]:
        """Current 21-vs-50 regime ('call' if 21>50 else 'put')."""
        if self.ema21 is None or self.ema50 is None:
            return None
        return "call" if self.ema21 > self.ema50 else "put"

    def values(self) -> dict:
        """Current EMA values (for the chart's live last point + status)."""
        return {
            "ema9": round(self.ema9, 3) if self.ema9 is not None else None,
            "ema21": round(self.ema21, 3) if self.ema21 is not None else None,
            "ema50": round(self.ema50, 3) if self.ema50 is not None else None,
            "regime": self.regime_2150(),
        }

    def tri_hist_rows(self, n: int = 60) -> list:
        """Recent per-closed-bar (9,21,50) EMA values (oldest→newest), aligned
        with CandleSeries closed bars — the chart's EMA overlay lines."""
        return [{"ema9": round(a, 3), "ema21": round(b, 3), "ema50": round(c, 3)}
                for a, b, c in list(self._tri_hist)[-n:]]


def adaptive_trail_giveback(base_giveback_pct: float, vwap_z: Optional[float],
                            floor_pct: float = 0.03) -> float:
    """Tighten the trailing give-back as the move EXTENDS from VWAP.

    At VWAP (z≈0) use the full base give-back (let it breathe). As |z| grows
    (move stretched / likely near exhaustion), shrink the give-back toward the
    floor so we lock the move and exit just before the EMAs cross back.

    z=0 → base; |z|=1 → ~0.6*base; |z|>=2.5 → floor. Smooth, clamped.
    """
    if vwap_z is None:
        return base_giveback_pct
    import math
    stretch = min(abs(vwap_z), 2.5) / 2.5      # 0..1
    g = base_giveback_pct * (1.0 - 0.8 * stretch)  # shrink up to 80%
    return max(floor_pct, g)
