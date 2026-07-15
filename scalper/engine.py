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
from .macross import EmaPair, EmaTriple, adaptive_trail_giveback

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
    # Adaptive resolution — AI tunes the timescale to the regime:
    "regime_sample_seconds": (5, 60),   # finer for fast moves, coarser for slow grinds
    "er_window": (8, 40),               # how many samples ER spans
    # MA-crossover (macross) knobs — AI may tune the EMA horizon + exit aggression:
    "ma_fast": (3, 20),
    "ma_slow": (10, 60),
    "ma_tf_seconds": (15, 120),
    # the AI strategy's OWN EMA lengths (tuned by the supervisor in 'all' mode; kept
    # separate so tuning them never disturbs the fixed macross/macross_x EMAs):
    "ma_fast_ai": (3, 20),
    "ma_slow_ai": (10, 60),
    "ma_profit_target_pct": (0.10, 0.50),
    "ma_stop_pct": (0.06, 0.25),
    "ma_trail_giveback_pct": (0.04, 0.25),
    "ma_trail_floor_pct": (0.02, 0.10),
    "ma_trail_activate_pct": (0.03, 0.25),
    "ma_peak_lock_arm_pct": (0.10, 0.80),   # peak-giveback lock arms above this peak %
    "ma_peak_giveback_frac": (0.15, 0.60),  # give back this fraction of the peak → exit
    "ma_max_vwap_z": (0.0, 3.0),         # exhaustion entry guard (0 = off)
    "ma_min_separation_pct": (0.0, 0.10),# marginal-cross filter (0 = off)
    "ma_tri_min_sep_pct": (0.0, 0.15),   # 9/21/50 leg separation gate (anti kiss-flicker)
    "chop_shutoff_er": (0.0, 0.50),      # global chop shutoff: no entries below this ER (0=off)
    "apex_min_er": (0.0, 0.60),          # APEX strict efficiency floor
    "apex_max_vwap_z": (0.0, 4.0),       # APEX anti-chase: skip if extended past this σ (0=off)
    "apex_trend_bars": (2, 20),          # APEX dominant-trend slope lookback
    "ma_fast_exit_buffer_pct": (0.0, 0.10),  # buffer past fast EMA for the fast exit
    "ma_fast_exit_min_hold_s": (0, 120),     # settle-in before fast exit can fire
    "ma_min_er": (0.0, 0.80),                # chop gate: min efficiency-ratio to enter
    "ma_breakeven_arm_pct": (0.02, 0.15),    # green level that protects a trade from time-stop
    "ma_breakeven_floor_pct": (-0.02, 0.05), # give-back floor for a protected small-green trade
    "ma_book_oppose": (0.2, 1.0),            # L2: skip if book opposes by >= this
    "ma_extended_sep_pct": (0.02, 0.20),     # EMA sep above which a move is "extended"
    "ma_book_support_min": (0.0, 0.5),       # L2 support required on an extended move
    "ma_wall_near_pct": (0.02, 0.25),        # wall within this % of price = near
    "ma_book_confirm_min": (0.0, 0.6),       # book support required to confirm an entry
    "ma_underlying_stop_pct": (0.05, 0.60),  # underlying adverse move (%) that stops a trade
    "ma_stop_pct_backstop": (0.20, 0.80),    # wide option-% catastrophe backstop
    "ma_time_stop_backstop_s": (300, 3600),  # FAR time backstop for a never-invalidated flat trade
    "ma_min_hold_before_crossback_s": (0, 60),  # ignore cross_back within this many s of entry
    "ma_trend_bias_bars": (2, 12),           # slow-EMA slope window for the trend bias
    "ma_failed_reversal_grace_s": (10, 180),        # how long the reversal has to start working
    "ma_failed_reversal_min_pnl_pct": (-0.40, -0.02),  # option P&L worse than this at deadline = cut
}
_TUNE_BOOL_KEYS = ("chop_guard_enabled", "velocity_gate_enabled", "ma_fast_exit_enabled",
                   "ma_book_gate_enabled", "ma_book_invert",
                   "ma_wall_gate_enabled", "ma_wall_bounce_signal", "ma_require_confirm",
                   "ma_trend_bias_enabled", "ma_tri_require_l2",
                   "ma_failed_reversal_enabled", "ma_failed_reversal_needs_progress")
_TUNE_INT_KEYS = ("time_stop_seconds", "regime_sample_seconds", "er_window",
                  "ma_fast", "ma_slow", "ma_tf_seconds", "ma_fast_exit_min_hold_s",
                  "ma_min_hold_before_crossback_s", "ma_trend_bias_bars",
                  "ma_fast_ai", "ma_slow_ai", "ma_time_stop_backstop_s", "apex_trend_bars",
                  "ma_failed_reversal_grace_s")

# Exit-reason classification for user-confirmation mode. RISK exits protect
# capital/gains and ALWAYS fire automatically, even when autonomous is off — a
# stop you might miss is a bigger loss. Only discretionary SIGNAL exits (a
# reversal signal where the user might reasonably choose to hold) are gated.
_RISK_EXIT_REASONS = frozenset({
    "stop", "time_stop", "peak_lock", "trail", "kill", "profit_lock",
    "kill_switch", "profit_lock_giveback",
    "reversal_stop", "failed_reversal",   # thesis-invalidation cuts — protect capital
})
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
    def __init__(self, occ, underlying, direction, qty, entry_mid, order_id, strategy="macross3"):
        self.occ = occ
        self.underlying = underlying
        self.direction = direction
        self.strategy = strategy          # which scalp strategy opened this (for A/B tracking)
        self.key = f"{strategy}:{occ}"    # unique per (strategy, occ) so strategies coexist
        self.qty = qty
        self.entry_mid = entry_mid
        self.entry_premium = entry_mid * qty * 100
        self.order_id = order_id
        self.opened_at = datetime.utcnow()
        self.opened_epoch_ms = int(time.time() * 1000)  # real epoch (utcnow().timestamp() is tz-wrong)
        self.peak_mid = entry_mid
        self.entry_underlying: Optional[float] = None  # underlying px at entry (for the underlying-based stop)
        self.tight_trail = False              # set by a big adverse candle → exit tightens
        self.hard_stop_underlying: Optional[float] = None  # reversal-candle stop (underlying px)
        self.ema50_stop_armed = False         # macross: EMA-50 stop only arms once price
                                              # has first cleared the 50 on the winning side
        self.entry_ctx: Dict[str, Any] = {}  # signals snapshot captured at entry


