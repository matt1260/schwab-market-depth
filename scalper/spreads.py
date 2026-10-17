"""
Premium-SELLING core: systematic put credit spreads (the long-term-edge engine).

Why this exists: buying 0DTE (the macross scalper) fights theta + spread + IV. The
documented durable edge is SELLING premium when it's rich (Volatility Risk Premium).
This engine sells defined-risk put credit spreads — mandatory defined risk given the
small account (no naked/undefined risk).

Mechanics (all operator/AI-free, purely rules-based):
  GATE   — only sell when IV Rank (VIX/VXN proxy) is high (premium is expensive).
  SELECT — one expiry ~target_dte (30-45 DTE); SHORT put at ~short_delta; LONG put
           `width` dollars below (same expiry) for defined risk.
  SIZE   — qty so max loss = (width - credit)*100*qty stays under a small % of equity.
  MANAGE — close at 50% of max credit captured, or at 21 DTE, or at a 2x-credit stop.

Slow loop (minutes, not seconds) — these are 30-45 DTE positions, not scalps.
Separate P&L + SQLite table from the macross scalper so the two shapes are tracked
independently. PAPER first; live is a guarded, separate toggle (defaults paper).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, date, timezone
from typing import Any, Dict, List, Optional

from .iv_rank import IVRank

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "spreads.db")


@dataclass
class OpenSpread:
    underlying: str
    short_occ: str
    long_occ: str
    short_strike: float
    long_strike: float
    expiration: str          # YYYY-MM-DD
    qty: int
    entry_credit: float      # per-spread credit received (e.g. 1.30 = $130)
    width: float             # strike width in dollars
    opened_at: datetime
    order_id: Optional[str] = None
    peak_capture: float = 0.0  # best fraction of credit captured so far
    side: str = "put"          # 'put' (uptrend) or 'call' (downtrend)


class SpreadEngine:
    # AI-tunable / operator knobs persisted across restarts
    _STATE_KEYS = ("enabled", "ivr_min", "target_dte", "dte_min", "dte_max",
                   "short_delta", "width", "profit_take_frac", "dte_close",
                   "stop_credit_mult", "max_loss_per_spread_usd", "max_concurrent",
                   "min_credit", "entry_concession", "use_scanner",
                   "daily_entry_start_utc", "daily_entry_end_utc")

    def __init__(self, symbols, feed_getter, alpaca, alpaca_live=None):
        self.symbols = symbols
        self._feed_getter = feed_getter      # () -> SchwabEquityFeed (has ._client for IVR)
        self._alpaca_paper = alpaca
        self._alpaca_live = alpaca_live
        self.live_trading = False            # NOT persisted — reverts to paper on restart

        # strategy params (defaults)
        self.enabled = False
        self.ivr_min = 30.0                  # only sell when IV Rank >= this (rich premium)
        self.target_dte = 35
        self.dte_min = 30
        self.dte_max = 45
        self.short_delta = 0.30              # short put ~30-delta — classic credit-spread
                                             # strike; collects ~1/5 of width, RR ~3-4:1,
                                             # ~70-75% OTM prob (manage-at-50% lifts realized)
        self.width = 5.0                     # strike width $ (defined risk)
        self.profit_take_frac = 0.50         # close at 50% of max credit captured
        self.dte_close = 21                  # close/avoid gamma under 21 DTE
        self.stop_credit_mult = 2.0          # stop when debit-to-close >= 2x entry credit
        self.max_loss_per_spread_usd = 400.0 # cap risk/position. NOTE: a 5-wide index
                                             # spread risks ~$380-435 = ~30% of a $1.3k
                                             # acct. Aggressive but DEFINED; it's the floor
                                             # to trade SPY/QQQ spreads in a small account.
        self.max_concurrent = 1              # one spread at a time given the account size
        self.min_credit = 0.20               # skip spreads paying < this (not worth fees/slip)
        self.entry_concession = 0.05         # route this far through mid so the limit fills
        self.daily_entry_start_utc: Optional[int] = None  # spreads can enter any RTH time
        self.daily_entry_end_utc: Optional[int] = None

        self._ivr = IVRank()
        self._scanner = None                      # optional PremiumScanner (set by api)
        self.use_scanner = True                   # trade scanner's ranked universe vs fixed list
        self._open: Dict[str, OpenSpread] = {}    # key = short_occ
        self._day = date.today()
        self._realized_pnl = 0.0
        self._wins = 0
        self._closed = 0
        self._last_scan: Dict[str, float] = {}
        self._last_ivr: Dict[str, Dict] = {}
        self._note = ""                            # last human-readable status note
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self.tick_seconds = 60                     # slow loop

        self._init_db()
        self._load_state()

    # ── execution client ────────────────────────────────────────────────────
    @property
    def alpaca(self):
        return self._alpaca_live if (self.live_trading and self._alpaca_live) else self._alpaca_paper

    def _client(self):
        feed = self._feed_getter()
        return getattr(feed, "_client", None) if feed else None

    def _leg_quote(self, occ: str) -> Dict[str, Optional[float]]:
        """{bid, ask, mid} for one leg off SCHWAB real-time (project rule: Schwab
        prices, Alpaca executes), falling back to Alpaca."""
        feed = self._feed_getter()
        if feed is not None and hasattr(feed, "option_quote"):
            q = feed.option_quote(occ)
            if q and (q.get("mid_price") or q.get("bid")):
                return {"bid": q.get("bid"), "ask": q.get("ask"), "mid": q.get("mid_price")}
        q = self.alpaca.option_quote(occ)
        if q:
            return {"bid": q.get("bid"), "ask": q.get("ask"), "mid": q.get("mid_price")}
        return {"bid": None, "ask": None, "mid": None}

    def _leg_mid(self, occ: str) -> Optional[float]:
        m = self._leg_quote(occ).get("mid")
        return float(m) if m else None

    # ── persistence ──────────────────────────────────────────────────────────
    def _init_db(self):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        c = sqlite3.connect(DB_PATH)
        c.execute("""CREATE TABLE IF NOT EXISTS spread_trades (
            ts INTEGER, underlying TEXT, short_occ TEXT, long_occ TEXT,
            short_strike REAL, long_strike REAL, expiration TEXT, qty INTEGER,
            entry_credit REAL, exit_debit REAL, width REAL, pnl REAL, reason TEXT,
            held_s REAL, opened_ts INTEGER)""")
        c.commit(); c.close()

    def _state_path(self):
        return os.path.join(os.path.dirname(DB_PATH), "spreads_state.json")

    def save_state(self):
        try:
            with open(self._state_path(), "w") as f:
                json.dump({k: getattr(self, k) for k in self._STATE_KEYS}, f)
        except Exception as e:
            logger.debug("spreads save_state failed: %s", e)

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
        except Exception as e:
            logger.debug("spreads load_state failed: %s", e)

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def start(self):
        if self._running:
            return
        self._running = True
        try:
            await asyncio.to_thread(self._rehydrate_open)
        except Exception as e:
            logger.warning("spread rehydrate failed: %s", e)
        self._task = asyncio.create_task(self._loop(), name="spread-engine")

    def _rehydrate_open(self):
        """Rebuild open spreads from the broker's actual option positions so a
        restart doesn't ORPHAN a live spread (the engine resumes managing it).
        Pairs a short put (qty<0) with a long put (qty>0) of the SAME underlying
        + expiry; entry_credit = short_avg - long_avg."""
        poss = self.alpaca.get_option_positions()
        if not poss:
            return
        import re
        # group legs by (root, expiry, type) — a credit spread is two legs same type
        by_grp: Dict[tuple, List[Dict]] = {}
        for p in poss:
            if not p["expiration"]:
                continue
            m = re.match(r"^([A-Z]+)\d", p["occ"])
            root = m.group(1) if m else "?"
            by_grp.setdefault((root, p["expiration"], p["type"]), []).append(p)
        recovered = 0
        for (root, exp, typ), legs in by_grp.items():
            shorts = [l for l in legs if l["qty"] < 0]
            longs = [l for l in legs if l["qty"] > 0]
            if not shorts or not longs:
                continue
            if typ == "call":   # call credit: short LOWER strike, long HIGHER
                short = min(shorts, key=lambda l: l["strike"])
                longp = max(longs, key=lambda l: l["strike"])
            else:               # put credit: short HIGHER strike, long LOWER
                short = max(shorts, key=lambda l: l["strike"])
                longp = min(longs, key=lambda l: l["strike"])
            qty = min(abs(short["qty"]), longp["qty"])
            credit = round(short["avg_entry_price"] - longp["avg_entry_price"], 2)
            width = abs(short["strike"] - longp["strike"])
            if width <= 0 or qty < 1:
                continue
            self._open[short["occ"]] = OpenSpread(
                underlying=root, short_occ=short["occ"], long_occ=longp["occ"],
                short_strike=short["strike"], long_strike=longp["strike"],
                expiration=exp, qty=qty, entry_credit=credit, width=width,
                opened_at=datetime.now(timezone.utc), side=typ,
            )
            recovered += 1
        if recovered:
            self._note = f"rehydrated {recovered} open spread(s) from broker positions"
            logger.info("SPREAD %s", self._note)

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    def set_enabled(self, on: bool):
        self.enabled = bool(on)
        self.save_state()

    def set_scanner(self, scanner):
        self._scanner = scanner

    def set_live_trading(self, on: bool) -> Dict[str, Any]:
        if on and self._alpaca_live is None:
            return {"ok": False, "error": "no live keys configured"}
        if on:
            h = self._alpaca_live.account_health()
            if not h.get("ok"):
                return {"ok": False, "error": f"live account not ready: {h}"}
        self.live_trading = bool(on)
        return {"ok": True, "live_trading": self.live_trading}

    # ── main loop ──────────────────────────────────────────────────────────
    async def _loop(self):
        while self._running:
            try:
                self._roll_day()
                await self._refresh_ivr()   # always — keeps the UI gate live even when idle
                if self.enabled:
                    await self._manage_open()
                    if self._in_window():
                        await self._scan_for_entries()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("spread loop tick failed: %s", e)
            try:
                await asyncio.sleep(self.tick_seconds)
            except asyncio.CancelledError:
                break

    def _roll_day(self):
        today = date.today()
        if today != self._day:
            self._day = today
            self._realized_pnl = 0.0
            self._wins = self._closed = 0

    def _in_window(self) -> bool:
        now = datetime.now(timezone.utc)
        cm = now.hour * 60 + now.minute
        # RTH-ish guard: 13:30-20:00 UTC unless operator narrowed it
        start = self.daily_entry_start_utc if self.daily_entry_start_utc is not None else 810
        end = self.daily_entry_end_utc if self.daily_entry_end_utc is not None else 1200
        return start <= cm < end

    # ── selection ────────────────────────────────────────────────────────────
    def _select_spread(self, sym: str, side: str = "put") -> Optional[Dict[str, Any]]:
        """Pick ONE expiry near target_dte, then a same-expiry credit spread on `side`:
        PUT = short ~delta put + long `width` BELOW; CALL = short ~delta call + long
        `width` ABOVE. Credit priced off Schwab. Returns the spread dict or None."""
        chain = (self.alpaca.get_call_chain if side == "call" else self.alpaca.get_put_chain)(
            sym, self.dte_min, self.dte_max)
        if not chain:
            return None
        # group by expiry, choose the expiry whose DTE is closest to target
        by_exp: Dict[str, List[Dict]] = {}
        for c in chain:
            if c.get("expiration"):
                by_exp.setdefault(c["expiration"], []).append(c)
        if not by_exp:
            return None
        exp = min(by_exp, key=lambda e: abs((by_exp[e][0].get("dte") or 999) - self.target_dte))
        legs = sorted(by_exp[exp], key=lambda c: c["strike"])
        # SHORT leg: closest to short_delta
        short = min(legs, key=lambda c: abs(abs(c["delta"]) - self.short_delta))
        # LONG leg: same expiry, ~`width` away on the protected side
        if side == "call":
            wing = [c for c in legs if c["strike"] > short["strike"]]   # long ABOVE
            target_long = short["strike"] + self.width
        else:
            wing = [c for c in legs if c["strike"] < short["strike"]]   # long BELOW
            target_long = short["strike"] - self.width
        if not wing:
            return None
        longp = min(wing, key=lambda c: abs(c["strike"] - target_long))
        width = abs(short["strike"] - longp["strike"])
        if width <= 0:
            return None
        # PRICE THE CREDIT OFF SCHWAB (Alpaca chain mids are too coarse for the
        # adjacent-strike difference). Selection used Alpaca delta; credit uses Schwab.
        short_mid = self._leg_mid(short["occ"]) or short.get("mid")
        long_mid = self._leg_mid(longp["occ"]) or longp.get("mid")
        if short_mid is None or long_mid is None:
            return None
        credit = round(short_mid - long_mid, 2)
        if credit < self.min_credit:
            return None
        max_loss_per = (width - credit) * 100.0
        if max_loss_per <= 0:
            return None
        qty = int(self.max_loss_per_spread_usd // max_loss_per)
        if qty < 1:
            return None
        return {
            "underlying": sym, "side": side, "expiration": exp, "short": short, "long": longp,
            "width": width, "credit": credit, "qty": qty,
            "max_loss": max_loss_per * qty, "max_win": credit * 100.0 * qty,
            "short_delta": short["delta"],
        }

    async def _refresh_ivr(self):
        """Keep IV Rank current for every symbol (cached 1hr in IVRank) so the UI
        gate is always live, even when the engine is idle/disabled/after-hours."""
        client = self._client()
        if client is None:
            return
        for sym in self.symbols:
            ivr = await asyncio.to_thread(self._ivr.get, sym, client)
            if ivr:
                self._last_ivr[sym] = ivr

    def _entry_candidates(self) -> List[tuple]:
        """(symbol, ivr) candidates to try, best first. If the SCANNER is wired and
        enabled, use its already-gated top-ranked opportunities across the whole
        universe (IV-vs-RV edge + earnings + liquidity). Else fall back to the fixed
        SPY/QQQ list with the VIX-proxy IVR gate."""
        if self.use_scanner and self._scanner is not None:
            try:
                top = self._scanner.results().get("top_tradeable", [])
                # (symbol, ivr, side) — side from the scanner's trend filter
                return [(r["symbol"], r.get("ivr") or 0, r.get("side") or "put") for r in top]
            except Exception:
                pass
        out = []
        for sym in self.symbols:
            rank = (self._last_ivr.get(sym) or {}).get("rank")
            if rank is not None and rank >= self.ivr_min:
                out.append((sym, rank, "put"))  # fixed-list fallback = puts only
        return out

    async def _scan_for_entries(self):
        if len(self._open) >= self.max_concurrent:
            return
        for sym, rank, side in self._entry_candidates():
            if len(self._open) >= self.max_concurrent:
                break
            if any(s.underlying == sym for s in self._open.values()):
                continue  # one spread per underlying at a time
            if time.time() - self._last_scan.get(sym, 0) < 30:  # throttle heavy chain calls
                continue
            self._last_scan[sym] = time.time()
            sel = await asyncio.to_thread(self._select_spread, sym, side)
            if sel is None:
                self._note = f"{sym}: {side} IVR {rank:.0f} OK but no spread met filters"
                continue
            await self._open_spread(sel, rank)

    async def _open_spread(self, sel: Dict[str, Any], rank: float):
        short, longp = sel["short"], sel["long"]
        # Submit at the NATURAL credit (short_bid - long_ask) so the limit is
        # marketable and actually fills — resting at mid often never fills. Natural is
        # the realistic, conservative (lowest) credit you'd get crossing the spread.
        sq = self._leg_quote(short["occ"]); lq = self._leg_quote(longp["occ"])
        natural = None
        if sq.get("bid") and lq.get("ask"):
            natural = round(float(sq["bid"]) - float(lq["ask"]), 2)
        submit_credit = natural if (natural and natural >= self.min_credit) else \
            max(self.min_credit, round(sel["credit"] - self.entry_concession, 2))
        side = sel.get("side", "put")
        opener = self.alpaca.open_call_credit_spread if side == "call" else self.alpaca.open_put_credit_spread
        res = await asyncio.to_thread(opener, short["occ"], longp["occ"], sel["qty"], submit_credit)
        if not res:
            self._note = f"{sel['underlying']}: {side} spread order rejected"
            return
        oid = res.get("order_id")
        # CONFIRM THE FILL before marking the spread open — don't track a phantom
        # position. Poll briefly; if it doesn't fill, cancel and move on (retry next scan).
        filled = False
        for _ in range(8):  # ~12s
            await asyncio.sleep(1.5)
            stt = await asyncio.to_thread(self.alpaca.order_status, oid)
            s = (stt.get("status") or "").lower()
            if "filled" in s and "partial" not in s:
                filled = True
                break
            if any(x in s for x in ("canceled", "cancelled", "rejected", "expired")):
                break
        if not filled:
            await asyncio.to_thread(self.alpaca.cancel_order, oid)
            self._note = (f"{sel['underlying']}: spread did not fill @ ${submit_credit:.2f} "
                          f"(IVR {rank:.0f}) — canceled, will retry")
            logger.info("SPREAD %s", self._note)
            return
        sp = OpenSpread(
            underlying=sel["underlying"], short_occ=short["occ"], long_occ=longp["occ"],
            short_strike=short["strike"], long_strike=longp["strike"],
            expiration=sel["expiration"], qty=sel["qty"], entry_credit=submit_credit,
            width=sel["width"], opened_at=datetime.now(timezone.utc), order_id=oid, side=side,
        )
        self._open[short["occ"]] = sp
        tag = "c" if side == "call" else "p"
        self._note = (f"OPEN {sel['underlying']} {short['strike']:.0f}/{longp['strike']:.0f}{tag} "
                      f"x{sel['qty']} credit ${submit_credit:.2f} FILLED (IVR {rank:.0f})")
        logger.info("SPREAD %s", self._note)

    # ── management ────────────────────────────────────────────────────────────
    def _spread_debit_to_close(self, sp: OpenSpread) -> Optional[float]:
        """Current cost to close = short_mid - long_mid (what we'd pay back). Priced
        off Schwab real-time, same as entry."""
        sm = self._leg_mid(sp.short_occ)
        lm = self._leg_mid(sp.long_occ)
        if sm is None:
            return None
        return round(sm - (lm or 0), 2)

    async def _manage_open(self):
        for key in list(self._open.keys()):
            sp = self._open.get(key)
            if sp is None:
                continue
            debit = await asyncio.to_thread(self._spread_debit_to_close, sp)
            dte = (date.fromisoformat(sp.expiration) - date.today()).days if sp.expiration else None
            reason = None
            if debit is not None and sp.entry_credit > 0:
                capture = (sp.entry_credit - debit) / sp.entry_credit  # frac of credit captured
                sp.peak_capture = max(sp.peak_capture, capture)
                if capture >= self.profit_take_frac:
                    reason = "profit_50"
                elif debit >= self.stop_credit_mult * sp.entry_credit:
                    reason = "stop_2x"
            if reason is None and dte is not None and dte <= self.dte_close:
                reason = "dte_21"
            if reason:
                await self._close_spread(sp, debit, reason)

    async def _close_spread(self, sp: OpenSpread, debit: Optional[float], reason: str):
        # pay up a touch on the debit to ensure the close fills
        close_limit = (debit if debit is not None else sp.entry_credit) + 0.05
        closer = self.alpaca.close_call_credit_spread if sp.side == "call" else self.alpaca.close_put_credit_spread
        res = await asyncio.to_thread(closer, sp.short_occ, sp.long_occ, sp.qty, close_limit)
        if not res:
            self._note = f"{sp.underlying}: CLOSE rejected ({reason})"
            logger.warning(self._note)
            return
        # realized P&L = (credit received - debit paid) * 100 * qty
        pnl = round((sp.entry_credit - (debit if debit is not None else 0)) * 100.0 * sp.qty, 2)
        self._realized_pnl += pnl
        self._closed += 1
        if pnl > 0:
            self._wins += 1
        held_s = (datetime.now(timezone.utc) - sp.opened_at).total_seconds()
        self._persist(sp, debit, pnl, reason, held_s)
        self._open.pop(sp.short_occ, None)
        self._note = (f"CLOSE {sp.underlying} {sp.short_strike:.0f}/{sp.long_strike:.0f}p "
                      f"pnl ${pnl:.0f} ({reason})")
        logger.info("SPREAD %s", self._note)

    def _persist(self, sp: OpenSpread, debit, pnl, reason, held_s):
        try:
            c = sqlite3.connect(DB_PATH)
            c.execute("INSERT INTO spread_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (int(time.time() * 1000), sp.underlying, sp.short_occ, sp.long_occ,
                       sp.short_strike, sp.long_strike, sp.expiration, sp.qty,
                       sp.entry_credit, debit, sp.width, pnl, reason, held_s,
                       int(sp.opened_at.timestamp() * 1000)))
            c.commit(); c.close()
        except Exception as e:
            logger.debug("spread persist failed: %s", e)

    def recent_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        try:
            c = sqlite3.connect(DB_PATH)
            rows = c.execute(
                "SELECT ts,underlying,short_occ,long_occ,short_strike,long_strike,expiration,"
                "qty,entry_credit,exit_debit,width,pnl,reason,held_s FROM spread_trades "
                "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
            c.close()
            keys = ["ts", "underlying", "short_occ", "long_occ", "short_strike", "long_strike",
                    "expiration", "qty", "entry_credit", "exit_debit", "width", "pnl", "reason", "held_s"]
            return [dict(zip(keys, r)) for r in rows]
        except Exception:
            return []

    # ── status for the UI ────────────────────────────────────────────────────
    def status(self) -> Dict[str, Any]:
        return {
            "available": True,
            "enabled": self.enabled, "running": self._running,
            "live_trading": self.live_trading, "live_available": self._alpaca_live is not None,
            "in_window": self._in_window(), "note": self._note,
            "use_scanner": self.use_scanner, "scanner_wired": self._scanner is not None,
            "day_pnl": round(self._realized_pnl, 2),
            "closed_today": self._closed, "wins_today": self._wins,
            "win_rate": round(self._wins / self._closed, 2) if self._closed else None,
            "open_count": len(self._open),
            "ivr": {s: self._last_ivr.get(s) for s in self.symbols},
            "open_spreads": [
                {"underlying": s.underlying, "side": s.side, "short_strike": s.short_strike,
                 "long_strike": s.long_strike, "expiration": s.expiration, "qty": s.qty,
                 "entry_credit": s.entry_credit, "width": s.width,
                 "dte": (date.fromisoformat(s.expiration) - date.today()).days if s.expiration else None,
                 "peak_capture": round(s.peak_capture, 2),
                 "held_s": round((datetime.now(timezone.utc) - s.opened_at).total_seconds(), 0)}
                for s in self._open.values()
            ],
            "params": {k: getattr(self, k) for k in self._STATE_KEYS},
        }
