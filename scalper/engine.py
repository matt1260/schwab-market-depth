"""
Standalone scalper engine — the heart, ported from the SwarmTrade platform
scalper (backend/queen/scalper_loop.py) into a self-contained app.

Data in:  Schwab L1 (NBBO mid → underlying price series) + L2 (book_imbalance),
          fed by the app's existing Schwab streamer.
Signals:  indicators.py (ER, MTF, VWAP-z, short-thrust) + the gate sequence.
Exec:     alpaca_exec.py (paper options orders).
Brain:    supervisor.py (Opus, mode + param tuning + strategy spec) when mode=ai.
State:    in-memory + SQLite (scalper.db) for the UI + restart survival.

Mirrors the platform's gates exactly: chop guard, velocity gate, direction lock,
score vs entry_threshold, regime filter, 2-tick confirm, trailing/profit/stop/
time-stop exits, kill switch, profit lock.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from collections import deque, Counter
from datetime import datetime, date, timedelta, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

from . import indicators as ind
from .alpaca_exec import AlpacaExec
from . import supervisor as sup

logger = logging.getLogger(__name__)

_OCC_RE = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")


def _r3(x):
    return round(x, 3) if isinstance(x, (int, float)) else x

_TUNE_SAFE_BANDS: Dict[str, Tuple[float, float]] = {
    "profit_target_pct": (0.05, 0.60),
    "stop_pct": (0.05, 0.35),
    "time_stop_seconds": (60, 900),
    "entry_threshold": (0.0, 0.60),
    "chop_min_efficiency": (0.0, 1.0),
    "min_recent_range_pct": (0.0, 1.5),
    "profit_lock_arm_usd": (0.0, 5000.0),
    "profit_giveback_frac": (0.05, 0.95),
}
_TUNE_BOOL_KEYS = ("chop_guard_enabled", "velocity_gate_enabled")
_TUNE_INT_KEYS = ("time_stop_seconds",)
_DSL_OPS = {">", "<", ">=", "<=", "between", "==", "and", "or", "not"}
_DSL_ACTIONS = {"call", "put", "with_momentum", "fade", "skip"}
_DSL_SIGNALS = {
    "efficiency_ratio", "recent_range_pct", "vs_anchor", "momentum_dir",
    "short_thrust_er", "short_thrust_dir", "at_support", "at_resistance",
    "breaking_out", "breaking_down", "mid_range", "recent_win_rate",
    "mtf_dir", "mtf_alignment", "vwap_z", "above_upper_band", "below_lower_band",
    "book_imbalance",
}

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "scalper.db")

# Max age of an L1 quote before we treat the feed as stale and refuse entries.
_MAX_QUOTE_AGE_S = 15


class _OpenScalp:
    def __init__(self, occ, underlying, direction, qty, entry_mid, order_id):
        self.occ = occ
        self.underlying = underlying
        self.direction = direction
        self.qty = qty
        self.entry_mid = entry_mid
        self.entry_premium = entry_mid * qty * 100
        self.order_id = order_id
        self.opened_at = datetime.utcnow()
        self.peak_mid = entry_mid
        self.entry_ctx: Dict[str, Any] = {}  # signals snapshot captured at entry


class ScalperEngine:
    def __init__(self, symbols: List[str], stream_getter, alpaca: AlpacaExec,
                 anthropic_key: Optional[str] = None):
        self.symbols = symbols
        self._get_stream = stream_getter  # returns the Schwab streamer wrapper (book_imbalance/latest mid)
        self.alpaca = alpaca
        self._anthropic_key = anthropic_key

        # tunables (defaults mirror platform)
        self.signal_mode = "ai"           # momentum | fade | auto | ai
        self.enabled = False
        self.max_dte = 0
        self.tick_seconds = 3
        self.max_concurrent = 2
        self.per_trade_usd = 500.0
        self.budget_usd = 2000.0
        self.entry_threshold = 0.5
        self.profit_target_pct = 0.20
        self.stop_pct = 0.14
        self.time_stop_seconds = 180
        self.daily_loss_kill_usd = 300.0
        self.profit_lock_arm_usd = 300.0
        self.profit_giveback_frac = 0.4
        self.chop_guard_enabled = True
        self.chop_min_efficiency = 0.22
        self.velocity_gate_enabled = True
        self.min_recent_range_pct = 0.18
        self.w_momentum = 0.6
        self.w_flow = 0.4
        self.ai_supervisor_seconds = 120
        self.ai_tune_seconds = 600
        self.daily_entry_start_utc: Optional[int] = None
        self.daily_entry_end_utc: Optional[int] = None

        # state
        self._prices: Dict[str, Deque[Tuple[datetime, float]]] = {s: deque(maxlen=ind.PRICE_WINDOW) for s in symbols}
        self._regime_samples: Dict[str, Deque[Tuple[datetime, float]]] = {s: deque(maxlen=ind.REGIME_ER_WINDOW) for s in symbols}
        self._regime_history: Dict[str, Deque] = {s: deque(maxlen=ind.REGIME_WINDOW) for s in symbols}
        self._anchor_sum: Dict[str, float] = {}
        self._anchor_sq: Dict[str, float] = {}
        self._anchor_n: Dict[str, int] = {}
        # Rolling chart series per symbol for the live VWAP-ish chart:
        # (epoch_s, price, mean, upper_band, lower_band). ~1/sec, ~30min retained.
        self._chart_hist: Dict[str, Deque[Tuple[int, float, Optional[float], Optional[float], Optional[float]]]] = {
            s: deque(maxlen=1800) for s in symbols
        }
        self._chart_last_t: Dict[str, int] = {}
        self._open: Dict[str, _OpenScalp] = {}
        self._pending_signal: Dict[str, Dict[str, Any]] = {}
        self._stop_cooldown: Dict[str, datetime] = {}
        self._last_entry_at: Optional[datetime] = None
        self._min_entry_interval_seconds = 45
        self._recent_exits: Deque[Tuple[str, float]] = deque(maxlen=30)
        self._daily_realized_pnl = 0.0
        self._daily_peak_pnl = 0.0
        self._exits_today = 0
        self._wins_today = 0
        self._entries_today = 0
        self._kill_tripped = False
        self._profit_lock_tripped = False
        self._pnl_day = date.today()

        # AI state
        self._ai_mode: Optional[str] = None
        self._ai_reason: Optional[str] = None
        self._ai_assessment: Optional[str] = None
        self._ai_levels: Dict[str, Any] = {}
        self._ai_error: Optional[str] = None
        self._ai_log: Deque[Dict[str, Any]] = deque(maxlen=40)
        self._ai_last_run: Optional[datetime] = None
        self._ai_tune_last_run: Optional[datetime] = None
        self._active_strategy_spec: Optional[Dict[str, Any]] = None

        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._init_db()
        self._load_state()  # restore enabled/mode/tunables across restarts

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="scalper-engine")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def set_enabled(self, on: bool):
        self.enabled = on
        self._save_state()

    # ── state persistence (survive restarts) ───────────────────────────────
    _STATE_KEYS = ("signal_mode", "enabled", "entry_threshold", "stop_pct",
                   "profit_target_pct", "time_stop_seconds", "chop_min_efficiency",
                   "min_recent_range_pct", "chop_guard_enabled", "velocity_gate_enabled",
                   "profit_lock_arm_usd", "profit_giveback_frac",
                   "ai_supervisor_seconds", "ai_tune_seconds", "max_concurrent",
                   "per_trade_usd", "budget_usd", "max_dte")

    def _state_path(self):
        return os.path.join(os.path.dirname(DB_PATH), "scalper_state.json")

    def save_state(self):
        self._save_state()

    def _save_state(self):
        try:
            data = {k: getattr(self, k) for k in self._STATE_KEYS}
            with open(self._state_path(), "w") as f:
                json.dump(data, f)
        except Exception as e:
            logger.debug("save_state failed: %s", e)

    def _load_state(self):
        try:
            p = self._state_path()
            if not os.path.exists(p):
                return
            with open(p) as f:
                data = json.load(f)
            for k in self._STATE_KEYS:
                if k in data and data[k] is not None:
                    setattr(self, k, data[k])
            logger.info("Restored scalper state: enabled=%s mode=%s", self.enabled, self.signal_mode)
        except Exception as e:
            logger.debug("load_state failed: %s", e)

    # ── ingest prices from the Schwab stream ───────────────────────────────
    def _ingest_prices(self):
        stream = self._get_stream()
        if stream is None:
            return
        now = datetime.utcnow()
        for sym in self.symbols:
            q = None
            try:
                q = stream.latest_quote(sym)
            except Exception:
                q = None
            px = (q or {}).get("mid")
            if px and px > 0:
                self._prices.setdefault(sym, deque(maxlen=ind.PRICE_WINDOW)).append((now, px))
                self._record_regime_sample(sym, px, now)
                self._record_chart_point(sym, px)

    def _record_chart_point(self, sym, px):
        """Append (time, price, mean, ±2σ band) for the live chart, ~1/sec."""
        import time as _t
        t = int(_t.time())
        if self._chart_last_t.get(sym) == t:
            return  # at most one point per second
        self._chart_last_t[sym] = t
        mean = self._anchored_mean(sym)
        std = ind.anchor_std(self._anchor_sum.get(sym, 0.0), self._anchor_sq.get(sym, 0.0), self._anchor_n.get(sym, 0))
        upper = (mean + ind.VWAP_BAND_K * std) if (mean is not None and std is not None) else None
        lower = (mean - ind.VWAP_BAND_K * std) if (mean is not None and std is not None) else None
        self._chart_hist[sym].append((t, round(px, 2),
                                      round(mean, 2) if mean is not None else None,
                                      round(upper, 2) if upper is not None else None,
                                      round(lower, 2) if lower is not None else None))

    def chart_history(self, sym) -> List[Dict[str, Any]]:
        """Live chart series for a symbol: [{t, price, mean, upper, lower}]."""
        return [{"t": t, "price": p, "mean": m, "upper": u, "lower": lo}
                for (t, p, m, u, lo) in self._chart_hist.get(sym, ())]

    def _record_regime_sample(self, sym, px, now):
        self._anchor_sum[sym] = self._anchor_sum.get(sym, 0.0) + px
        self._anchor_n[sym] = self._anchor_n.get(sym, 0) + 1
        self._anchor_sq[sym] = self._anchor_sq.get(sym, 0.0) + px * px
        s = self._regime_samples.setdefault(sym, deque(maxlen=ind.REGIME_ER_WINDOW))
        if s and (now - s[-1][0]).total_seconds() < ind.REGIME_SAMPLE_SECONDS:
            return
        s.append((now, px))

    def _book_imbalance(self, sym) -> Optional[float]:
        stream = self._get_stream()
        if stream is None:
            return None
        try:
            return stream.book_imbalance(sym)
        except Exception:
            return None

    def _quote_age(self, sym) -> Optional[float]:
        stream = self._get_stream()
        if stream is None or not hasattr(stream, "quote_age_seconds"):
            return None
        try:
            return stream.quote_age_seconds(sym)
        except Exception:
            return None

    async def _option_quote(self, occ: str) -> Dict[str, Any]:
        """Real-time options quote — Schwab feed first (no 15-min lag), Alpaca only
        as a fallback. Alpaca is otherwise used solely to SUBMIT orders."""
        stream = self._get_stream()
        if stream is not None and hasattr(stream, "option_quote"):
            q = await asyncio.to_thread(stream.option_quote, occ)
            if q and (q.get("mid_price") or q.get("bid")):
                return q
        return await asyncio.to_thread(self.alpaca.option_quote, occ)

    # ── indicator wrappers (bind engine state to pure indicators) ──────────
    def _latest_price(self, sym):
        w = self._prices.get(sym)
        return w[-1][1] if w else None

    def _efficiency_ratio(self, sym): return ind.efficiency_ratio(self._regime_samples.get(sym))
    def _recent_range_pct(self, sym): return ind.recent_range_pct(self._regime_samples.get(sym))
    def _short_thrust(self, sym): return ind.short_thrust(self._regime_samples.get(sym))
    def _mtf_trend(self, sym): return ind.mtf_trend(self._regime_samples.get(sym))
    def _anchored_mean(self, sym): return ind.anchored_mean(self._anchor_sum.get(sym, 0.0), self._anchor_n.get(sym, 0))

    def _vwap_z(self, sym):
        return ind.vwap_z(self._latest_price(sym), self._anchored_mean(sym),
                          ind.anchor_std(self._anchor_sum.get(sym, 0.0), self._anchor_sq.get(sym, 0.0), self._anchor_n.get(sym, 0)))

    def _near_level(self, sym):
        lv = ind.intraday_levels(self._regime_samples.get(sym))
        last = self._latest_price(sym)
        if lv is None or not last:
            return None
        support, resistance = lv
        span = resistance - support
        if span <= 0:
            return None
        band = max(span * ind.LEVEL_PROXIMITY_FRAC, last * ind.LEVEL_PROXIMITY_MIN_PCT / 100.0)
        if last <= support + band:
            return "support"
        if last >= resistance - band:
            return "resistance"
        return None

    # ── effective fade / momentum signal (port) ────────────────────────────
    def _effective_fade(self, sym) -> bool:
        if self.signal_mode == "fade":
            return True
        if self.signal_mode == "ai":
            if self._ai_mode == "fade":
                return True
            if self._ai_mode == "momentum":
                return False
        if self.signal_mode in ("auto", "ai"):
            er = self._efficiency_ratio(sym)
            return False if er is None else er < ind.TREND_ER_THRESHOLD
        return False

    def _ai_paused(self) -> bool:
        return self.signal_mode == "ai" and self._ai_mode == "pause"

    def _momentum_signal(self, sym) -> Tuple[Optional[str], float]:
        window = self._prices.get(sym)
        if not window or len(window) < 4:
            return None, 0.0
        prices = [p for _, p in window]
        recent = prices[-1]
        fade = self._effective_fade(sym)
        ref = self._anchored_mean(sym) if fade else None
        if ref is None:
            ref = sum(prices) / len(prices)
        prev = prices[-min(6, len(prices))]
        if ref <= 0 or prev <= 0:
            return None, 0.0
        roc = (recent - prev) / prev
        vs_mean = (recent - ref) / ref
        if roc > 0 and vs_mean > 0:
            direction = "call"
        elif roc < 0 and vs_mean < 0:
            direction = "put"
        else:
            return None, 0.0
        if fade:
            direction = "put" if direction == "call" else "call"
        strength = min(1.0, abs(roc) / 0.0003)
        # MTF confirmation
        if ind.MTF_GATE_ENABLED:
            mt = self._mtf_trend(sym)
            if mt is not None:
                mdir, malign = mt
                trade = 1 if direction == "call" else -1
                if mdir != 0 and malign >= ind.MTF_MIN_ALIGNMENT:
                    if mdir == trade:
                        strength = min(1.0, strength * ind.MTF_CONFIRM_BOOST)
                    else:
                        return None, 0.0
        # order-flow confirmation
        imb = self._book_imbalance(sym)
        if imb is not None:
            trade = 1 if direction == "call" else -1
            aligned = (imb > 0 and trade > 0) or (imb < 0 and trade < 0)
            if abs(imb) >= ind.BOOK_IMBALANCE_BLOCK and not aligned:
                return None, 0.0
            if abs(imb) >= ind.BOOK_IMBALANCE_CONFIRM and aligned:
                strength = min(1.0, strength * ind.BOOK_IMBALANCE_BOOST)
        return direction, strength

    # ── DSL (port) ─────────────────────────────────────────────────────────
    def _dsl_signal_value(self, sym, name, momentum_dir):
        if name == "efficiency_ratio": return self._efficiency_ratio(sym)
        if name == "recent_range_pct": return self._recent_range_pct(sym)
        if name == "momentum_dir": return momentum_dir
        if name == "recent_win_rate": return (self._wins_today / self._exits_today) if self._exits_today else None
        if name == "vs_anchor":
            last, anchor = self._latest_price(sym), self._anchored_mean(sym)
            return ((last - anchor) / anchor * 100.0) if (last and anchor) else None
        if name in ("short_thrust_er", "short_thrust_dir"):
            th = self._short_thrust(sym)
            return None if th is None else (th[1] if name == "short_thrust_er" else th[0])
        if name in ("mtf_dir", "mtf_alignment"):
            mt = self._mtf_trend(sym)
            if mt is None: return None
            return mt[1] if name == "mtf_alignment" else {1: "call", -1: "put", 0: "flat"}[mt[0]]
        if name == "vwap_z": return self._vwap_z(sym)
        if name == "book_imbalance": return self._book_imbalance(sym)
        if name in ("above_upper_band", "below_lower_band"):
            z = self._vwap_z(sym)
            if z is None: return None
            return z >= ind.VWAP_BAND_K if name == "above_upper_band" else z <= -ind.VWAP_BAND_K
        lv = (self._ai_levels or {}).get(sym) or {}
        pos = str(lv.get("position") or "").lower()
        rule_level = self._near_level(sym) if not pos else None
        if name == "at_support": return ("support" in pos) or (rule_level == "support")
        if name == "at_resistance": return ("resistance" in pos) or (rule_level == "resistance")
        if name == "breaking_out": return "breaking out" in pos
        if name == "breaking_down": return "breaking down" in pos
        if name == "mid_range": return "mid-range" in pos or "mid range" in pos
        return None

    def _dsl_eval_cond(self, sym, node, momentum_dir) -> bool:
        if isinstance(node, bool): return node
        if not isinstance(node, dict): return False
        op = node.get("op")
        if op == "and": return all(self._dsl_eval_cond(sym, k, momentum_dir) for k in node.get("conds", []))
        if op == "or": return any(self._dsl_eval_cond(sym, k, momentum_dir) for k in node.get("conds", []))
        if op == "not": return not self._dsl_eval_cond(sym, node.get("cond"), momentum_dir)
        val = self._dsl_signal_value(sym, node.get("signal"), momentum_dir)
        if val is None: return False
        try:
            if op == "between":
                lo, hi = node.get("range"); return lo <= float(val) <= hi
            target = node.get("value")
            if op == "==": return val == target
            fv, ft = float(val), float(target)
            return {">" : fv > ft, "<": fv < ft, ">=": fv >= ft, "<=": fv <= ft}.get(op, False)
        except (TypeError, ValueError):
            return False

    def _eval_strategy_spec(self, sym, momentum_dir) -> Optional[str]:
        spec = self._active_strategy_spec
        if not spec: return momentum_dir
        for rule in spec.get("rules", []):
            if self._dsl_eval_cond(sym, rule.get("when"), momentum_dir):
                act = rule.get("action")
                if act == "skip": return None
                if act in ("call", "put"): return act
                if act == "with_momentum": return momentum_dir
                if act == "fade": return ("put" if momentum_dir == "call" else "call") if momentum_dir else None
        return None

    def _validate_strategy_spec(self, spec) -> Optional[Dict[str, Any]]:
        if not isinstance(spec, dict): return None
        rules = spec.get("rules")
        if not isinstance(rules, list) or not rules or len(rules) > 12: return None
        def ok(node, depth):
            if depth > 6: return False
            if isinstance(node, bool): return True
            if not isinstance(node, dict): return False
            op = node.get("op")
            if op not in _DSL_OPS: return False
            if op in ("and", "or"):
                kids = node.get("conds")
                return isinstance(kids, list) and 0 < len(kids) <= 8 and all(ok(k, depth + 1) for k in kids)
            if op == "not": return ok(node.get("cond"), depth + 1)
            if node.get("signal") not in _DSL_SIGNALS: return False
            if op == "between":
                rng = node.get("range")
                return isinstance(rng, list) and len(rng) == 2 and all(isinstance(x, (int, float)) for x in rng)
            return isinstance(node.get("value"), (int, float, str, bool))
        for rule in rules:
            if not isinstance(rule, dict) or not ok(rule.get("when"), 0) or rule.get("action") not in _DSL_ACTIONS:
                return None
        return spec

    def _apply_runtime_params(self, changes) -> Dict[str, Any]:
        if not isinstance(changes, dict): return {}
        applied = {}
        for key, raw in changes.items():
            try:
                if key in _TUNE_SAFE_BANDS:
                    lo, hi = _TUNE_SAFE_BANDS[key]
                    val = float(raw)
                    if val != val: continue
                    val = min(hi, max(lo, val))
                    if key in _TUNE_INT_KEYS: val = int(val)
                    setattr(self, key, val); applied[key] = val
                elif key in _TUNE_BOOL_KEYS:
                    setattr(self, key, bool(raw)); applied[key] = bool(raw)
            except (TypeError, ValueError):
                continue
        return applied

    def _regime_ok(self, sym, direction) -> bool:
        hist = self._regime_history.get(sym, [])
        populated = [r for r in hist if r is not None]
        if len(populated) < ind.REGIME_WINDOW // 2:
            return True
        agrees = sum(1 for r in populated if r == direction)
        return (agrees / len(populated)) >= ind.REGIME_THRESHOLD

    # ── main loop ──────────────────────────────────────────────────────────
    async def _loop(self):
        while self._running:
            try:
                self._maybe_roll_day()
                self._ingest_prices()
                if self.enabled and not self._kill_tripped:
                    await self._manage_open_scalps()
                    await self._maybe_run_supervisor()
                    if self.signal_mode == "ai" and self._ai_paused():
                        pass  # AI says stand aside
                    elif not self._profit_lock_tripped:
                        await self._scan_for_entries()
                    self._check_kill_and_lock()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("scalper loop tick failed: %s", e)
            try:
                await asyncio.sleep(self.tick_seconds)
            except asyncio.CancelledError:
                break

    def _maybe_roll_day(self):
        today = date.today()
        if today != self._pnl_day:
            self._pnl_day = today
            self._daily_realized_pnl = 0.0
            self._daily_peak_pnl = 0.0
            self._kill_tripped = False
            self._profit_lock_tripped = False
            self._exits_today = self._wins_today = self._entries_today = 0
            self._anchor_sum = {}; self._anchor_n = {}; self._anchor_sq = {}
            self._recent_exits.clear()

    def _check_kill_and_lock(self):
        if self._daily_realized_pnl <= -self.daily_loss_kill_usd and not self._kill_tripped:
            self._kill_tripped = True
            logger.warning("KILL SWITCH tripped at $%.0f", self._daily_realized_pnl)
        if self._daily_realized_pnl > self._daily_peak_pnl:
            self._daily_peak_pnl = self._daily_realized_pnl
        if (self._daily_peak_pnl >= self.profit_lock_arm_usd
                and self._daily_realized_pnl <= self._daily_peak_pnl * (1 - self.profit_giveback_frac)
                and not self._profit_lock_tripped):
            self._profit_lock_tripped = True
            logger.warning("PROFIT LOCK tripped — peak $%.0f now $%.0f", self._daily_peak_pnl, self._daily_realized_pnl)

    # ── exits (port) ───────────────────────────────────────────────────────
    async def _manage_open_scalps(self):
        if not self._open:
            return
        now = datetime.utcnow()
        for occ in list(self._open.keys()):
            scalp = self._open.get(occ)
            if scalp is None:
                continue
            snap = await self._option_quote(occ)
            mid = snap.get("mid_price") or snap.get("bid")
            held = (now - scalp.opened_at).total_seconds()
            er = self._efficiency_ratio(scalp.underlying)
            trending = er is not None and er >= ind.TREND_ER_THRESHOLD
            eff_stop = self.stop_pct * (ind.TREND_STOP_MULT if trending else 1.0)
            eff_time = self.time_stop_seconds * (ind.TREND_TIME_MULT if trending else 1.0)
            reason = None
            pnl_pct = 0.0
            if mid and scalp.entry_mid > 0:
                mid_f = float(mid)
                pnl_pct = (mid_f - scalp.entry_mid) / scalp.entry_mid
                if mid_f > scalp.peak_mid:
                    scalp.peak_mid = mid_f
                peak_pct = (scalp.peak_mid - scalp.entry_mid) / scalp.entry_mid
                if peak_pct >= ind.TRAIL_ACTIVATE_PCT and pnl_pct <= peak_pct - ind.TRAIL_GIVEBACK_PCT:
                    reason = "trail"
                elif not trending and pnl_pct >= self.profit_target_pct:
                    reason = "profit_target"
                elif pnl_pct <= -eff_stop:
                    reason = "stop"
            if reason is None and held >= eff_time:
                reason = "time_stop"
            if reason:
                await self._close_scalp(scalp, float(mid) if mid else None, pnl_pct, reason)

    async def _close_scalp(self, scalp, exit_mid, pnl_pct, reason):
        await asyncio.to_thread(self.alpaca.sell_to_close, scalp.occ, scalp.qty,
                                (exit_mid * 0.98) if exit_mid else None)
        pnl_usd = (pnl_pct * scalp.entry_premium) if exit_mid else 0.0
        self._daily_realized_pnl += pnl_usd
        self._exits_today += 1
        if pnl_usd > 0:
            self._wins_today += 1
        self._recent_exits.append((reason, pnl_usd))
        if reason == "stop":
            self._stop_cooldown[scalp.underlying] = datetime.utcnow() + timedelta(seconds=60)
        self._open.pop(scalp.occ, None)
        held_s = (datetime.utcnow() - scalp.opened_at).total_seconds()
        exit_ctx = self._signal_snapshot(scalp.underlying)
        self._persist_trade(scalp, exit_mid, pnl_usd, pnl_pct, reason, held_s, exit_ctx)
        logger.info("EXIT %s %s pnl=$%.0f (%s) held=%.0fs", scalp.direction, scalp.occ, pnl_usd, reason, held_s)

    # ── entries (port of _scan_for_entries) ────────────────────────────────
    async def _scan_for_entries(self):
        now = datetime.utcnow()
        minutes_to_close = (20 * 60 - now.hour * 60 - now.minute)
        if 0 < minutes_to_close <= 30:
            return
        if self.daily_entry_start_utc is not None or self.daily_entry_end_utc is not None:
            cm = now.hour * 60 + now.minute
            if (self.daily_entry_start_utc is not None and cm < self.daily_entry_start_utc) or \
               (self.daily_entry_end_utc is not None and cm >= self.daily_entry_end_utc):
                return
        committed = sum(s.entry_premium for s in self._open.values())
        if committed >= self.budget_usd:
            return
        open_dirs = {s.direction for s in self._open.values()}
        locked = open_dirs.pop() if len(open_dirs) == 1 else None
        if self._last_entry_at and (now - self._last_entry_at).total_seconds() < self._min_entry_interval_seconds:
            return

        for sym in self.symbols:
            if len(self._open) >= self.max_concurrent:
                break
            if any(s.underlying == sym for s in self._open.values()):
                continue
            cd = self._stop_cooldown.get(sym)
            if cd and now < cd:
                continue
            # Staleness guard: never enter on a frozen/stalled feed. If the L1
            # quote hasn't updated in _MAX_QUOTE_AGE_S, skip — a scalper trading
            # on stale prices is how you bleed.
            age = self._quote_age(sym)
            if age is not None and age > _MAX_QUOTE_AGE_S:
                self._pending_signal.pop(sym, None)
                logger.warning("Scalper %s: STALE feed (%.0fs) — skipping entries", sym, age)
                continue
            momentum_dir, strength = self._momentum_signal(sym)
            if sym in self._regime_history:
                self._regime_history[sym].append(momentum_dir)
            if momentum_dir is None or strength <= 0:
                self._pending_signal.pop(sym, None)
                continue
            if self._active_strategy_spec is not None:
                dsl_dir = self._eval_strategy_spec(sym, momentum_dir)
                if dsl_dir is None:
                    self._pending_signal.pop(sym, None)
                    continue
                momentum_dir = dsl_dir
            if self.chop_guard_enabled:
                er = self._efficiency_ratio(sym)
                if er is not None and er < self.chop_min_efficiency:
                    th = self._short_thrust(sym)
                    override = (self.velocity_gate_enabled and th is not None and th[0] == momentum_dir
                                and th[1] >= ind.THRUST_MIN_ER and th[2] >= self.min_recent_range_pct * ind.THRUST_RANGE_MULT)
                    if not override:
                        self._pending_signal.pop(sym, None)
                        continue
            if self.velocity_gate_enabled:
                rng = self._recent_range_pct(sym)
                if rng is not None and rng < self.min_recent_range_pct:
                    self._pending_signal.pop(sym, None)
                    continue
            if locked and momentum_dir != locked:
                self._pending_signal.pop(sym, None)
                continue
            score = self.w_momentum * strength  # flow_corr = 0 standalone (no options-flow scout)
            if score < self.entry_threshold:
                self._pending_signal.pop(sym, None)
                continue
            if not self._regime_ok(sym, momentum_dir):
                self._pending_signal.pop(sym, None)
                continue
            prev = self._pending_signal.get(sym)
            if prev and prev.get("dir") == momentum_dir and (now - prev["ts"]).total_seconds() < 15:
                self._pending_signal.pop(sym, None)
                await self._open_scalp(sym, momentum_dir, score)
            else:
                self._pending_signal[sym] = {"dir": momentum_dir, "ts": now}

    async def _open_scalp(self, sym, direction, score):
        contracts = await asyncio.to_thread(self.alpaca.find_contracts, sym, direction, self.max_dte)
        underlying_px = self._latest_price(sym)
        if not contracts or not underlying_px:
            return
        # pick nearest-the-money 1-strike-OTM-ish
        contracts.sort(key=lambda c: abs(c["strike"] - underlying_px))
        contract = contracts[0]
        occ = contract["symbol"]
        snap = await self._option_quote(occ)
        ask = snap.get("ask") or snap.get("mid_price")
        if not ask or ask <= 0:
            return
        ask = float(ask)
        qty = int(self.per_trade_usd // (ask * 100.0))
        if qty < 1:
            return
        order = await asyncio.to_thread(self.alpaca.buy_to_open, occ, qty, ask)
        if not order:
            return
        entry = await asyncio.to_thread(self.alpaca.fill_price, order.get("order_id"), ask)
        scalp = _OpenScalp(occ, sym, direction, qty, float(entry), order.get("order_id"))
        scalp.entry_ctx = self._signal_snapshot(sym, score)  # full forensic context at entry
        self._open[occ] = scalp
        self._entries_today += 1
        self._last_entry_at = datetime.utcnow()
        logger.info("ENTRY %s %s x%d @ %.2f (score=%.2f)", direction, occ, qty, entry, score)

    def _signal_snapshot(self, sym, score=None) -> Dict[str, Any]:
        """Full signal context for a symbol — captured at entry/exit so each trade
        is forensically explainable (why it entered, what the tape was doing)."""
        mt = self._mtf_trend(sym)
        return {
            "underlying": self._latest_price(sym),
            "signal_mode": self.signal_mode,
            "ai_mode": self._ai_mode,
            "ai_assessment": self._ai_assessment,
            "score": round(score, 3) if score is not None else None,
            "efficiency_ratio": _r3(self._efficiency_ratio(sym)),
            "recent_range_pct": _r3(self._recent_range_pct(sym)),
            "vwap_z": _r3(self._vwap_z(sym)),
            "anchored_mean": _r3(self._anchored_mean(sym)),
            "mtf_dir": (mt[0] if mt else None),
            "mtf_alignment": _r3(mt[1]) if mt else None,
            "book_imbalance": _r3(self._book_imbalance(sym)),
            "entry_threshold": self.entry_threshold,
            "stop_pct": self.stop_pct,
            "profit_target_pct": self.profit_target_pct,
        }

    # ── supervisor ─────────────────────────────────────────────────────────
    async def _maybe_run_supervisor(self):
        if self.signal_mode != "ai" or not self._anthropic_key:
            return
        now = datetime.utcnow()
        deep_due = self._ai_tune_last_run is None or (now - self._ai_tune_last_run).total_seconds() >= self.ai_tune_seconds
        fast_due = self._ai_last_run is None or (now - self._ai_last_run).total_seconds() >= self.ai_supervisor_seconds
        if not (deep_due or fast_due):
            return
        deep = deep_due
        self._ai_last_run = now
        if deep:
            self._ai_tune_last_run = now
        tape = {}
        for s in self.symbols:
            tape[s] = {
                "last": round(self._latest_price(s), 2) if self._latest_price(s) else None,
                "session_vwap_ish": round(self._anchored_mean(s), 2) if self._anchored_mean(s) else None,
                "efficiency_ratio": round(self._efficiency_ratio(s), 3) if self._efficiency_ratio(s) is not None else None,
                "recent_range_pct": round(self._recent_range_pct(s), 3) if self._recent_range_pct(s) is not None else None,
                "book_imbalance": round(self._book_imbalance(s), 2) if self._book_imbalance(s) is not None else None,
            }
        wr = (self._wins_today / self._exits_today) if self._exits_today else None
        tunable = {k: getattr(self, k) for k in _TUNE_SAFE_BANDS}
        tunable.update({k: getattr(self, k) for k in _TUNE_BOOL_KEYS})
        prompt = sup.build_prompt(
            self.symbols, tape, deep, tunable=tunable,
            safe_bands={k: list(v) for k, v in _TUNE_SAFE_BANDS.items()},
            bool_keys=list(_TUNE_BOOL_KEYS), dsl_signals=list(_DSL_SIGNALS),
            recent_exits=list(self._recent_exits), day_pnl=self._daily_realized_pnl,
            win_rate=wr, exits_today=self._exits_today,
            last_tune=self._ai_log[0] if self._ai_log else None,
        )
        data = await sup.run_supervisor_call(self._anthropic_key, prompt, deep)
        if not data:
            self._ai_error = "unparseable / no response"
            return
        mode = str(data.get("mode", "")).lower().strip()
        if mode not in ("momentum", "fade", "pause"):
            self._ai_error = "no valid mode"
            return
        self._ai_mode = mode
        self._ai_reason = str(data.get("reason", ""))[:120]
        self._ai_levels = data.get("levels", {}) or {}
        self._ai_assessment = str(data.get("assessment", ""))[:600]
        self._ai_error = None
        params_after = {}
        if deep:
            req = data.get("param_changes") or {}
            if isinstance(req, dict) and req:
                params_after = self._apply_runtime_params(req)
            if "strategy_spec" in data:
                spec = data.get("strategy_spec")
                self._active_strategy_spec = self._validate_strategy_spec(spec) if spec else None
        entry = {
            "mode": mode, "reason": self._ai_reason, "assessment": self._ai_assessment,
            "reasoning": str(data.get("reasoning", ""))[:2000] or None,
            "param_changes": params_after or None, "levels": self._ai_levels,
            "strategy_spec": self._active_strategy_spec if deep else None,
            "cycle": "deep" if deep else "fast", "day_pnl": round(self._daily_realized_pnl),
            "timestamp": now.isoformat() + "Z",
        }
        self._ai_log.appendleft(entry)
        self._persist_ai(entry)
        logger.info("AI [%s] → %s | %s", entry["cycle"], mode, self._ai_assessment)

    # ── persistence (SQLite) ───────────────────────────────────────────────
    def _init_db(self):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        c = sqlite3.connect(DB_PATH)
        c.execute("CREATE TABLE IF NOT EXISTS scalp_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, occ TEXT, underlying TEXT, direction TEXT, qty INTEGER, entry REAL, exit REAL, pnl REAL, pnl_pct REAL, reason TEXT, held_s REAL, opened_ts INTEGER, entry_ctx TEXT, exit_ctx TEXT)")
        # Migrate older DBs that predate the rich columns.
        for col, decl in (("pnl_pct","REAL"),("held_s","REAL"),("opened_ts","INTEGER"),("entry_ctx","TEXT"),("exit_ctx","TEXT")):
            try:
                c.execute(f"ALTER TABLE scalp_trades ADD COLUMN {col} {decl}")
            except Exception:
                pass
        c.execute("CREATE TABLE IF NOT EXISTS scalp_ai (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, cycle TEXT, mode TEXT, assessment TEXT, reasoning TEXT, param_changes TEXT, strategy_spec TEXT)")
        c.commit(); c.close()

    def _persist_trade(self, scalp, exit_mid, pnl, pnl_pct, reason, held_s, exit_ctx):
        try:
            c = sqlite3.connect(DB_PATH)
            c.execute(
                "INSERT INTO scalp_trades (ts,occ,underlying,direction,qty,entry,exit,pnl,pnl_pct,reason,held_s,opened_ts,entry_ctx,exit_ctx) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (int(time.time() * 1000), scalp.occ, scalp.underlying, scalp.direction, scalp.qty,
                 scalp.entry_mid, exit_mid, pnl, round(pnl_pct, 4), reason, round(held_s, 1),
                 int(scalp.opened_at.timestamp() * 1000),
                 json.dumps(scalp.entry_ctx), json.dumps(exit_ctx)))
            c.commit(); c.close()
        except Exception as e:
            logger.debug("persist trade failed: %s", e)

    def _persist_ai(self, entry):
        try:
            c = sqlite3.connect(DB_PATH)
            c.execute("INSERT INTO scalp_ai (ts,cycle,mode,assessment,reasoning,param_changes,strategy_spec) VALUES (?,?,?,?,?,?,?)",
                      (int(time.time() * 1000), entry["cycle"], entry["mode"], entry.get("assessment"),
                       entry.get("reasoning"), json.dumps(entry.get("param_changes")), json.dumps(entry.get("strategy_spec"))))
            c.commit(); c.close()
        except Exception as e:
            logger.debug("persist ai failed: %s", e)

    # ── status for the UI ──────────────────────────────────────────────────
    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled, "running": self._running, "signal_mode": self.signal_mode,
            "ai_mode": self._ai_mode, "ai_reason": self._ai_reason, "ai_assessment": self._ai_assessment,
            "ai_error": self._ai_error, "ai_log": list(self._ai_log)[:20],
            "active_strategy_spec": self._active_strategy_spec,
            "kill_switch_tripped": self._kill_tripped, "profit_lock_tripped": self._profit_lock_tripped,
            "day_pnl": round(self._daily_realized_pnl, 2), "peak_pnl": round(self._daily_peak_pnl, 2),
            "entries_today": self._entries_today, "exits_today": self._exits_today, "wins_today": self._wins_today,
            "win_rate": round(self._wins_today / self._exits_today, 2) if self._exits_today else None,
            "open_scalps": len(self._open),
            "symbols": self.symbols,
            "ai_supervisor_seconds": self.ai_supervisor_seconds,
            "ai_tune_seconds": self.ai_tune_seconds,
            "options_quote_source": "schwab" if (self._get_stream() and hasattr(self._get_stream(), "option_quote")) else "alpaca",
            "tunables": {k: getattr(self, k) for k in _TUNE_SAFE_BANDS},
            "signals": {
                s: {
                    "last": self._latest_price(s),
                    "efficiency_ratio": self._efficiency_ratio(s),
                    "recent_range_pct": self._recent_range_pct(s),
                    "vwap_z": self._vwap_z(s),
                    "mtf": self._mtf_trend(s),
                    "book_imbalance": self._book_imbalance(s),
                    "quote_age_s": round(a, 1) if (a := self._quote_age(s)) is not None else None,
                } for s in self.symbols
            },
        }