class ScalperEngine:
    def __init__(self, symbols: List[str], stream_getter, alpaca: AlpacaExec,
                 anthropic_key: Optional[str] = None, alpaca_live: Optional[AlpacaExec] = None):
        self.symbols = symbols
        self._get_stream = stream_getter  # returns the Schwab streamer wrapper (book_imbalance/latest mid)
        self._alpaca_paper = alpaca
        self._alpaca_live = alpaca_live   # None unless LIVE Alpaca keys were provided
        self.live_trading = False         # REAL MONEY when True — default OFF, guarded
        self.alpaca = self._alpaca_paper  # active execution client (paper by default)
        self._anthropic_key = anthropic_key

        # tunables (defaults mirror platform)
        self.signal_mode = "ai"           # momentum | fade | auto | ai | macross
        self.enabled = False
        # Autonomous vs. user-confirmed trading. When True (default), signals
        # execute immediately (the classic behavior). When False, entry signals
        # and DISCRETIONARY (signal-driven) exits are parked as pending signals
        # for the user to confirm within a short window; RISK exits (stop /
        # time-stop / peak-lock / trail / kill / profit-lock) ALWAYS fire
        # automatically regardless — capital protection is never gated on a tap.
        self.autonomous = True
        self._pending: Dict[str, dict] = {}   # signal_id -> pending signal
        self._pending_seq = 0
        self.confirm_window_s = 8             # a pending signal expires after this
        # Options ladder: the ATM±2 strikes the engine is currently eyeing per
        # symbol (side chosen by the EMA regime), refreshed periodically off the
        # hot path so status() stays cheap. {sym: {side, strikes:[{strike,occ,mid,...}]}}
        self._ladder: Dict[str, dict] = {}
        self._ladder_at: Optional[datetime] = None
        self._position_cache: list = []       # enriched open positions (loop-refreshed)
        # MA-crossover (high-frequency) params — the 'macross' mode.
        self.ma_fast = 8
        self.ma_slow = 21
        self.ma_tf_seconds = 60           # 1-min-equivalent EMA horizon
        # The 'ai' strategy (in 'all' mode) runs its OWN EMA pair so the supervisor
        # can tune EMA lengths WITHOUT disturbing the fixed macross/macross_x EMAs —
        # keeps the A/B clean. Seeded from the same 8/21 defaults; AI retunes via
        # param_changes {ma_fast_ai, ma_slow_ai}.
        self.ma_fast_ai = 8
        self.ma_slow_ai = 21
        self.ma_trail_giveback_pct = 0.10 # base trail give-back (tightens by vwap_z)
        self.ma_trail_floor_pct = 0.03    # min give-back when fully extended
        self.ma_profit_target_pct = 0.25  # min target on stallers — must EXCEED ma_stop
        self.ma_trail_activate_pct = 0.12 # arm the runner-trail once up this much
        # PEAK-GIVEBACK LOCK (V-reversal fix): once a runner peaks >= arm_pct, exit if it
        # gives back giveback_frac of that PEAK — regardless of the lagging EMA regime.
        self.ma_peak_lock_arm_pct = 0.25  # only lock once the option is up >= +25% at peak
        self.ma_peak_giveback_frac = 0.35 # give back 35% of the peak → lock the rest
        self.ma_stop_pct = 0.12           # (legacy option-% stop — superseded by the
                                          # underlying-based stop below; kept for reference)
        self.ma_underlying_stop_pct = 0.10 # STOP when the UNDERLYING moves against us by
                                          # this % (stable, not gamma-amplified). TIGHT
                                          # ("just below entry") = the cross invalidation:
                                          # if price crosses back under entry the thesis is
                                          # dead → stop → wait for the next cross.
        self.ma_stop_pct_backstop = 0.45  # wide option-% HARD backstop (catastrophe only)
        self.ma_time_stop_backstop_s = 1800  # FAR time backstop (30m): a flat/red trade is
                                          # NOT time-stopped on the usual clock (rides while
                                          # the cross stays valid); this only kills a truly
                                          # dead, never-invalidated trade so it can't hold
                                          # forever bleeding theta.
        self.ma_min_hold_before_crossback_s = 15  # ignore a regime flip within this many
                                          # seconds of entry (EMA flicker at the cross, not
                                          # a real reversal) — the underlying stop caps risk
        # FAILED-REVERSAL EARLY EXIT (operator model): an entry is a bet on an
        # IMMEDIATE change of direction. A good reversal signal works FAST — if
        # the move hasn't gone our way shortly after entry, the direction-change
        # didn't happen and the signal was wrong. Cut it SMALL rather than ride a
        # bleeder to the wide catastrophe stop (the QQQ 719C that leaked -16% over
        # 321s waiting on a lagging EMA break). At the grace deadline: if the trade
        # is worse than a small tolerance AND the underlying hasn't progressed in
        # our favor, exit "failed_reversal". Distinct from the min-hold flicker
        # guard: this fires ONCE, at the deadline, on a trade that never started
        # working — not on transient noise.
        self.ma_failed_reversal_enabled = True
        self.ma_failed_reversal_grace_s = 45   # give the reversal this long to start
        self.ma_failed_reversal_min_pnl_pct = -0.10  # if option P&L is worse than this at
                                          # the deadline (and underlying hasn't progressed),
                                          # the reversal failed → cut it (-10% option)
        self.ma_failed_reversal_needs_progress = True  # also require the UNDERLYING to have
                                          # moved in our favor vs entry; if it hasn't by the
                                          # deadline, the direction-change clearly didn't happen
        # HIGHER-TF TREND BIAS: only trade WITH the slow-EMA-slope trend (ignore the
        # counter-trend 8/21 flickers on intra-trend bounces → ride the dominant move).
        self.ma_trend_bias_enabled = True
        self.ma_trend_bias_bars = 5       # slow-EMA slope measured over this many bars
        self.ma_breakeven_arm_pct = 0.05  # once a trade peaks >= +this, DON'T time-stop it —
                                          # protect it with a breakeven floor + let it run
        self.ma_breakeven_floor_pct = 0.01 # exit a protected small-green trade if it gives
                                          # back to +this (locks a small win, not a loss)
        # L2 ORDER-FLOW GATE: don't buy into a move the book isn't backing.
        self.ma_book_gate_enabled = True
        self.ma_book_invert = False       # flip if data proves our book_imbalance sign is inverted
        self.ma_book_oppose = 0.5         # skip if book opposes the trade by >= this (|imb|)
        self.ma_extended_sep_pct = 0.05   # EMA sep (%) above which a move counts as "extended"
        self.ma_book_support_min = 0.15   # on an extended move, require book support >= this
        # LIQUIDITY WALLS (from L2 depth): support (bid wall) / resistance (ask wall)
        self.ma_wall_gate_enabled = True
        self.ma_wall_near_pct = 0.06      # a wall within this % of price is "near" (block/confirm)
        self.ma_wall_bounce_signal = True # bounce off a bid wall → CALL entry (stop below wall)
        # POSITIVE CONFIRMATION: require order-flow OR wall to back an EMA entry (raises
        # win rate by skipping marginal crosses with no flow behind them).
        self.ma_require_confirm = True
        self.ma_book_confirm_min = 0.15   # book must support the direction by >= this to confirm
        self.ma_max_vwap_z = 1.5          # don't ENTER when price already this many σ past
                                          # VWAP in the trade's direction (exhaustion guard);
                                          # 0 disables. Catches the late cross at the extreme.
        self.ma_min_separation_pct = 0.015 # min |fast-slow|/price (%) to enter — filters
                                          # marginal 1-cent crosses that whipsaw; 0 disables.
        # 9/21/50 triple: min |21-50| separation (% of price) for a regime to count as a
        # real leg — stops the kiss-flicker machine-gun entries in low-ER chop.
        self.ma_tri_min_sep_pct = 0.03
        # require L2 order-flow/wall confirmation on 9/21/50 entries (the X strategy's
        # winning ingredient — added after 9/21/50 got stopped on half its geometry-only
        # entries while L2-confirmed X went 100%).
        self.ma_tri_require_l2 = True
        # GLOBAL CHOP SHUTOFF: block ALL entries on a symbol whose efficiency ratio is
        # below this (directionless chop = every trend-scalp entry whipsaw-stops). 0=off.
        self.chop_shutoff_er = 0.20
        # APEX (synthesis strategy) filters — stacked for max selectivity / high win rate:
        self.apex_min_er = 0.30       # STRICT efficiency floor (stricter than the global)
        self.apex_max_vwap_z = 2.0    # skip if already this many σ extended in the trade dir
        self.apex_trend_bars = 10     # dominant-trend (slow-EMA slope) lookback for agreement
        # FAST EXIT — "slow in, fast out": exit when price crosses back through the
        # FAST EMA (leads the 8/21 cross by a few min, captures the reversal earlier).
        self.ma_fast_exit_enabled = True
        self.ma_fast_exit_buffer_pct = 0.0   # small buffer past fast EMA to avoid noise
        self.ma_fast_exit_min_hold_s = 20    # settle-in: don't fast-exit right at entry
        self.ma_min_er = 0.33                # CHOP GATE: skip entries when recent
                                             # efficiency-ratio < this (choppy tape). 0 = off.
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
        # Post-open blackout: wait this many minutes AFTER the 09:30 ET open before
        # scalping (the opening auction/first minutes are the whipsaw-iest — usually
        # better to let the range establish). 0 = trade from the bell. Market open is
        # 14:30 UTC (09:30 ET); close is 20:00 UTC (16:00 ET).
        self.open_blackout_minutes = 10
        # Adaptive resolution (AI-tunable): how the regime/ER series is sampled.
        # regime_sample_seconds = spacing between samples (coarser = sees slow
        # grinds, finer = sees fast moves). er_window = how many recent samples ER
        # spans. The AI adjusts these per regime so a slow drift isn't drowned by
        # tick noise (low res) nor a fast move smeared (too coarse).
        self.regime_sample_seconds = ind.REGIME_SAMPLE_SECONDS  # 15
        self.er_window = ind.ER_WINDOW                          # 16 (~4 min @ 15s)

        # state
        self._prices: Dict[str, Deque[Tuple[datetime, float]]] = {s: deque(maxlen=ind.PRICE_WINDOW) for s in symbols}
        self._ema: Dict[str, EmaPair] = {s: EmaPair(self.ma_fast, self.ma_slow, self.ma_tf_seconds) for s in symbols}
        # separate EMA pair for the 'ai' strategy (tuned independently by the supervisor)
        self._ema_ai: Dict[str, EmaPair] = {s: EmaPair(self.ma_fast_ai, self.ma_slow_ai, self.ma_tf_seconds) for s in symbols}
        # the mechanical macross now uses a 9/21/50 triple-EMA confluence system
        # (entry: 21×50 cross confirmed by 9-vs-21; exit: opposite 9×21 cross).
        self._ema_tri: Dict[str, EmaTriple] = {s: EmaTriple(self.ma_tf_seconds) for s in symbols}
        for _t in self._ema_tri.values():
            _t.min_sep_pct = self.ma_tri_min_sep_pct
        from .candles import CandleSeries
        self._candles: Dict[str, CandleSeries] = {s: CandleSeries(self.ma_tf_seconds) for s in symbols}
        # big-candle / W reversal overlay state (per symbol)
        self._block_puts: Dict[str, bool] = {}       # big red flush → don't sell puts
        self._block_calls: Dict[str, bool] = {}      # big green spike → don't buy calls
        self._watch_call: Dict[str, bool] = {}       # big red armed a reversal-up watch
        self._watch_put: Dict[str, bool] = {}        # big green armed a reversal-down watch
        self._reversal_stop: Dict[str, float] = {}   # hard stop level from the reversal candle
        self._reversal_flip: Dict[str, tuple] = {}   # (side, stop_level) — a confirmed W/M flip to act on
        self._cross_lockout: Dict[str, bool] = {}    # after reversal_stop: locked out until a FRESH EMA cross
        self._ma_last_regime: Dict[str, Optional[str]] = {}  # last regime we OPENED on (macross)
        self._fresh_cross: Dict[str, Optional[str]] = {}  # this-tick EMA cross event (computed once, shared by strategies)
        self._fresh_cross_ai: Dict[str, Optional[str]] = {}  # this-tick cross for the AI strategy's own EMA pair
        self._last_entry_at_strat: Dict[tuple, datetime] = {}  # per-(strategy,symbol) entry throttle
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
        self._kill_baseline = 0.0   # daily-loss measured from here (rebased on manual reset)
        self._pnl_day = date.today()

        # AI state
        self._ai_mode: Optional[str] = None
        self._ai_reason: Optional[str] = None
        self._ai_assessment: Optional[str] = None
        self._ai_levels: Dict[str, Any] = {}
        self._ai_error: Optional[str] = None
        self._ai_fail_streak: int = 0  # consecutive supervisor parse failures
        self._ai_log: Deque[Dict[str, Any]] = deque(maxlen=40)
        self._ai_last_run: Optional[datetime] = None
        self._ai_tune_last_run: Optional[datetime] = None
        # AI meta-control: in 'ai' mode the supervisor may turn ON the mechanical
        # macross strategy to run ALONGSIDE its own trading when the tape looks like a
        # clean trend (its 'enable_macross' verdict). macross positions are tracked
        # separately (labeled 'macross'); the AI turns it back OFF on chop.
        self._ai_enable_macross: bool = False
        self._active_strategy_spec: Optional[Dict[str, Any]] = None
        # in-flight open guard: (strategy, symbol) pairs with an _open_scalp awaiting.
        # _open_scalp AWAITS (chain lookup + order fill) for seconds before the position
        # registers in self._open — without this reservation the scan loop re-fires and
        # opens DUPLICATE positions for the same (symbol, strategy) in that window.
        self._opening: set = set()

        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._seeded = False   # True once EMAs/candles warmed from history (loop retries)
        self._init_db()
        self._load_state()       # restore enabled/mode/tunables across restarts
        self._rebuild_emas()     # rebuild EMAs if ma_fast/slow/tf were restored
        self._rehydrate_today()  # rebuild day P&L/wins/exits from persisted trades

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def start(self):
        if self._running:
            return
        self._running = True
        try:
            # SEED FIRST (before rehydrate): warm bar-EMAs + OHLC from 1-min history.
            # May no-op if the feed client isn't connected yet → the loop RETRIES until
            # it succeeds. Seeding must precede rehydrate so rehydrate's leg_traded()
            # marks aren't wiped by seed_from_closes resetting the leg.
            self._seeded = bool(await asyncio.to_thread(self._seed_emas))
            if self._seeded:
                logger.info("scalper EMAs/candles seeded at startup")
        except Exception as e:
            self._seeded = False
            logger.warning("scalper EMA seed failed: %s", e)
        try:
            await asyncio.to_thread(self._rehydrate_open_scalps)
        except Exception as e:
            logger.warning("scalper rehydrate failed: %s", e)
        self._task = asyncio.create_task(self._loop(), name="scalper-engine")

    def _seed_emas(self) -> bool:
        """Warm each symbol's bar-EMAs + OHLC candles from Schwab 1-min history so
        macross/the AI have chart context immediately (no ~20-min warmup after a
        restart). Returns True once at least one symbol seeded — the loop RETRIES this
        until it succeeds, because at start() the feed client may not be connected yet
        (in which case this silently no-ops and the AI is left blind: 'no OHLC history')."""
        feed = self._get_stream()
        client = getattr(feed, "_client", None) if feed else None
        if client is None:
            return False
        now = datetime.utcnow()
        seeded_any = False
        # Use an explicit date RANGE (last ~2 days) — periodType=day returns only the
        # PRIOR completed session (misses today's live bars), which anchored the EMAs
        # to yesterday's stale close. startDate/endDate returns bars up to the minute.
        start_ms = int((time.time() - 2 * 86400) * 1000)
        end_ms = int(time.time() * 1000)
        for sym in self.symbols:
            try:
                r = client.price_history(sym, frequencyType="minute", frequency="1",
                                         startDate=start_ms, endDate=end_ms).json()
                cands = r.get("candles", [])
                closes = [x.get("close") for x in cands if x.get("close")]
                ema = self._ema.get(sym)
                if ema is not None and closes:
                    ema.seed_from_closes(closes[-120:], now)  # last ~2h of bars is plenty
                    logger.info("Seeded %s EMAs from %d bars: fast=%.2f slow=%.2f (%s)",
                                sym, min(len(closes), 120), ema.fast or 0, ema.slow or 0, ema.spread_dir())
                ema_ai = self._ema_ai.get(sym)   # warm the AI strategy's own EMA pair too
                if ema_ai is not None and closes:
                    ema_ai.seed_from_closes(closes[-120:], now)
                tri = self._ema_tri.get(sym)     # warm the 9/21/50 triple (needs >=50 bars)
                if tri is not None and closes:
                    tri.seed_from_closes(closes[-120:], now)
                cs = self._candles.get(sym)
                if cs is not None:
                    for x in cands[-60:]:   # seed OHLC for big-candle/W detection + chart
                        if x.get("close"):
                            # Schwab 'datetime' is epoch MS → bar-open epoch SECONDS
                            _ts = int((x.get("datetime") or 0) / 1000)
                            cs.seed_bar(x.get("open"), x.get("high"), x.get("low"),
                                        x.get("close"), _ts)
                if closes:
                    seeded_any = True
            except Exception as e:
                logger.debug("seed %s failed: %s", sym, e)
        return seeded_any

    def _rehydrate_open_scalps(self):
        """Re-track open single-leg scalp positions from the broker after a restart so
        a deploy doesn't ORPHAN a live position (the engine resumes managing it)."""
        try:
            poss = self.alpaca.get_option_positions()
        except Exception as e:
            logger.warning("scalper rehydrate positions failed: %s", e)
            return
        import re
        recovered = 0
        for p in (poss or []):
            occ = p.get("occ"); qty = p.get("qty") or 0
            if qty <= 0:  # scalps are long single legs (qty>0); skip short legs (those are spreads')
                continue
            # only NEAR-TERM (0-2 DTE) legs are scalps; 30-45 DTE long puts are the
            # spread engine's LONG legs — don't hijack them.
            exp = p.get("expiration")
            if exp:
                try:
                    dte = (date.fromisoformat(exp) - date.today()).days
                    if dte > 2:
                        continue
                except Exception:
                    pass
            m = re.match(r"^([A-Z]+)\d", occ or "")
            root = m.group(1) if m else None
            if root not in self.symbols:
                continue
            entry = float(p.get("avg_entry_price") or 0)
            if entry <= 0:
                continue
            # rehydrated legs are labeled 'macross3' so they're managed by the CURRENT
            # mechanical exit ladder (EMA-50 stop / 9×21 cross) — never the retired
            # 'macross' (MA-old) strategy, which is fully decommissioned.
            sc = _OpenScalp(occ, root, p.get("type") or "call", int(qty), entry, None, strategy="macross3")
            self._open[sc.key] = sc
            recovered += 1
            # mark this symbol's current 21×50 leg as already-traded so the post-restart
            # re-seed doesn't open a SECOND macross3 position on top of the rehydrated
            # one (that stacking is what accumulated duplicate SPY/QQQ calls).
            tri = self._ema_tri.get(root)
            if tri is not None:
                tri.leg_traded()
        if recovered:
            logger.info("Scalper rehydrated %d open scalp(s) from broker", recovered)

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

    def reset_kill(self) -> dict:
        """Manually clear the kill switch / profit lock so testing can continue (paper).
        Re-bases the daily-loss baseline to the CURRENT realized P&L so the limit
        measures losses FROM HERE — otherwise the existing drawdown instantly re-trips
        it. The day's true realized P&L is preserved for the record."""
        self._kill_tripped = False
        self._profit_lock_tripped = False
        self._kill_baseline = self._daily_realized_pnl   # losses counted from this point
        self._daily_peak_pnl = self._daily_realized_pnl
        logger.warning("KILL/LOCK manually reset — baseline rebased to $%.0f", self._daily_realized_pnl)
        return {"kill_switch_tripped": False, "profit_lock_tripped": False,
                "day_pnl": round(self._daily_realized_pnl, 2),
                "kill_baseline": round(self._kill_baseline, 2)}

    def set_live_trading(self, on: bool) -> dict:
        """Switch execution between PAPER and LIVE (real money). Guarded:
        - LIVE requires a live Alpaca client (built only if ALPACA_LIVE_API_KEY/
          SECRET were provided) — refuses otherwise.
        - Always returns the resulting mode so the caller/UI can confirm.
        Does NOT auto-enable trading; the engine's enabled flag is separate."""
        if on:
            if self._alpaca_live is None:
                return {"ok": False, "live_trading": False,
                        "error": "No live Alpaca keys configured (ALPACA_LIVE_API_KEY/SECRET). Staying on paper."}
            # Hard gate: verify the live account actually authenticates + is
            # active/funded/options-approved BEFORE routing real orders to it.
            health = self._alpaca_live.account_health()
            if not health.get("ok"):
                return {"ok": False, "live_trading": False,
                        "error": f"Live account not ready: {health.get('error') or health}. Staying on paper."}
            self.live_trading = True
            self.alpaca = self._alpaca_live
            logger.warning("*** LIVE TRADING ENABLED — orders route to the REAL Alpaca account (equity $%s) ***", health.get("equity"))
        else:
            self.live_trading = False
            self.alpaca = self._alpaca_paper
            logger.info("Execution set to PAPER")
        self._save_state()
        return {"ok": True, "live_trading": self.live_trading}

    # ── state persistence (survive restarts) ───────────────────────────────
    _STATE_KEYS = ("signal_mode", "enabled", "entry_threshold", "stop_pct",
                   "profit_target_pct", "time_stop_seconds", "chop_min_efficiency",
                   "min_recent_range_pct", "chop_guard_enabled", "velocity_gate_enabled",
                   "profit_lock_arm_usd", "profit_giveback_frac",
                   "ai_supervisor_seconds", "ai_tune_seconds", "max_concurrent",
                   "per_trade_usd", "budget_usd", "max_dte", "daily_loss_kill_usd",
                   "daily_entry_start_utc", "daily_entry_end_utc",
                   "open_blackout_minutes",
                   "regime_sample_seconds", "er_window",
                   "ma_fast", "ma_slow", "ma_tf_seconds", "ma_fast_ai", "ma_slow_ai",
                   "ma_profit_target_pct",
                   "ma_stop_pct", "ma_trail_giveback_pct", "ma_trail_floor_pct",
                   "ma_trail_activate_pct", "ma_peak_lock_arm_pct", "ma_peak_giveback_frac",
                   "ma_max_vwap_z", "ma_min_separation_pct", "ma_tri_min_sep_pct", "ma_tri_require_l2",
                   "chop_shutoff_er", "apex_min_er", "apex_max_vwap_z", "apex_trend_bars",
                   "ma_fast_exit_enabled", "ma_fast_exit_buffer_pct", "ma_fast_exit_min_hold_s",
                   "ma_min_er", "ma_breakeven_arm_pct", "ma_breakeven_floor_pct",
                   "ma_book_gate_enabled", "ma_book_invert", "ma_book_oppose",
                   "ma_extended_sep_pct", "ma_book_support_min",
                   "ma_wall_gate_enabled", "ma_wall_near_pct", "ma_wall_bounce_signal",
                   "ma_require_confirm", "ma_book_confirm_min",
                   "ma_underlying_stop_pct", "ma_stop_pct_backstop", "ma_time_stop_backstop_s",
                   "ma_min_hold_before_crossback_s", "ma_trend_bias_enabled", "ma_trend_bias_bars",
                   "ma_failed_reversal_enabled", "ma_failed_reversal_grace_s",
                   "ma_failed_reversal_min_pnl_pct", "ma_failed_reversal_needs_progress")

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

    def _rebuild_emas(self):
        """Recreate the per-symbol EmaPair objects to match the current
        ma_fast/ma_slow/ma_tf_seconds (after a state restore or a config/AI tune)."""
        self._ema = {s: EmaPair(self.ma_fast, self.ma_slow, self.ma_tf_seconds)
                     for s in self.symbols}
        self._ema_ai = {s: EmaPair(self.ma_fast_ai, self.ma_slow_ai, self.ma_tf_seconds)
                        for s in self.symbols}
        self._ema_tri = {s: EmaTriple(self.ma_tf_seconds) for s in self.symbols}
        for _t in self._ema_tri.values():
            _t.min_sep_pct = self.ma_tri_min_sep_pct

    def _rehydrate_today(self):
        """Rebuild today's P&L / wins / exits / entries from persisted trades so a
        restart doesn't zero the day's stats (trades persist in SQLite; the
        in-memory aggregates didn't). Day boundary = local midnight."""
        try:
            import time as _t
            day_start_ms = int(datetime.combine(date.today(), datetime.min.time()).timestamp() * 1000)
            c = sqlite3.connect(DB_PATH)
            rows = c.execute(
                "SELECT pnl, opened_ts FROM scalp_trades WHERE ts >= ?", (day_start_ms,)
            ).fetchall()
            c.close()
            if not rows:
                return
            pnl_run = 0.0; peak = 0.0; wins = 0
            for pnl, _opened in rows:
                p = float(pnl or 0)
                pnl_run += p
                if p > 0:
                    wins += 1
                if pnl_run > peak:
                    peak = pnl_run
            self._daily_realized_pnl = pnl_run
            self._daily_peak_pnl = peak
            self._exits_today = len(rows)
            self._wins_today = wins
            self._entries_today = len(rows)  # each closed trade had an entry
            logger.info("Rehydrated today: pnl=$%.0f exits=%d wins=%d", pnl_run, len(rows), wins)
        except Exception as e:
            logger.debug("rehydrate_today failed: %s", e)

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
                if sym in self._ema:
                    self._ema[sym].update(px, now)
                    # compute the fresh-cross ONCE per tick so multiple strategies can
                    # share it (ema.cross advances state, so it must be called once)
                    self._fresh_cross[sym] = self._ema[sym].cross(px)
                if sym in self._ema_ai:
                    self._ema_ai[sym].update(px, now)  # the AI strategy's own EMA pair
                if sym in self._ema_tri:
                    self._ema_tri[sym].update(px, now)  # the macross 9/21/50 triple
                if sym in self._candles:
                    closed = self._candles[sym].update(px, now)
                    if closed is not None:
                        self._on_candle_close(sym)

    def _on_candle_close(self, sym):
        """Big-candle + W reversal OVERLAY (macross only) — price-action that LEADS the
        lagging 8/21. Runs when a 1-min bar closes. Sets per-symbol flags the entry +
        exit logic honor:
          BIG RED down  → tighten any open PUT trail, BLOCK new puts, ARM watch-for-call
          BIG GREEN up   → tighten any open CALL trail, BLOCK new calls, ARM watch-for-put
          W (double-bottom) + fast<slow → FLIP to CALL (lead the cross), stop below the low
          M (double-top)   + fast>slow → FLIP to PUT, stop above the high
        Flags clear when the opposite condition or a normal state resumes."""
        if not self._is_macross_mode():
            return   # overlays feed the macross-family entry path (macross / macross_x / all)
        cs = self._candles.get(sym)
        if cs is None:
            return
        big = cs.big_candle()
        ema = self._ema.get(sym)
        regime = ema.spread_dir() if ema else None
        # default: clear one-bar blocks unless re-set below
        self._block_puts[sym] = False
        self._block_calls[sym] = False
        if big is not None:
            color, ratio, candle = big
            if color == "red":
                # climactic flush → don't chase puts; protect open puts; watch for reversal up
                self._block_puts[sym] = True
                self._watch_call[sym] = True
                self._watch_put[sym] = False
                for sc in self._open.values():
                    if sc.underlying == sym and sc.direction == "put":
                        sc.tight_trail = True   # exit logic tightens the trail
            elif color == "green":
                self._block_calls[sym] = True
                self._watch_put[sym] = True
                self._watch_call[sym] = False
                for sc in self._open.values():
                    if sc.underlying == sym and sc.direction == "call":
                        sc.tight_trail = True
        # EMA-ENGULFING CONTINUATION (user's SPY case): a big candle that opens beyond
        # the slow EMA and closes beyond the fast EMA, WITH the regime agreeing, is a
        # high-conviction continuation reclaim → arm a with-trend entry (bypasses the
        # marginal gates like the W flip). Only when flat in the symbol.
        held_sym = any(s.underlying == sym for s in self._open.values())
        if not held_sym and ema is not None:
            eng = cs.ema_engulf(ema.fast, ema.slow)
            if eng == "call" and regime == "call":
                last = cs.last_closed()
                self._reversal_flip[sym] = ("call", last.l if last else None)  # stop below the bar
            elif eng == "put" and regime == "put":
                last = cs.last_closed()
                self._reversal_flip[sym] = ("put", last.h if last else None)   # stop above the bar
        # WALL BOUNCE (user: walls find support → add call entry signal): if the just-
        # closed candle dipped to a persisted BID WALL (support) and closed back above
        # it = demand defended → CALL, hard stop just BELOW the wall (defined risk).
        # Mirror: candle rejected off an ASK WALL (resistance) → PUT, stop above it.
        if not held_sym and self.ma_wall_bounce_signal:
            last = cs.last_closed()
            if last is not None:
                bw = self._nearest_wall(sym, "bid")   # support
                aw = self._nearest_wall(sym, "ask")   # resistance
                near = self.ma_wall_near_pct / 100.0
                if bw and last.l <= bw[0] * (1 + near) and last.c > bw[0]:
                    self._reversal_flip[sym] = ("call", bw[0] * (1 - 0.0005))  # stop just below wall
                elif aw and last.h >= aw[0] * (1 - near) and last.c < aw[0]:
                    self._reversal_flip[sym] = ("put", aw[0] * (1 + 0.0005))   # stop just above wall
        # W / M confirmation → flip side ahead of the MA cross
        if self._watch_call.get(sym) and cs.is_W() and regime == "put":
            # bull reversal confirmed while EMAs still say put → flip to calls
            last = cs.last_closed()
            self._reversal_flip[sym] = ("call", last.l if last else None)
            self._watch_call[sym] = False
        elif self._watch_put.get(sym) and cs.is_M() and regime == "call":
            last = cs.last_closed()
            self._reversal_flip[sym] = ("put", last.h if last else None)
            self._watch_put[sym] = False

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
        ema = self._ema.get(sym)
        ema_fast = round(ema.fast, 2) if (ema and ema.fast is not None) else None
        ema_slow = round(ema.slow, 2) if (ema and ema.slow is not None) else None
        self._chart_hist[sym].append((t, round(px, 2),
                                      round(mean, 2) if mean is not None else None,
                                      round(upper, 2) if upper is not None else None,
                                      round(lower, 2) if lower is not None else None,
                                      ema_fast, ema_slow))

    def candles(self, sym) -> Dict[str, Any]:
        """1-min candlestick chart payload for a symbol:
          bars:   [{t,o,h,l,c}]           closed 1-min OHLC (oldest→newest)
          emas:   [{ema9,ema21,ema50}]    9/21/50 aligned 1:1 with bars
          live:   {t,o,h,l,c} | None      the still-forming current bar
          ema_now:{ema9,ema21,ema50,regime}   current EMA values (live last point)
          markers:[{t, side, kind}]       9/21 crossover events on closed bars
        The chart draws candles + three EMA lines + up/down cross markers, all on
        the SAME closed-bar basis the engine trades on (no intra-minute wobble)."""
        cs = self._candles.get(sym)
        tri = self._ema_tri.get(sym)
        if cs is None or tri is None:
            return {"symbol": sym, "bars": [], "emas": [], "live": None,
                    "ema_now": {}, "markers": []}
        bars = cs.ohlc_ts_rows(60)
        emas = tri.tri_hist_rows(len(bars))
        # Align lengths defensively (seed order can differ by a bar).
        n = min(len(bars), len(emas))
        bars, emas = bars[-n:], emas[-n:]
        # Cross markers: a 9×21 sign flip between consecutive aligned EMA rows.
        markers = []
        for i in range(1, len(emas)):
            prev = emas[i - 1]["ema9"] - emas[i - 1]["ema21"]
            cur = emas[i]["ema9"] - emas[i]["ema21"]
            if prev <= 0 < cur:
                markers.append({"t": bars[i]["t"], "side": "call", "kind": "9x21"})
            elif prev >= 0 > cur:
                markers.append({"t": bars[i]["t"], "side": "put", "kind": "9x21"})
        live_c = cs.current()
        live = ({"t": live_c.ts, "o": round(live_c.o, 2), "h": round(live_c.h, 2),
                 "l": round(live_c.l, 2), "c": round(live_c.c, 2)}
                if live_c is not None else None)
        return {"symbol": sym, "bars": bars, "emas": emas, "live": live,
                "ema_now": tri.values(), "markers": markers}

    def chart_history(self, sym) -> List[Dict[str, Any]]:
        """Live chart series for a symbol: [{t, price, mean, upper, lower, ema_fast, ema_slow}]."""
        out = []
        for row in self._chart_hist.get(sym, ()):
            t, p, m, u, lo = row[0], row[1], row[2], row[3], row[4]
            ef = row[5] if len(row) > 5 else None
            es = row[6] if len(row) > 6 else None
            out.append({"t": t, "price": p, "mean": m, "upper": u, "lower": lo,
                        "ema_fast": ef, "ema_slow": es})
        return out

    def _record_regime_sample(self, sym, px, now):
        self._anchor_sum[sym] = self._anchor_sum.get(sym, 0.0) + px
        self._anchor_n[sym] = self._anchor_n.get(sym, 0) + 1
        self._anchor_sq[sym] = self._anchor_sq.get(sym, 0.0) + px * px
        s = self._regime_samples.setdefault(sym, deque(maxlen=ind.REGIME_ER_WINDOW))
        if s and (now - s[-1][0]).total_seconds() < self.regime_sample_seconds:
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

    def _nearest_wall(self, sym, side: str):
        """Nearest persisted wall on a side ('bid'=support below / 'ask'=resistance
        above), as (price, size, persist) or None. From the L2 book."""
        stream = self._get_stream()
        if stream is None or not hasattr(stream, "walls"):
            return None
        try:
            w = stream.walls(sym) or {}
        except Exception:
            return None
        lst = w.get(side) or []
        if not lst:
            return None
        px = self._latest_price(sym)
        if not px:
            return None
        # nearest to current price
        return min(lst, key=lambda t: abs(t[0] - px))

    def _wall_context(self, sym):
        """Support/resistance from walls relative to price → {'support': px, 'resistance': px,
        'near_support_pct', 'near_resistance_pct'}. Support = bid wall below; resistance =
        ask wall above. Used to CONFIRM calls (holding above support) / puts (under resistance)."""
        px = self._latest_price(sym)
        if not px:
            return {}
        out = {}
        bw = self._nearest_wall(sym, "bid")
        aw = self._nearest_wall(sym, "ask")
        if bw and bw[0] <= px:      # bid wall at/below = support
            out["support"] = bw[0]
            out["near_support_pct"] = (px - bw[0]) / px * 100.0
        if aw and aw[0] >= px:      # ask wall at/above = resistance
            out["resistance"] = aw[0]
            out["near_resistance_pct"] = (aw[0] - px) / px * 100.0
        return out

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

    async def _refresh_ladder(self):
        """Build the ATM±2 options ladder per symbol for the side the engine is
        currently leaning (EMA 21×50 regime). Runs off the hot path (~every 6s);
        the UI reads the cached result from status(). Best-effort — a failure just
        leaves the prior ladder in place."""
        now = datetime.utcnow()
        if self._ladder_at and (now - self._ladder_at).total_seconds() < 6:
            return
        self._ladder_at = now
        for sym in self.symbols:
            try:
                spot = self._latest_price(sym)
                if not spot:
                    continue
                tri = self._ema_tri.get(sym)
                side = (tri.regime_2150() if tri else None) or "call"
                contracts = await asyncio.to_thread(
                    self.alpaca.find_contracts, sym, side, self.max_dte, spot)
                if not contracts:
                    continue
                # 5 strikes centered on the money (ATM ±2)
                contracts.sort(key=lambda c: abs(c["strike"] - spot))
                near = sorted(contracts[:5], key=lambda c: c["strike"])
                rows = []
                for c in near:
                    q = await self._option_quote(c["symbol"])
                    mid = q.get("mid_price") or q.get("mid") or q.get("ask")
                    per = self.per_trade_usd
                    qty = int(per // (float(mid) * 100.0)) if mid else 0
                    rows.append({
                        "strike": c["strike"], "occ": c["symbol"],
                        "mid": round(float(mid), 2) if mid else None,
                        "bid": round(float(q.get("bid")), 2) if q.get("bid") else None,
                        "ask": round(float(q.get("ask")), 2) if q.get("ask") else None,
                        "qty": qty,
                        "atm": abs(c["strike"] - spot) == min(abs(x["strike"] - spot) for x in near),
                    })
                self._ladder[sym] = {"side": side, "spot": round(spot, 2), "strikes": rows}
            except Exception as e:
                logger.debug("ladder %s failed: %s", sym, e)

    async def _position_view(self) -> list:
        """Enriched open positions for the UI: live mid, P&L $/%, peak %, and the
        active stop/target so the panel shows a real-time performance indicator."""
        out = []
        for sc in self._open.values():
            try:
                q = await self._option_quote(sc.occ)
                mid = q.get("mid_price") or q.get("bid")
                mid_f = float(mid) if mid else None
                pnl_pct = ((mid_f - sc.entry_mid) / sc.entry_mid * 100.0) if (mid_f and sc.entry_mid) else None
                pnl_usd = ((mid_f - sc.entry_mid) * sc.qty * 100.0) if (mid_f and sc.entry_mid) else None
                peak_pct = ((sc.peak_mid - sc.entry_mid) / sc.entry_mid * 100.0) if sc.entry_mid else None
            except Exception:
                mid_f = pnl_pct = pnl_usd = peak_pct = None
            out.append({
                "occ": sc.occ, "scalp_key": sc.key, "underlying": sc.underlying,
                "direction": sc.direction, "qty": sc.qty,
                "entry_mid": round(sc.entry_mid, 2),
                "mid": round(mid_f, 2) if mid_f else None,
                "pnl_pct": round(pnl_pct, 1) if pnl_pct is not None else None,
                "pnl_usd": round(pnl_usd, 0) if pnl_usd is not None else None,
                "peak_pct": round(peak_pct, 1) if peak_pct is not None else None,
                "strategy": getattr(sc, "strategy", None),
                "held_s": round((datetime.utcnow() - sc.opened_at).total_seconds(), 0),
                # config-fixed risk levels shown for context (the stop is engine-managed)
                "stop_pct": round(self.ma_stop_pct_backstop * 100, 0) if sc.strategy == "macross3" else round(self.ma_stop_pct * 100, 0),
                "ema50_stop_armed": getattr(sc, "ema50_stop_armed", False),
            })
        self._position_cache = out
        return out

    # ── indicator wrappers (bind engine state to pure indicators) ──────────
    def _latest_price(self, sym):
        w = self._prices.get(sym)
        return w[-1][1] if w else None

    def _efficiency_ratio(self, sym): return ind.efficiency_ratio(self._regime_samples.get(sym), self.er_window)
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
        if self.signal_mode == "macross":
            return False  # the cross direction IS the trade; never invert it
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
        # LOOSENED: in macross the AI is INFO-ONLY — it never blocks entries (its
        # chop-pause was killing all volume). Only the dedicated 'ai' mode honors
        # a pause verdict.
        return self.signal_mode == "ai" and self._ai_mode == "pause"

    _MACROSS_MODES = ("macross", "macross_x", "all")

    def _is_macross_mode(self) -> bool:
        return self.signal_mode in self._MACROSS_MODES

    def _active_scalp_strategies(self):
        """Which labeled scalp strategies are trading now (based on signal_mode).
        NOTE: the mechanical macross is now the 9/21/50 TRIPLE — labeled 'macross3' so
        its trades stay DISTINCT from the retired 8/21 'macross' trades in the DB."""
        if self.signal_mode == "macross":
            return ["macross3"]     # legacy: 9/21/50 alone (retired from 'all' — see below)
        if self.signal_mode == "macross_x":
            return ["macross_x"]
        if self.signal_mode == "all":
            # 9/21/50 (macross3) REMOVED — worst performer. 'all' now runs: apex (the
            # synthesis strategy — every winning filter stacked), X (cross+L2), and ai.
            return ["apex", "macross_x"] + (["ai"] if self._anthropic_key else [])
        if self.signal_mode == "apex":
            return ["apex"]
        return []   # legacy momentum/fade/auto/ai modes use the old single-position path

    def _l2_confirms(self, sym, direction) -> bool:
        """L2 order-flow confirmation for a directional entry: the book supports the
        direction (aligned imbalance >= threshold) OR a wall confirms it (support below
        for a call / resistance above for a put). This is the filter that made the X
        strategy 100% today — screens out crosses the order book doesn't back."""
        imb = self._book_imbalance(sym)
        if imb is not None and self.ma_book_invert:
            imb = -imb
        aligned = (imb if direction == "call" else -imb) if imb is not None else None
        book_ok = aligned is not None and aligned >= self.ma_book_confirm_min
        wc = self._wall_context(sym)
        wall_ok = ("support" in wc) if direction == "call" else ("resistance" in wc)
        return bool(book_ok or wall_ok)

    def _apex_signal(self, sym) -> Tuple[Optional[str], float]:
        """APEX — the synthesis strategy: every filter that WON across ~2 weeks of live
        A/B, stacked for maximum selectivity (few trades, high win rate).

          TRIGGER : a FRESH 8/21 EMA cross this bar (X's timing edge — enter at the
                    pivot, best price, so we're rarely underwater → tiny avg loss).
          + L2    : the order book / a wall CONFIRMS the direction (the single biggest
                    edge — made X 75-100%). No book backing → no trade.
          + TREND : the DOMINANT trend (slow-EMA slope) AGREES with the cross — never
                    fight the trend; only take a cross that CONTINUES it.
          + FLOW  : efficiency ratio >= a STRICT floor — chop stops every trade; only
                    engage a genuinely efficient, directional tape.
          + NOT EXTENDED: not already stretched from VWAP in the trade direction — don't
                    chase an exhausted move into a V-reversal (the give-back lesson).

        Exit uses the proven peak-giveback lock + underlying stop ladder (shared with X).
        """
        ema = self._ema.get(sym)
        px = self._latest_price(sym)
        if ema is None or not px:
            return None, 0.0
        # 1) TRIGGER — fresh 8/21 cross (shared, computed once per tick)
        cross = self._fresh_cross.get(sym)
        if cross is None:
            return None, 0.0
        # 2) L2 confirmation (X's edge)
        if not self._l2_confirms(sym, cross):
            return None, 0.0
        # 3) dominant-trend agreement — only take a cross that continues the trend
        trend = ema.slow_slope(self.apex_trend_bars)
        if trend is not None and trend != cross:
            return None, 0.0
        # 4) efficiency floor — strict; no chop
        er = self._efficiency_ratio(sym)
        if er is not None and er < self.apex_min_er:
            return None, 0.0
        # 5) not over-extended — don't chase into exhaustion (V-reversal risk)
        z = self._vwap_z(sym)
        if z is not None and self.apex_max_vwap_z > 0:
            if (cross == "call" and z > self.apex_max_vwap_z) or \
               (cross == "put" and z < -self.apex_max_vwap_z):
                return None, 0.0
        return cross, 1.0

    def _macross_triple_signal(self, sym) -> Tuple[Optional[str], float]:
        """The 'macross' strategy — 9/21/50 triple-EMA confluence + L2 confirmation:
          CALL: EMA21 above EMA50 AND EMA9 > EMA21   (fires once per 21×50 up-leg)
          PUT : EMA21 below EMA50 AND EMA9 < EMA21   (fires once per 21×50 down-leg)
          ...AND the L2 order book confirms the direction (added after 9/21/50 over-
          entered on pure geometry and got stopped on half its trades; the X strategy's
          L2 confirm was the winning ingredient — 100% vs 40% — so it's applied here too).
        Entry fires ONCE per 21×50 leg (restart-robust; separation-gated). Exit is the
        opposite 9×21 cross. One position per symbol (enforced by the scan loop)."""
        tri = self._ema_tri.get(sym)
        if tri is None:
            return None, 0.0
        # don't re-enter the same symbol while this strategy already holds it
        if any(s.underlying == sym and s.strategy == "macross3" for s in self._open.values()):
            return None, 0.0
        sig = tri.entry_signal()
        if not sig:
            return None, 0.0
        if self.ma_tri_require_l2 and not self._l2_confirms(sym, sig):
            return None, 0.0    # confluence cross with no L2/wall backing → skip
        return sig, 1.0

    def _cross_signal(self, sym) -> Tuple[Optional[str], float]:
        """The 'macross_x' strategy: enter ONLY on a FRESH EMA cross (the sign-flip
        event this bar), confirmed by L2 order flow (book supports) OR a wall (support
        below for a call / resistance above for a put). Deliberately SKIPS the state
        gates (ER / separation / exhaustion / trend-bias) — it's a pure cross + L2
        variant to A/B against the full-gate state-based macross."""
        ema = self._ema.get(sym)
        px = self._latest_price(sym)
        if ema is None or not px:
            return None, 0.0
        cross = self._fresh_cross.get(sym)   # sign-flip this tick (shared, computed once)
        if cross is None:
            return None, 0.0
        if not self._l2_confirms(sym, cross):
            return None, 0.0    # a cross with no L2/wall backing → skip
        return cross, 1.0

    def _ai_signal(self, sym) -> Tuple[Optional[str], float]:
        """The 'ai' strategy (in 'all' mode): trade the AI's OWN EMA regime, with the
        SUPERVISOR choosing HOW to trade it. The supervisor sets self._ai_mode each
        cadence:
          - 'momentum' → trade WITH the AI-EMA cross/regime (up=call, down=put)
          - 'fade'     → trade AGAINST it (fade the extreme)
          - 'pause'    → sit out (dangerous tape)
        Uses the AI's independently-tuned EMA pair (ma_fast_ai/ma_slow_ai) so the
        supervisor can adjust EMA lengths without touching macross/macross_x. A light
        ER chop gate keeps it from churning dead tape; otherwise the AI's mode call IS
        the gate — the whole point is to watch how the AI chooses vs the mechanical
        strategies."""
        mode = self._ai_mode
        # FADE DISABLED: the AI only trades WITH the regime (momentum) or sits out
        # (pause). Fade (counter-trend) was churning — a fade buys a put while the EMA
        # regime is 'call', but the cross_back exit fires the instant regime != direction
        # (true from entry), so every fade insta-exited in ~30s. Momentum + pause only.
        if mode != "momentum":
            return None, 0.0            # None/pause/fade → sit out
        ema = self._ema_ai.get(sym)
        px = self._latest_price(sym)
        if ema is None or not px:
            return None, 0.0
        regime = ema.spread_dir()       # 'call' if fast>slow, 'put' if fast<slow
        if regime is None:
            return None, 0.0
        # light chop gate — even the AI shouldn't scalp a flat efficiency-ratio tape
        if self.ma_min_er > 0:
            er = self._efficiency_ratio(sym)
            if er is not None and er < self.ma_min_er:
                return None, 0.0
        return regime, 1.0              # trade WITH the regime (momentum)

    def _momentum_signal(self, sym, strategy="macross") -> Tuple[Optional[str], float]:
        # ── MA-crossover mode (high frequency): trade the EMA regime ────────────
        if self._is_macross_mode():
            ema = self._ema.get(sym)
            px = self._latest_price(sym)
            if ema is None or not px:
                return None, 0.0
            fresh_cross = self._fresh_cross.get(sym)  # 'call'/'put' sign-flip this tick (shared)
            regime = ema.spread_dir()   # 'call' if fast>slow, 'put' if fast<slow
            if regime is None:
                return None, 0.0
            # REVERSAL-STOP LOCKOUT: after a reversal_stop the move is over and price is
            # chopping. Stay OUT of this symbol until a FRESH EMA cross (real new trend) —
            # the lagging regime persists through the consolidation and would churn.
            if self._cross_lockout.get(sym):
                if fresh_cross is None:
                    return None, 0.0    # still no new cross → stay locked out (skip the chop)
                self._cross_lockout[sym] = False  # a genuine cross re-establishes a trend
            # HIGHER-TF TREND BIAS (the "hold one put all the way" fix): the 1-min 8/21
            # flickers call/put on intra-trend bounces, so it churned instead of riding a
            # 75-min downtrend. The SLOPE of the SLOW (21) EMA is the DOMINANT trend —
            # only take the regular regime entry WITH it (falling→puts, rising→calls);
            # ignore the counter-trend flickers. (Reversal FLIPS below are exempt — a
            # confirmed W/M is meant to catch the turn against the current trend.)
            if self.ma_trend_bias_enabled and self._reversal_flip.get(sym) is None:
                bias = ema.slow_slope(self.ma_trend_bias_bars)
                if bias is not None and regime != bias:
                    return None, 0.0    # regime fights the dominant slow-EMA trend → skip flicker
            # REVERSAL-FLIP OVERRIDE (W/M price-action LEADS the lagging MA): a confirmed
            # double-bottom → CALL even while EMAs say put (double-top → PUT). It
            # short-circuits the MARGINAL gates (separation, price-reclaim), BUT it must
            # STILL respect the ER CHOP GATE — a W / engulf / wall-bounce in dead chop
            # (ER ~0.06) is noise, not conviction, and was reintroducing chop churn.
            flip = self._reversal_flip.get(sym)
            if flip is not None:
                if any(s.underlying == sym and s.strategy == strategy for s in self._open.values()):
                    return None, 0.0    # holding (this strategy) — let exit logic run; flip acts once flat
                if self.ma_min_er > 0:
                    er = self._efficiency_ratio(sym)
                    if er is not None and er < self.ma_min_er:
                        self._reversal_flip.pop(sym, None)  # stale in chop — drop it
                        return None, 0.0    # price-action signal in chop = noise, skip
                return flip[0], 1.0     # (side, strength); stop applied in _open_scalp
            # BIG-CANDLE BLOCKS: don't chase a side that just had a climactic candle
            # against a reversal (big red flush → block puts; big green spike → block calls)
            if regime == "put" and self._block_puts.get(sym):
                return None, 0.0
            if regime == "call" and self._block_calls.get(sym):
                return None, 0.0
            # STATE-based entry: while FLAT in this symbol, if the EMA regime is
            # directional, be IN it. This catches the move in progress at startup,
            # AND re-enters after a position closes for any reason (time_stop, trail,
            # stop) while the regime is still valid — so we don't sit flat in a live
            # put/call regime. The "one position per symbol" rule (in _scan_for_entries)
            # prevents stacking; the entry throttle paces re-entries.
            if any(s.underlying == sym and s.strategy == strategy for s in self._open.values()):
                return None, 0.0        # already holding this symbol (this strategy) — let the exit logic run it
            # PRICE-RECLAIM GATE (V-reversal fix): the 8/21 regime LAGS a sharp V, so
            # after a bounce it still says 'put' while price has reclaimed up — leading
            # to puts sold into a rising post-V price. Require PRICE to confirm the
            # regime vs the fast EMA: a put needs price BELOW the fast EMA (still down),
            # a call needs price ABOVE it. Once price reclaims the fast EMA, stop selling
            # that side even though the slow cross hasn't flipped yet. Same line as the
            # fast-exit → coherent in & out; kills the V-churn.
            if self.ma_fast_exit_enabled and ema.fast is not None:
                if regime == "put" and px > ema.fast:
                    return None, 0.0    # price reclaimed above fast EMA — V up, don't sell puts
                if regime == "call" and px < ema.fast:
                    return None, 0.0    # price below fast EMA — V down, don't buy calls
            # SEPARATION GUARD: only enter when the EMAs have real separation, not a
            # 1-cent "kiss" that reverts in seconds (the 3s whipsaw cross_backs). The
            # gap must be >= ma_min_separation_pct of price = an ESTABLISHED regime.
            if self.ma_min_separation_pct > 0:
                sep = ema.spread_pct()
                if sep is not None and sep < self.ma_min_separation_pct:
                    return None, 0.0    # marginal cross — wait for conviction
            # CHOP GATE: the regime fires continuously, but in choppy/mean-reverting
            # tape it buys the move right as it exhausts → instant reversal/stop (the
            # 0/5 put day). Require the recent move to be EFFICIENT (trending), not
            # oscillating. ER low = back-and-forth chop → stand down. Keeps frequency
            # on trending mornings, kills the chop-churn that loses.
            # A FRESH decisive cross (real sign-flip, this bar) IS the move starting —
            # ER lags (it still reflects the PRIOR chop), so it blocks the START of moves
            # (missed the 11:47 down-cross ride). Let a fresh cross through the ER gate;
            # only the persisting (stale) regime must clear the chop filter.
            if self.ma_min_er > 0 and fresh_cross is None:
                er = self._efficiency_ratio(sym)
                if er is not None and er < self.ma_min_er:
                    return None, 0.0    # choppy AND no fresh cross → stand down
            # EXHAUSTION GUARD: the EMA cross often confirms a direction only AFTER
            # price is already stretched far past VWAP (e.g. 'put' confirms ~2σ BELOW
            # VWAP = the bottom). Buying that = entering an exhausted move that then
            # chops/reverts. Skip if price is already extended >= ma_max_vwap_z in the
            # trade's own direction (don't buy puts at the lows / calls at the highs).
            z = self._vwap_z(sym)
            if z is not None and self.ma_max_vwap_z > 0:
                if regime == "put" and z <= -self.ma_max_vwap_z:
                    return None, 0.0    # already stretched below VWAP — don't chase the bottom
                if regime == "call" and z >= self.ma_max_vwap_z:
                    return None, 0.0    # already stretched above VWAP — don't chase the top
            # L2 ORDER-FLOW GATE: don't buy into a move the book isn't backing. Two rules:
            #  (a) don't enter if the book strongly OPPOSES the direction (a wall against us)
            #  (b) don't enter an EXTENDED run (EMAs widely separated = late in the move)
            #      unless the book is meaningfully SUPPORTING it — a weak/flat book on a
            #      stretched move = buyers exhausted → likely to reverse (user's rule).
            if self.ma_book_gate_enabled:
                imb = self._book_imbalance(sym)
                aligned = None
                if imb is not None:
                    if self.ma_book_invert:
                        imb = -imb          # flip if data proves our book sign is inverted
                    aligned = imb if regime == "call" else -imb  # >0 = book supports the trade
                    if aligned <= -self.ma_book_oppose:
                        return None, 0.0    # book strongly opposes → skip
                    sep = ema.spread_pct() or 0.0
                    extended = sep >= self.ma_extended_sep_pct
                    if extended and aligned < self.ma_book_support_min:
                        return None, 0.0    # extended run + weak book → exhaustion risk, skip
                # POSITIVE CONFIRMATION (raise win rate): an EMA cross alone is marginal —
                # require the ORDER FLOW to back it. Enter only if the BOOK supports the
                # direction (>= ma_book_confirm_min) OR a WALL confirms it (support below
                # for a call / resistance above for a put). No confirmation from either →
                # skip the marginal entry that would otherwise instant-fail (fast_exit).
                if self.ma_require_confirm:
                    book_ok = aligned is not None and aligned >= self.ma_book_confirm_min
                    wc = self._wall_context(sym)
                    wall_ok = ("support" in wc) if regime == "call" else ("resistance" in wc)
                    if not (book_ok or wall_ok):
                        return None, 0.0    # no flow/wall confirmation → skip marginal entry
            # LIQUIDITY-WALL GATE: don't trade INTO a nearby persisted wall — a put with
            # a big BID WALL (support) just below is likely to bounce; a call with an ASK
            # WALL (resistance) just above is likely to reject. (Support-as-call-CONFIRM
            # is surfaced in the entry snapshot; the wall's main job here is the block.)
            if self.ma_wall_gate_enabled:
                wc = self._wall_context(sym)
                if regime == "put":
                    nsp = wc.get("near_support_pct")
                    if nsp is not None and nsp <= self.ma_wall_near_pct:
                        return None, 0.0    # bid wall (support) right below → put likely bounces
                elif regime == "call":
                    nrp = wc.get("near_resistance_pct")
                    if nrp is not None and nrp <= self.ma_wall_near_pct:
                        return None, 0.0    # ask wall (resistance) right above → call likely rejects
            return regime, 1.0          # regime + gates + book + walls OK → enter
        # ── existing momentum/fade/auto/ai logic ───────────────────────────────
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
        if any(k in applied for k in ("ma_fast", "ma_slow", "ma_tf_seconds")):
            self._rebuild_emas()
        if "ma_tri_min_sep_pct" in applied:      # push the leg-separation gate to live triples
            for _t in self._ema_tri.values():
                _t.min_sep_pct = self.ma_tri_min_sep_pct
        if any(k in applied for k in ("ma_fast_ai", "ma_slow_ai", "ma_tf_seconds")):
            # rebuild + re-seed the AI strategy's own EMA pair so a length change takes
            # effect immediately (warm, chart-accurate) rather than cold-starting.
            self._ema_ai = {s: EmaPair(self.ma_fast_ai, self.ma_slow_ai, self.ma_tf_seconds)
                            for s in self.symbols}
            try:
                self._seed_emas()
            except Exception:
                pass
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
                # RETRY seeding until it succeeds — at startup the feed client may not
                # be connected yet, leaving the AI blind ('no OHLC history'). Keep
                # trying each tick until history warms the EMAs/candles.
                if not self._seeded:
                    try:
                        if await asyncio.to_thread(self._seed_emas):
                            self._seeded = True
                            logger.info("scalper EMAs/candles seeded (retry succeeded)")
                    except Exception:
                        pass
                self._maybe_roll_day()
                self._ingest_prices()
                self._expire_pending()   # drop stale unconfirmed signals
                # UI data refreshed off the sync status() path (I/O-bound):
                await self._refresh_ladder()       # ATM±2 strikes (cached ~6s)
                if self._open:
                    await self._position_view()    # live P&L per open position
                else:
                    self._position_cache = []
                if self.enabled:
                    # ALWAYS manage open positions — even when halted (kill switch /
                    # profit lock). Halting must stop NEW ENTRIES, never abandon an
                    # open position to sit unmanaged.
                    await self._manage_open_scalps()
                    if not self._kill_tripped:
                        await self._maybe_run_supervisor()
                        if not self._ai_paused() and not self._profit_lock_tripped:
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
            self._kill_baseline = 0.0
            self._kill_tripped = False
            self._profit_lock_tripped = False
            self._exits_today = self._wins_today = self._entries_today = 0
            self._anchor_sum = {}; self._anchor_n = {}; self._anchor_sq = {}
            self._recent_exits.clear()

    def _check_kill_and_lock(self):
        # loss measured FROM the baseline (0 normally; rebased on a manual reset so a
        # cleared kill doesn't instantly re-trip on the existing drawdown)
        loss_from_baseline = self._daily_realized_pnl - self._kill_baseline
        if loss_from_baseline <= -self.daily_loss_kill_usd and not self._kill_tripped:
            self._kill_tripped = True
            logger.warning("KILL SWITCH tripped at $%.0f (from baseline $%.0f)",
                           self._daily_realized_pnl, self._kill_baseline)
        if self._daily_realized_pnl > self._daily_peak_pnl:
            self._daily_peak_pnl = self._daily_realized_pnl
        if (self._daily_peak_pnl >= self.profit_lock_arm_usd
                and self._daily_realized_pnl <= self._daily_peak_pnl * (1 - self.profit_giveback_frac)
                and not self._profit_lock_tripped):
            self._profit_lock_tripped = True
            logger.warning("PROFIT LOCK tripped — peak $%.0f now $%.0f", self._daily_peak_pnl, self._daily_realized_pnl)

    async def _do_exit(self, scalp, mid, pnl_pct, reason) -> None:
        """Route an exit through the confirmation gate. RISK exits (stop, time-stop,
        peak-lock, trail, kill, profit-lock) ALWAYS close immediately, even when
        autonomous is off — capital/gains protection is never gated. Discretionary
        SIGNAL exits (cross_back, fast_exit, profit_target) are parked for user
        confirmation when autonomous is off; otherwise they close immediately."""
        if self.autonomous or reason in _RISK_EXIT_REASONS:
            await self._close_scalp(scalp, mid, pnl_pct, reason)
            return
        # user-confirm mode + a discretionary signal exit → park it
        self._park_pending({
            "kind": "exit", "scalp_key": scalp.key, "occ": scalp.occ,
            "symbol": scalp.underlying, "direction": scalp.direction,
            "qty": scalp.qty, "reason": reason,
            "pnl_pct": round(pnl_pct * 100, 1) if pnl_pct is not None else None,
        })

    # ── exits (port) ───────────────────────────────────────────────────────
    async def _manage_open_scalps(self):
        if not self._open:
            return
        now = datetime.utcnow()
        for key in list(self._open.keys()):
            scalp = self._open.get(key)
            if scalp is None:
                continue
            snap = await self._option_quote(scalp.occ)   # true OCC (key is 'strategy:occ')
            mid = snap.get("mid_price") or snap.get("bid")
            held = (now - scalp.opened_at).total_seconds()
            er = self._efficiency_ratio(scalp.underlying)
            trending = er is not None and er >= ind.TREND_ER_THRESHOLD
            eff_stop = self.stop_pct * (ind.TREND_STOP_MULT if trending else 1.0)
            eff_time = self.time_stop_seconds * (ind.TREND_TIME_MULT if trending else 1.0)
            reason = None
            pnl_pct = 0.0
            # ── MACROSS (9/21/50 triple) exit:
            #   PRIMARY : the opposite 9×21 cross (the trend's fast/mid flip).
            #   STOP    : price closes back through EMA 50 against the position — the
            #             50 is the trend anchor, so losing it means the 9/21/50 trend
            #             structure that justified the entry is broken. This is the
            #             operator-specified stop (EMA 50, not a fixed %).
            #   BACKSTOP: option-% catastrophe floor (last resort).
            if scalp.strategy == "macross3" and mid and scalp.entry_mid > 0:
                mid_f = float(mid)
                pnl_pct = (mid_f - scalp.entry_mid) / scalp.entry_mid
                if mid_f > scalp.peak_mid:
                    scalp.peak_mid = mid_f
                tri = self._ema_tri.get(scalp.underlying)
                u_px = self._latest_price(scalp.underlying)
                ema50 = tri.ema50 if tri else None
                # EMA-50 STOP — armed only AFTER price first clears the 50 on the winning
                # side (call: above / put: below), so a lagging entry sitting near the 50
                # isn't instantly stopped. Once armed, a break BACK through the 50 exits.
                ema50_stop_hit = False
                if ema50 is not None and u_px:
                    on_winning_side = (u_px > ema50) if scalp.direction == "call" else (u_px < ema50)
                    if on_winning_side:
                        scalp.ema50_stop_armed = True
                    elif scalp.ema50_stop_armed:
                        ema50_stop_hit = True   # cleared the 50, now broke back through it
                peak_pct = (scalp.peak_mid - scalp.entry_mid) / scalp.entry_mid
                # PEAK-GIVEBACK LOCK (V-reversal fix) — checked FIRST so a sharp reversal
                # locks the run before the lagging 9×21 cross confirms.
                # FAILED-REVERSAL early cut (same operator model as macross_x): a
                # reversal entry that never started working in the grace window is a
                # dead signal — cut it small instead of riding to the EMA-50 break.
                armed3 = peak_pct >= self.ma_peak_lock_arm_pct
                progress3 = 0.0
                if scalp.entry_underlying and u_px:
                    progress3 = ((u_px - scalp.entry_underlying) / scalp.entry_underlying if scalp.direction == "call"
                                 else (scalp.entry_underlying - u_px) / scalp.entry_underlying) * 100.0
                failed3 = (self.ma_failed_reversal_enabled and not armed3
                           and held >= self.ma_failed_reversal_grace_s
                           and pnl_pct <= self.ma_failed_reversal_min_pnl_pct
                           and ((not self.ma_failed_reversal_needs_progress) or progress3 <= 0))
                if (peak_pct >= self.ma_peak_lock_arm_pct
                        and pnl_pct <= peak_pct * (1 - self.ma_peak_giveback_frac)):
                    reason = "peak_lock"
                elif failed3:
                    reason = "failed_reversal"       # never started working in the grace window
                elif tri is not None and tri.exit_signal(scalp.direction):
                    reason = "cross_back"          # opposite 9×21 cross — the primary exit
                elif ema50_stop_hit:
                    reason = "stop"                 # price broke back through EMA 50 (trend anchor)
                elif pnl_pct <= -self.ma_stop_pct_backstop:
                    reason = "stop"                 # option-% catastrophe backstop
                if reason:
                    await self._do_exit(scalp, mid_f, pnl_pct, reason)
                continue
            if scalp.strategy in ("macross_x", "ai", "apex") and mid and scalp.entry_mid > 0:
                # High-frequency MA exit: TIGHT target, but let runners ride a trail
                # that TIGHTENS as the move stretches from VWAP (adaptive give-back),
                # and HARD-EXIT the instant the EMAs cross back against the position.
                mid_f = float(mid)
                pnl_pct = (mid_f - scalp.entry_mid) / scalp.entry_mid
                if mid_f > scalp.peak_mid:
                    scalp.peak_mid = mid_f
                peak_pct = (scalp.peak_mid - scalp.entry_mid) / scalp.entry_mid
                armed = peak_pct >= self.ma_trail_activate_pct  # became a runner
                # the AI strategy rides its OWN EMA pair for cross-back/fast-exit
                ema = (self._ema_ai if scalp.strategy == "ai" else self._ema).get(scalp.underlying)
                regime = ema.spread_dir() if ema else None
                # RIDE TO THE CROSS-BACK: cross_back is the primary runner exit. The
                # trail is a LOOSE backstop (flat, wide give-back) so ordinary
                # pullbacks within a run don't shake us out before the EMAs flip.
                give = self.ma_trail_giveback_pct
                if scalp.tight_trail:
                    give = min(give, self.ma_trail_floor_pct)  # big adverse candle → lock gains
                # Win/loss asymmetry: STOP is tight & FIXED (small losers); winners ride
                # the cross-back/trail to capture the whole leg (big winners) — so avg
                # win structurally exceeds avg loss. profit_target only fires on a
                # STALLER that never armed the trail, and it sits ABOVE the stop.
                # CATCHING RUNS is the whole edge: while the EMA regime still agrees
                # with the position, we let it ride — the ONLY things that close a
                # runner are (a) the regime flipping (cross_back) or (b) it giving
                # back a chunk of its peak (trail). We do NOT time-stop or tighten a
                # trade that's still trending in our favor.
                # FAST EXIT ("slow in, fast out"): the 8/21 cross confirms a reversal
                # only AFTER the fast EMA drags back through the slow — a few minutes
                # late, giving back the top. Price crosses the FAST EMA first, so exit
                # the moment underlying closes back through the fast EMA AGAINST the
                # position. Keeps the clean 8/21 entry but captures the reversal earlier.
                u_px = self._latest_price(scalp.underlying)
                fast_ema = ema.fast if ema else None
                slow_ema = ema.slow if ema else None
                # RIDE THE TREND: once ARMED (a runner), a pullback to the FAST EMA is a
                # normal bull-flag, NOT an exit — so a runner exits on the SLOW EMA break
                # (trend structure) instead. Only UNARMED trades use the tight fast-EMA
                # exit (quick reversal protection). This stops churning a clean trend into
                # scalps (the "it should have ridden it 15 min" problem).
                exit_ema = slow_ema if (armed and slow_ema) else fast_ema
                fast_exit = False
                if (self.ma_fast_exit_enabled and exit_ema and u_px and held >= self.ma_fast_exit_min_hold_s):
                    buf = self.ma_fast_exit_buffer_pct / 100.0 * exit_ema
                    if scalp.direction == "call" and u_px < exit_ema - buf:
                        fast_exit = True
                    elif scalp.direction == "put" and u_px > exit_ema + buf:
                        fast_exit = True
                # REVERSAL-CANDLE HARD STOP: a W/M flip carries a stop at the reversal
                # candle's extreme (call: below the low; put: above the high). Breach = out.
                hard_stop_hit = False
                if scalp.hard_stop_underlying and u_px:
                    if scalp.direction == "call" and u_px < scalp.hard_stop_underlying:
                        hard_stop_hit = True
                    elif scalp.direction == "put" and u_px > scalp.hard_stop_underlying:
                        hard_stop_hit = True
                # RIDE THE TREND (the whole point): while the REGIME still AGREES with the
                # position, the underlying trend is intact → HOLD and ride it. A runner
                # exits ONLY on underlying-structure signals — cross_back (regime flip) or
                # slow-EMA break (fast_exit). The option-P&L TRAIL is SUPPRESSED here
                # because option price is noisy (theta/gamma) and gives back 25% of peak on
                # a tiny underlying bounce — which was shaking us out of correct trades
                # right before the big move (QQQ tanked 4pts AFTER a +2% trail exit).
                regime_agrees = (regime == scalp.direction)
                # UNDERLYING-BASED STOP: stop on the UNDERLYING moving against us, not the
                # gamma-amplified option %. A small counter-bounce swings the option 12%+
                # (premature stop) while the underlying barely moved and the trend's intact
                # — that whipsawed right-direction puts as QQQ kept dropping 725→718. Stop
                # only if the underlying moves against by >= ma_underlying_stop_pct.
                u_stop_hit = False
                underlying_progress = 0.0   # % the underlying has moved IN OUR FAVOR vs entry
                if scalp.entry_underlying and u_px:
                    adverse = ((scalp.entry_underlying - u_px) / scalp.entry_underlying if scalp.direction == "call"
                               else (u_px - scalp.entry_underlying) / scalp.entry_underlying) * 100.0
                    underlying_progress = -adverse   # positive = moved our way
                    if adverse >= self.ma_underlying_stop_pct:
                        u_stop_hit = True
                # FAILED-REVERSAL EARLY EXIT: at the grace deadline, if the trade never
                # started working (option P&L still worse than tolerance AND — if required —
                # the underlying hasn't progressed in our favor), the direction-change the
                # signal predicted didn't happen → cut it small. Only for trades that never
                # became runners (an armed runner is working by definition).
                failed_reversal = False
                if (self.ma_failed_reversal_enabled and not armed
                        and held >= self.ma_failed_reversal_grace_s
                        and pnl_pct <= self.ma_failed_reversal_min_pnl_pct):
                    if (not self.ma_failed_reversal_needs_progress) or underlying_progress <= 0:
                        failed_reversal = True
                if hard_stop_hit:
                    reason = "reversal_stop"         # broke the reversal candle → thesis dead
                elif failed_reversal:
                    reason = "failed_reversal"       # never started working in the grace window
                elif (regime is not None and regime != scalp.direction
                      and held >= self.ma_min_hold_before_crossback_s):
                    # MIN-HOLD before cross_back: a regime flip within a few seconds of
                    # entry is the EMAs FLICKERING at the crossover (fast≈slow), not a real
                    # reversal — it was exiting fresh trades in 4s. Give the position a
                    # moment to prove the move; the underlying stop caps the risk meanwhile.
                    reason = "cross_back"            # EMAs flipped (past the settle window)
                elif fast_exit:
                    reason = "fast_exit"             # broke the exit-EMA (slow-EMA for runners)
                elif armed and peak_pct >= self.ma_peak_lock_arm_pct and pnl_pct <= peak_pct * (1 - self.ma_peak_giveback_frac):
                    # PEAK-GIVEBACK LOCK (V-reversal fix): once a runner has a real peak,
                    # exit if it gives back this FRACTION of the peak — regardless of what
                    # the lagging EMA regime says. On a sharp V-reversal price rips back
                    # through entry before the 9×21 cross confirms, giving back most of the
                    # gain; this locks the bulk of the run instead of waiting for the cross.
                    reason = "peak_lock"
                elif armed and not regime_agrees and pnl_pct <= peak_pct - give:
                    reason = "trail"                 # runner gave back margin AND trend no longer clean
                elif not armed and pnl_pct >= self.ma_profit_target_pct:
                    reason = "profit_target"         # staller hit the min target (> stop)
                elif not armed and u_stop_hit:
                    reason = "stop"                  # UNDERLYING moved against us → real stop
                elif not armed and pnl_pct <= -self.ma_stop_pct_backstop:
                    reason = "stop"                  # wide option-% HARD backstop (catastrophe)
                # BREAKEVEN-TRAIL for small GREEN trades: a modestly-green trade that
                # hasn't armed the runner-trail should NOT be time-stopped flat — lock
                # the gain (stop at breakeven) and KEEP HOLDING so it can develop into a
                # runner. It only exits if it gives back to ~breakeven (be_trail) or via
                # the normal trail/cross-back/fast-exit.
                #
                # "RIDE WHILE VALID" (operator model): a FLAT/RED trade is NOT killed on the
                # clock. As long as the CROSS stays valid (regime still agrees — handled by
                # cross_back above) the trade rides; it only closes on a real INVALIDATION —
                # the tight UNDERLYING stop just below entry (u_stop_hit above) or the cross
                # flipping. The premium BACKSTOP (ma_stop_pct_backstop above) remains the
                # theta floor so a flat 0DTE option that quietly bleeds still has a hard cap.
                # time_stop is retained only as a FAR backstop (ma_time_stop_backstop_s) so a
                # truly dead, never-invalidated trade can't hold literally forever.
                if reason is None and not armed:
                    if peak_pct >= self.ma_breakeven_arm_pct:
                        # green enough to protect: hold with a breakeven floor
                        if pnl_pct <= self.ma_breakeven_floor_pct:
                            reason = "be_trail"      # gave back to breakeven → lock the small win
                        # else: keep holding — let it run
                    elif held >= self.ma_time_stop_backstop_s:
                        reason = "time_stop"         # far backstop only (never-invalidated dead trade)
            elif mid and scalp.entry_mid > 0:
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
            elif held >= eff_time:
                reason = "time_stop"
            if reason:
                await self._do_exit(scalp, float(mid) if mid else None, pnl_pct, reason)

    async def _close_scalp(self, scalp, exit_mid, pnl_pct, reason):
        await asyncio.to_thread(self.alpaca.sell_to_close, scalp.occ, scalp.qty,
                                (exit_mid * 0.98) if exit_mid else None)
        pnl_usd = (pnl_pct * scalp.entry_premium) if exit_mid else 0.0
        self._daily_realized_pnl += pnl_usd
        self._exits_today += 1
        if pnl_usd > 0:
            self._wins_today += 1
        # record direction + whether the trade was WITH the dominant slow-EMA trend, so
        # the AI supervisor can SEE patterns like "my counter-trend calls keep losing".
        _ema = (self._ema_ai if getattr(scalp, "strategy", "") == "ai" else self._ema).get(scalp.underlying)
        _bias = _ema.slow_slope(self.ma_trend_bias_bars or 10) if _ema else None
        with_trend = None if _bias is None else (scalp.direction == _bias)
        self._recent_exits.append((reason, pnl_usd, scalp.direction, with_trend))
        if reason == "reversal_stop":
            # A reversal-flip that broke its stop = the MOVE IS OVER and price is now
            # chopping/consolidating. Do NOT trade this symbol at all until a FRESH EMA
            # cross re-establishes a real trend (the lagging regime persists through the
            # range and would churn puts/calls in the consolidation — giving the day back).
            self._cross_lockout[scalp.underlying] = True
            self._reversal_flip.pop(scalp.underlying, None)
            self._watch_call.pop(scalp.underlying, None)
            self._watch_put.pop(scalp.underlying, None)
        elif reason == "stop":
            self._stop_cooldown[(scalp.strategy, scalp.underlying)] = datetime.utcnow() + timedelta(seconds=60)
        elif scalp.strategy in ("macross3", "macross_x", "ai", "apex"):
            # Light re-entry guard after a non-stop close: brief cooldown so a flat close
            # doesn't machine-gun the same exhausted move — but SHORT (5s) so a whipsaw
            # that immediately crosses back and RESUMES the move can be re-entered (the
            # 20s cooldown was blocking those winners). Keyed by (strategy, symbol).
            self._stop_cooldown[(scalp.strategy, scalp.underlying)] = datetime.utcnow() + timedelta(seconds=5)
        self._open.pop(getattr(scalp, "key", scalp.occ), None)
        held_s = (datetime.utcnow() - scalp.opened_at).total_seconds()
        exit_ctx = self._signal_snapshot(scalp.underlying)
        self._persist_trade(scalp, exit_mid, pnl_usd, pnl_pct, reason, held_s, exit_ctx)
        logger.info("EXIT %s %s pnl=$%.0f (%s) held=%.0fs", scalp.direction, scalp.occ, pnl_usd, reason, held_s)

    async def _run_strategy_entries(self, strat, now):
        """One labeled strategy's entry pass (its OWN position per (symbol, strategy)).
        Shared by 'all'-family modes and the AI-enabled-macross pass in 'ai' mode."""
        for sym in self.symbols:
            if len(self._open) >= self.max_concurrent:
                break
            # one position per (symbol, strategy) — strategies run independently.
            # also skip if an open is already IN FLIGHT for this pair (awaiting fill).
            if (strat, sym) in self._opening:
                continue
            if any(s.underlying == sym and s.strategy == strat for s in self._open.values()):
                continue
            cd = self._stop_cooldown.get((strat, sym))
            if cd and now < cd:
                continue
            age = self._quote_age(sym)
            if age is not None and age > _MAX_QUOTE_AGE_S:
                continue
            # GLOBAL CHOP SHUTOFF — the biggest lesson: these are TREND scalps and they
            # BLEED in directionless chop (every entry gets whipsaw-stopped regardless
            # of direction). Block ALL strategies on a symbol whose efficiency ratio is
            # below the floor (dead/chop tape). e.g. a -$690 day was mostly SPY at ER
            # 0.02 — this gate would have skipped it entirely.
            if self.chop_shutoff_er > 0:
                er = self._efficiency_ratio(sym)
                if er is not None and er < self.chop_shutoff_er:
                    continue
            # per-(strategy,symbol) throttle
            la = self._last_entry_at_strat.get((strat, sym))
            if la and (now - la).total_seconds() < 5:
                continue
            if strat == "apex":
                d, strn = self._apex_signal(sym)
            elif strat == "macross3":
                d, strn = self._macross_triple_signal(sym)
            elif strat == "macross_x":
                d, strn = self._cross_signal(sym)
            elif strat == "ai":
                d, strn = self._ai_signal(sym)
            else:
                d, strn = self._momentum_signal(sym, strategy=strat)
            if d is None or strn <= 0:
                continue
            self._opening.add((strat, sym))           # reserve the slot BEFORE awaiting
            try:
                await self._open_scalp(sym, d, strn, strategy=strat)
            finally:
                self._opening.discard((strat, sym))   # release once registered/failed
            self._last_entry_at_strat[(strat, sym)] = now
            if strat == "macross3":
                tri = self._ema_tri.get(sym)
                if tri is not None:
                    tri.leg_traded()   # one entry per 21×50 leg; resets on the next cross

    # ── entries (port of _scan_for_entries) ────────────────────────────────
    def _market_open_utc_min(self) -> int:
        """RTH open (09:30 ET) in minutes-since-UTC-midnight, DST-aware.
        EDT (summer) → 13:30 UTC = 810; EST (winter) → 14:30 UTC = 870."""
        try:
            from zoneinfo import ZoneInfo
            et = datetime.now(ZoneInfo("America/New_York"))
            off_h = et.utcoffset().total_seconds() / 3600.0   # -4 (EDT) or -5 (EST)
            return int((9 * 60 + 30) - off_h * 60)            # 09:30 ET → UTC minutes
        except Exception:
            return 13 * 60 + 30   # fallback: EDT

    def _market_close_utc_min(self) -> int:
        """RTH close (16:00 ET) in minutes-since-UTC-midnight, DST-aware."""
        return self._market_open_utc_min() + 6 * 60 + 30      # 6h30m after the open

    async def _scan_for_entries(self):
        now = datetime.utcnow()
        cm_now = now.hour * 60 + now.minute
        minutes_to_close = self._market_close_utc_min() - cm_now
        if 0 < minutes_to_close <= 30:
            return
        # post-open blackout: sit out the first N minutes after the RTH open (DST-aware)
        if self.open_blackout_minutes > 0:
            minutes_since_open = cm_now - self._market_open_utc_min()
            if 0 <= minutes_since_open < self.open_blackout_minutes:
                return
        if self.daily_entry_start_utc is not None or self.daily_entry_end_utc is not None:
            cm = now.hour * 60 + now.minute
            if (self.daily_entry_start_utc is not None and cm < self.daily_entry_start_utc) or \
               (self.daily_entry_end_utc is not None and cm >= self.daily_entry_end_utc):
                return
        committed = sum(s.entry_premium for s in self._open.values())
        if committed >= self.budget_usd:
            return

        # ── AI META-CONTROL (DISABLED): the 9/21/50 macross was retired for poor
        # performance, so the AI no longer deploys it alongside itself. (Kept as a
        # no-op guard in case macross3 is ever revived.)
        if False and self.signal_mode == "ai" and self._ai_enable_macross:
            await self._run_strategy_entries("macross3", now)

        # ── MULTI-STRATEGY scalp modes (macross family) ────────────────────────
        # macross → state-based (full gates); macross_x → cross+L2 only; all → both.
        # Each strategy holds its OWN position per symbol (one per (symbol, strategy)),
        # each labeled, so their P&L can be compared. They share the exit logic.
        active = self._active_scalp_strategies()
        if active:
            for strat in active:
                await self._run_strategy_entries(strat, now)
            return

        # ── legacy momentum/fade/auto/ai path (single position per symbol) ─────
        open_dirs = {s.direction for s in self._open.values()}
        locked = open_dirs.pop() if len(open_dirs) == 1 else None
        min_interval = self._min_entry_interval_seconds
        if self._last_entry_at and (now - self._last_entry_at).total_seconds() < min_interval:
            return

        for sym in self.symbols:
            if len(self._open) >= self.max_concurrent:
                break
            if any(s.underlying == sym for s in self._open.values()):
                continue
            cd = self._stop_cooldown.get(sym)
            if cd and now < cd:
                continue
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
            # MA-crossover (high-frequency) mode: the cross IS the trigger — bypass
            # the momentum-mode filters (chop/velocity/regime/2-tick) that exist to
            # SUPPRESS trades. Only the direction-lock + sizing + rails still apply.
            if self.signal_mode == "macross":
                if locked and momentum_dir != locked:
                    continue
                await self._open_scalp(sym, momentum_dir, 1.0)
                continue
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

    async def _open_scalp(self, sym, direction, score, strategy="macross3"):
        underlying_px = self._latest_price(sym)
        if not underlying_px:
            return
        # pass spot so the chain is CENTERED on the money (else limit=200 returns low
        # strikes only → picks a deep-ITM contract → qty rounds to 0 → no trade)
        contracts = await asyncio.to_thread(self.alpaca.find_contracts, sym, direction,
                                            self.max_dte, underlying_px)
        if not contracts:
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

        # USER-CONFIRM GATE: when autonomous is off, park the resolved entry as a
        # pending signal (with the real strike/price/qty) for the user to confirm,
        # instead of firing. The entry stops/sizing stay fixed by config — only
        # the go/no-go decision is handed to the user. Autonomous → execute now.
        if not self.autonomous:
            self._park_pending({
                "kind": "entry", "strategy": strategy, "symbol": sym,
                "direction": direction, "occ": occ,
                "strike": contract.get("strike"), "qty": qty, "price": round(ask, 2),
                "score": round(score, 3) if score is not None else None,
                "reason": "entry_signal",
            })
            return

        await self._execute_entry(sym, direction, occ, ask, qty, score, strategy,
                                  contract.get("strike"))

    async def _execute_entry(self, sym, direction, occ, ask, qty, score, strategy,
                             strike=None):
        """Place the entry order + register the open scalp. Shared by the
        autonomous path and the user-confirm path."""
        underlying_px = self._latest_price(sym)
        order = await asyncio.to_thread(self.alpaca.buy_to_open, occ, qty, ask)
        if not order:
            return
        entry = await asyncio.to_thread(self.alpaca.fill_price, order.get("order_id"), ask)
        scalp = _OpenScalp(occ, sym, direction, qty, float(entry), order.get("order_id"), strategy=strategy)
        scalp.entry_underlying = underlying_px   # anchor the underlying-based stop
        scalp.entry_ctx = self._signal_snapshot(sym, score)  # full forensic context at entry
        # if this entry came from a W/M reversal flip, attach the candle-based hard stop
        flip = self._reversal_flip.pop(sym, None)
        if flip is not None and flip[0] == direction and flip[1]:
            scalp.hard_stop_underlying = flip[1]
            logger.info("REVERSAL FLIP %s %s hard-stop@underlying %.2f", direction, sym, flip[1])
        self._open[scalp.key] = scalp
        self._entries_today += 1
        self._last_entry_at = datetime.utcnow()
        logger.info("ENTRY [%s] %s %s x%d @ %.2f", strategy, direction, occ, qty, entry)

    # ── user-confirmation machinery ─────────────────────────────────────────
    def _park_pending(self, sig: dict) -> str:
        """Register a pending signal awaiting user confirmation. Coalesces on
        (kind, occ/symbol) so a repeated tick doesn't stack duplicates; sets an
        expiry after which the loop drops it (a stale scalp signal must not fill
        late). Returns the signal id."""
        now = datetime.utcnow()
        # de-dup: replace an existing same-target pending rather than pile up.
        dedup_key = (sig.get("kind"), sig.get("occ") or sig.get("symbol"))
        for sid, ex in list(self._pending.items()):
            if (ex.get("kind"), ex.get("occ") or ex.get("symbol")) == dedup_key:
                del self._pending[sid]
        self._pending_seq += 1
        sid = f"sig{self._pending_seq}"
        sig = dict(sig)
        sig["id"] = sid
        sig["created_ts"] = int(now.timestamp())
        sig["expires_ts"] = int(now.timestamp()) + self.confirm_window_s
        self._pending[sid] = sig
        logger.info("PENDING %s %s %s (awaiting confirm, %ss)",
                    sig.get("kind"), sig.get("direction") or "", sig.get("occ") or sig.get("symbol"),
                    self.confirm_window_s)
        return sid

    def _expire_pending(self) -> None:
        """Drop pending signals past their window. Called each tick."""
        nowt = int(datetime.utcnow().timestamp())
        for sid, ex in list(self._pending.items()):
            if nowt >= ex.get("expires_ts", 0):
                logger.info("PENDING %s expired (unconfirmed)", sid)
                del self._pending[sid]

    def pending_signals(self) -> list:
        """Live pending signals for the UI (soonest-expiring first)."""
        nowt = int(datetime.utcnow().timestamp())
        out = []
        for ex in self._pending.values():
            e = dict(ex)
            e["seconds_left"] = max(0, e.get("expires_ts", nowt) - nowt)
            out.append(e)
        return sorted(out, key=lambda e: e["seconds_left"])

    async def confirm_signal(self, signal_id: str, action: str = "confirm") -> dict:
        """User confirms/skips a pending signal. 'confirm' executes it now;
        anything else drops it. Returns {ok, executed, reason}."""
        sig = self._pending.pop(signal_id, None)
        if sig is None:
            return {"ok": False, "reason": "not_found_or_expired"}
        if action != "confirm":
            return {"ok": True, "executed": False, "reason": "skipped"}
        if int(datetime.utcnow().timestamp()) >= sig.get("expires_ts", 0):
            return {"ok": False, "reason": "expired"}
        try:
            if sig["kind"] == "entry":
                await self._execute_entry(
                    sig["symbol"], sig["direction"], sig["occ"], sig["price"],
                    sig["qty"], sig.get("score"), sig.get("strategy", "macross3"),
                    sig.get("strike"))
                return {"ok": True, "executed": True, "kind": "entry"}
            if sig["kind"] == "exit":
                scalp = self._open.get(sig["scalp_key"])
                if scalp is None:
                    return {"ok": False, "reason": "position_gone"}
                snap = await self._option_quote(scalp.occ)
                mid = snap.get("mid_price") or snap.get("bid")
                pnl_pct = ((float(mid) - scalp.entry_mid) / scalp.entry_mid) if (mid and scalp.entry_mid) else 0.0
                await self._close_scalp(scalp, float(mid) if mid else None, pnl_pct,
                                        sig.get("reason", "user_exit"))
                return {"ok": True, "executed": True, "kind": "exit"}
        except Exception as e:
            logger.warning("confirm_signal %s failed: %s", signal_id, e)
            return {"ok": False, "reason": str(e)}
        return {"ok": False, "reason": "unknown_kind"}

    async def manual_close(self, scalp_key: str) -> dict:
        """User closes an open position immediately (the Close Now button)."""
        scalp = self._open.get(scalp_key)
        if scalp is None:
            # tolerate passing a bare OCC
            scalp = next((s for s in self._open.values() if s.occ == scalp_key), None)
        if scalp is None:
            return {"ok": False, "reason": "not_open"}
        snap = await self._option_quote(scalp.occ)
        mid = snap.get("mid_price") or snap.get("bid")
        pnl_pct = ((float(mid) - scalp.entry_mid) / scalp.entry_mid) if (mid and scalp.entry_mid) else 0.0
        await self._close_scalp(scalp, float(mid) if mid else None, pnl_pct, "manual")
        return {"ok": True, "executed": True}

    def set_autonomous(self, on: bool) -> None:
        self.autonomous = bool(on)
        if self.autonomous:
            self._pending.clear()   # switching to auto clears any parked prompts
        logger.info("Scalper autonomous=%s", self.autonomous)

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
            "walls": self._wall_context(sym),   # L2 support/resistance at entry (forensics)
            "entry_threshold": self.entry_threshold,
            "stop_pct": self.stop_pct,
            "profit_target_pct": self.profit_target_pct,
        }

    # ── supervisor ─────────────────────────────────────────────────────────
    async def _maybe_run_supervisor(self):
        # macross is a PURELY MECHANICAL engine — the AI is OFF (no calls, no feed,
        # no veto). The supervisor runs for the dedicated 'ai' mode AND for 'all' mode
        # (where 'ai' is one of the tracked strategies and needs its mode + EMA tune).
        if not self._anthropic_key or (self.signal_mode != "ai" and self.signal_mode != "all"):
            return
        now = datetime.utcnow()
        # (ai mode only) fast mode-check + periodic deep param-tune.
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
            smp = self._regime_samples.get(s)
            # Multi-resolution ER so the AI can SEE the timescale mismatch: if ER
            # is low at a fine window but high at a coarse one, it's a slow grind
            # → the AI should coarsen regime_sample_seconds / widen er_window.
            er_fast = ind.efficiency_ratio(smp, 8)    # ~recent, fine
            er_slow = ind.efficiency_ratio(smp, 32)   # broader
            cs = self._candles.get(s)
            ohlc = cs.recent_ohlc(20) if cs is not None else []
            tape[s] = {
                "last": round(self._latest_price(s), 2) if self._latest_price(s) else None,
                "session_vwap_ish": round(self._anchored_mean(s), 2) if self._anchored_mean(s) else None,
                "efficiency_ratio": round(self._efficiency_ratio(s), 3) if self._efficiency_ratio(s) is not None else None,
                "er_fast_8": round(er_fast, 3) if er_fast is not None else None,
                "er_slow_32": round(er_slow, 3) if er_slow is not None else None,
                "recent_range_pct": round(self._recent_range_pct(s), 3) if self._recent_range_pct(s) is not None else None,
                "book_imbalance": round(self._book_imbalance(s), 2) if self._book_imbalance(s) is not None else None,
                "ohlc_1m": ohlc,   # last 20 closed 1-min bars [o,h,l,c] — the actual price action
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
            all_mode=(self.signal_mode == "all"),
        )
        data = await sup.run_supervisor_call(self._anthropic_key, prompt, deep)
        if not data or str(data.get("mode", "")).lower().strip() not in ("momentum", "fade", "pause"):
            # Transient LLM JSON blips are normal — only surface an error after
            # several CONSECUTIVE failures, and keep the last good mode running.
            self._ai_fail_streak = getattr(self, "_ai_fail_streak", 0) + 1
            if self._ai_fail_streak >= 3:
                self._ai_error = f"AI unparseable ×{self._ai_fail_streak} (using last mode: {self._ai_mode})"
            return
        mode = str(data.get("mode", "")).lower().strip()
        self._ai_fail_streak = 0
        self._ai_mode = mode
        self._ai_reason = str(data.get("reason", ""))[:120]
        self._ai_levels = data.get("levels", {}) or {}
        self._ai_assessment = str(data.get("assessment", ""))[:600]
        self._ai_error = None
        # AI meta-control: does the supervisor want the mechanical macross running
        # alongside it? (only honored in 'ai' mode). Clean-trend tape → on; chop → off.
        if "enable_macross" in data:
            new_flag = bool(data.get("enable_macross"))
            if new_flag != self._ai_enable_macross:
                logger.info("AI %s macross alongside (tape %s)",
                            "ENABLED" if new_flag else "disabled", self._ai_reason)
            self._ai_enable_macross = new_flag
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
        for col, decl in (("pnl_pct","REAL"),("held_s","REAL"),("opened_ts","INTEGER"),("entry_ctx","TEXT"),("exit_ctx","TEXT"),("strategy","TEXT")):
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
                "INSERT INTO scalp_trades (ts,occ,underlying,direction,qty,entry,exit,pnl,pnl_pct,reason,held_s,opened_ts,entry_ctx,exit_ctx,strategy) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (int(time.time() * 1000), scalp.occ, scalp.underlying, scalp.direction, scalp.qty,
                 scalp.entry_mid, exit_mid, pnl, round(pnl_pct, 4), reason, round(held_s, 1),
                 scalp.opened_epoch_ms,
                 json.dumps(scalp.entry_ctx), json.dumps(exit_ctx), getattr(scalp, "strategy", "macross3")))
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

    def _entry_state(self) -> Tuple[str, str]:
        """A single clear read of whether NEW entries can fire right now, and why.
        Returns (state, reason) where state ∈ active|stopped|paused|outside_window|
        blackout|kill|profit_lock|killbudget."""
        if not self.enabled:
            return "stopped", "engine off"
        if self._kill_tripped:
            return "kill", "daily loss kill switch tripped"
        if self._profit_lock_tripped:
            return "profit_lock", "profit lock tripped"
        if self._ai_paused():
            return "paused", "AI paused (dangerous tape)"
        now = datetime.utcnow()
        cm = now.hour * 60 + now.minute
        minutes_to_close = self._market_close_utc_min() - cm
        if 0 < minutes_to_close <= 30:
            return "blackout", "within 30 min of close"
        if self.open_blackout_minutes > 0:
            mso = cm - self._market_open_utc_min()   # minutes since the RTH open (DST-aware)
            if 0 <= mso < self.open_blackout_minutes:
                return "blackout", f"first {self.open_blackout_minutes} min after open"
        if self.daily_entry_start_utc is not None and cm < self.daily_entry_start_utc:
            return "outside_window", "before entry window"
        if self.daily_entry_end_utc is not None and cm >= self.daily_entry_end_utc:
            return "outside_window", "after entry window"
        if sum(s.entry_premium for s in self._open.values()) >= self.budget_usd:
            return "stopped", "budget fully committed"
        return "active", "entries active"

    # ── status for the UI ──────────────────────────────────────────────────
    def status(self) -> Dict[str, Any]:
        entry_state, entry_reason = self._entry_state()
        return {
            "enabled": self.enabled, "running": self._running, "signal_mode": self.signal_mode,
            "autonomous": self.autonomous,
            "confirm_window_s": self.confirm_window_s,
            "pending_signals": self.pending_signals(),
            "entry_state": entry_state, "entry_reason": entry_reason,
            "live_trading": self.live_trading,
            "live_available": self._alpaca_live is not None,
            "ai_mode": self._ai_mode, "ai_reason": self._ai_reason, "ai_assessment": self._ai_assessment,
            "ai_enable_macross": self._ai_enable_macross,
            "ai_error": self._ai_error, "ai_log": list(self._ai_log)[:20],
            "active_strategy_spec": self._active_strategy_spec,
            "kill_switch_tripped": self._kill_tripped, "profit_lock_tripped": self._profit_lock_tripped,
            "day_pnl": round(self._daily_realized_pnl, 2), "peak_pnl": round(self._daily_peak_pnl, 2),
            "entries_today": self._entries_today, "exits_today": self._exits_today, "wins_today": self._wins_today,
            "win_rate": round(self._wins_today / self._exits_today, 2) if self._exits_today else None,
            "open_scalps": len(self._open),
            # Enriched, loop-refreshed live positions (P&L $/%, peak, stop) — the
            # UI's active performance indicator. Falls back to a bare view if the
            # cache hasn't populated yet (first tick after an entry).
            "open_positions": self._position_cache or [
                {
                    "occ": sc.occ, "scalp_key": sc.key, "underlying": sc.underlying,
                    "direction": sc.direction, "qty": sc.qty, "entry_mid": round(sc.entry_mid, 2),
                    "strategy": getattr(sc, "strategy", None),
                    "held_s": round((datetime.utcnow() - sc.opened_at).total_seconds(), 0),
                } for sc in self._open.values()
            ],
            "ladder": self._ladder,   # ATM±2 strikes the engine is eyeing per symbol
            "symbols": self.symbols,
            "per_trade_usd": self.per_trade_usd,
            "max_concurrent": self.max_concurrent,
            "daily_loss_kill_usd": self.daily_loss_kill_usd,
            "budget_usd": self.budget_usd,
            "open_blackout_minutes": self.open_blackout_minutes,
            "daily_entry_start_utc": self.daily_entry_start_utc,
            "daily_entry_end_utc": self.daily_entry_end_utc,
            "regime_sample_seconds": self.regime_sample_seconds,
            "er_window": self.er_window,
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
                    "walls": self._wall_context(s),   # L2 support/resistance (bid/ask walls)
                    "ema_fast": round(e.fast, 2) if (e := self._ema.get(s)) and e.fast is not None else None,
                    "ema_slow": round(e.slow, 2) if (e := self._ema.get(s)) and e.slow is not None else None,
                    "ema_regime": e.spread_dir() if (e := self._ema.get(s)) else None,
                    "quote_age_s": round(a, 1) if (a := self._quote_age(s)) is not None else None,
                } for s in self.symbols
            },
            "ma_fast": self.ma_fast, "ma_slow": self.ma_slow, "ma_tf_seconds": self.ma_tf_seconds,
            "ma_fast_ai": self.ma_fast_ai, "ma_slow_ai": self.ma_slow_ai,
            "active_strategies": self._active_scalp_strategies(),
            "ma_profit_target_pct": self.ma_profit_target_pct,
            "ma_stop_pct": self.ma_stop_pct,
            "ma_trail_giveback_pct": self.ma_trail_giveback_pct,
            "ma_trail_activate_pct": self.ma_trail_activate_pct,
            "ma_max_vwap_z": self.ma_max_vwap_z,
            "ma_min_separation_pct": self.ma_min_separation_pct,
            "ma_fast_exit_enabled": self.ma_fast_exit_enabled,
            "ma_fast_exit_min_hold_s": self.ma_fast_exit_min_hold_s,
            "ma_min_er": self.ma_min_er,
        }
