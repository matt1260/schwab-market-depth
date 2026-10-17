# api.py
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime
import sqlite3
import os
import json
import traceback
from pydantic import BaseModel
from typing import List, Optional
import threading
import time
import multiprocessing
from stream import main as stream_main

app = FastAPI(title="Market Depth API")

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Store current symbols being streamed
active_symbols = set()
# Create a lock for thread-safe operations on active_symbols
symbols_lock = threading.Lock()

class PriceLevel(BaseModel):
    price: float
    quantity: int
    side: str

class DepthResponse(BaseModel):
    symbol: str
    timestamp: int
    levels: List[PriceLevel]
    last_price: Optional[float] = None
    last_size: Optional[float] = None
    underlying_price: Optional[float] = None

class SymbolRequest(BaseModel):
    symbol: str

# Define base and data directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
os.makedirs(DATA_DIR, exist_ok=True)

def get_db_connection():
    """Get a connection to today's database or the most recent one."""
    try:
        data_dir = DATA_DIR
        # Data directory already ensured at import

        today_date = datetime.now().strftime('%y%m%d')
        #today_date = 250423  # For testing purposes, set a fixed date
        db_filename = os.path.join(data_dir, f'options_data_{today_date}.db')
        
        # Check if today's DB exists
        if os.path.exists(db_filename):
            print(f"Using today's database: {db_filename}")
            return sqlite3.connect(db_filename)
        
        # If not, find the most recent DB file
        print(f"Today's database not found, searching for most recent in {data_dir}")
        if not os.path.exists(data_dir):
            raise FileNotFoundError(f"Data directory {data_dir} does not exist")
            
        db_files = [f for f in os.listdir(data_dir) if f.startswith('options_data_') and f.endswith('.db')]
        if not db_files:
            raise FileNotFoundError(f"No database files found in {data_dir}")
        
        # Sort by date (files are named options_data_YYMMDD.db)
        db_files.sort(reverse=True)
        db_filename = os.path.join(data_dir, db_files[0])
        print(f"Using most recent database: {db_filename}")
        return sqlite3.connect(db_filename)
    except Exception as e:
        print(f"Error connecting to database: {str(e)}")
        print(traceback.format_exc())
        raise

# ── Standalone scalper sandbox (Schwab L2 data + Alpaca paper exec + Opus AI) ──
import os as _os
_scalper = {"engine": None, "feed": None}
_spreads = {"engine": None}
_scanner = {"scanner": None}
_zdte = {"scanner": None}
SCALPER_SYMBOLS = ["SPY", "QQQ"]


@app.on_event("startup")
async def _start_scalper():
    """Build the equity feed + scalper engine and start them in the API event loop.
    The engine starts DISABLED (no orders) — toggle on from the UI when ready."""
    try:
        from scalper.schwab_feed import SchwabEquityFeed
        from scalper.alpaca_exec import AlpacaExec
        from scalper.engine import ScalperEngine
        feed = SchwabEquityFeed(SCALPER_SYMBOLS)
        alpaca = AlpacaExec(_os.getenv("ALPACA_API_KEY"), _os.getenv("ALPACA_API_SECRET"),
                            paper=str(_os.getenv("ALPACA_PAPER", "true")).lower() != "false")
        # Optional LIVE (real-money) Alpaca client — built ONLY if live keys exist.
        alpaca_live = None
        _lk, _ls = _os.getenv("ALPACA_LIVE_API_KEY"), _os.getenv("ALPACA_LIVE_API_SECRET")
        if _lk and _ls:
            try:
                alpaca_live = AlpacaExec(_lk, _ls, paper=False)
                print("LIVE Alpaca client built (real money) — available but NOT active until toggled")
            except Exception as _le:
                print(f"LIVE Alpaca client failed to build: {_le}")
        engine = ScalperEngine(SCALPER_SYMBOLS, stream_getter=lambda: feed, alpaca=alpaca,
                               anthropic_key=_os.getenv("ANTHROPIC_API_KEY"), alpaca_live=alpaca_live)
        _scalper["feed"], _scalper["engine"] = feed, engine
        await feed.start()   # opens Schwab L1+L2 stream (re-auth in browser on first run)
        await engine.start() # loop runs; enabled=False until toggled
        print("Scalper engine + Schwab equity feed started (engine disabled)")
        # premium-SELLING core: put credit spreads (separate engine, shares feed+alpaca)
        try:
            from scalper.spreads import SpreadEngine
            spread_eng = SpreadEngine(SCALPER_SYMBOLS, feed_getter=lambda: feed,
                                      alpaca=alpaca, alpaca_live=alpaca_live)
            _spreads["engine"] = spread_eng
            await spread_eng.start()  # loop runs; enabled=False until toggled
            print("Spread engine (put credit spreads) started (disabled)")
        except Exception as _se:
            import traceback as _tb
            print(f"Spread engine startup failed (scalper still runs): {_se}")
            _tb.print_exc()
        # premium-selling SCANNER: rank the universe for the richest spread opportunities
        try:
            from scalper.scanner import PremiumScanner
            scanner = PremiumScanner(alpaca, schwab_client_getter=lambda: getattr(feed, "_client", None))
            _scanner["scanner"] = scanner
            await scanner.start(interval_s=600)
            # let the spread engine trade the scanner's ranked universe
            if _spreads["engine"] is not None:
                _spreads["engine"].set_scanner(scanner)
            print("Premium scanner started (universe ranking every 10m); wired to spread engine")
        except Exception as _sce:
            import traceback as _tb
            print(f"Scanner startup failed: {_sce}")
            _tb.print_exc()

        # 0DTE credit-spread scanner — ranked by probability of expiring OTM (scanner-only)
        try:
            from scalper.zdte_scanner import ZdteScanner
            _zdte["scanner"] = ZdteScanner(alpaca, schwab_client_getter=lambda: getattr(feed, "_client", None))
            print("0DTE P(OTM) spread scanner ready (on-demand)")
        except Exception as _ze:
            print(f"0DTE scanner init failed: {_ze}")
    except Exception as e:
        import traceback
        print(f"Scalper startup failed (UI still runs): {e}")
        traceback.print_exc()


@app.get("/scalper/status")
async def scalper_status():
    eng = _scalper["engine"]
    if eng is None:
        return {"available": False}
    st = eng.status()
    st["available"] = True
    st["feed_running"] = bool(_scalper["feed"] and _scalper["feed"].running)
    return st


@app.post("/scalper/toggle")
async def scalper_toggle(body: dict):
    eng = _scalper["engine"]
    if eng is None:
        raise HTTPException(503, "Scalper not initialized")
    eng.set_enabled(bool(body.get("enabled")))
    return {"enabled": eng.enabled}


@app.post("/scalper/manual")
async def scalper_manual(body: dict):
    """MANUAL strategy tracker: user clicks Buy Call / Buy Put → open a scalp labeled
    'manual' on the given symbol, managed by the shared exit ladder (peak-lock/stop).
    Body: {symbol: 'SPY'|'QQQ', direction: 'call'|'put'}."""
    eng = _scalper["engine"]
    if eng is None:
        raise HTTPException(503, "Scalper not initialized")
    sym = (body.get("symbol") or "").upper()
    direction = body.get("direction")
    if direction not in ("call", "put"):
        raise HTTPException(400, "direction must be 'call' or 'put'")
    res = await eng.manual_entry(sym, direction)
    return res


@app.post("/scalper/autonomous")
async def scalper_autonomous(body: dict):
    """Toggle autonomous vs user-confirmed trading. When off, entry signals and
    discretionary exits are parked for confirmation (risk stops still auto-fire)."""
    eng = _scalper["engine"]
    if eng is None:
        raise HTTPException(503, "Scalper not initialized")
    eng.set_autonomous(bool(body.get("autonomous")))
    return {"autonomous": eng.autonomous}


@app.post("/scalper/confirm")
async def scalper_confirm(body: dict):
    """Confirm or skip a pending signal. body: {signal_id, action: confirm|skip}."""
    eng = _scalper["engine"]
    if eng is None:
        raise HTTPException(503, "Scalper not initialized")
    sid = body.get("signal_id")
    if not sid:
        raise HTTPException(400, "signal_id required")
    return await eng.confirm_signal(sid, body.get("action", "confirm"))


@app.post("/scalper/close")
async def scalper_close(body: dict):
    """Manually close an open position now. body: {scalp_key} (or bare OCC)."""
    eng = _scalper["engine"]
    if eng is None:
        raise HTTPException(503, "Scalper not initialized")
    key = body.get("scalp_key") or body.get("occ")
    if not key:
        raise HTTPException(400, "scalp_key required")
    return await eng.manual_close(key)


@app.post("/scalper/live")
async def scalper_live(body: dict):
    """Switch execution between PAPER and LIVE (real money). Guarded: refuses LIVE
    unless live Alpaca keys were configured. Live never auto-persists across
    restarts (defaults back to paper). Returns the resulting mode + live account
    equity (read-only) so the UI can confirm the real account is reachable."""
    eng = _scalper["engine"]
    if eng is None:
        raise HTTPException(503, "Scalper not initialized")
    want_live = bool(body.get("live"))
    res = eng.set_live_trading(want_live)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "Could not switch to live"))
    # On switching live, report the LIVE account equity as a reachability check.
    if eng.live_trading:
        try:
            res["live_equity"] = await __import__("asyncio").to_thread(eng.alpaca.equity)
        except Exception as e:
            res["live_equity_error"] = str(e)
    return res


@app.get("/scalper/live/check")
async def scalper_live_check():
    """Read-only readiness check for live trading WITHOUT switching: are live keys
    present, and is the live account reachable + funded? Never places an order."""
    eng = _scalper["engine"]
    if eng is None:
        raise HTTPException(503, "Scalper not initialized")
    out = {"live_keys_present": eng._alpaca_live is not None, "live_trading": eng.live_trading}
    if eng._alpaca_live is not None:
        health = await __import__("asyncio").to_thread(eng._alpaca_live.account_health)
        out["account"] = health
        out["ready"] = bool(health.get("ok"))  # keys present AND account active/funded/options-approved
    else:
        out["ready"] = False
    return out


@app.post("/scalper/reset-kill")
async def scalper_reset_kill():
    """Manually clear the kill switch / profit lock (paper testing). Re-bases the
    daily-loss baseline so it doesn't instantly re-trip on the existing drawdown."""
    eng = _scalper["engine"]
    if eng is None:
        raise HTTPException(503, "Scalper not initialized")
    return eng.reset_kill()


@app.post("/scalper/config")
async def scalper_config(body: dict):
    """Set signal_mode and/or any tunable knob from the UI."""
    eng = _scalper["engine"]
    if eng is None:
        raise HTTPException(503, "Scalper not initialized")
    if "signal_mode" in body and body["signal_mode"] in ("momentum", "fade", "auto", "ai", "macross", "macross_x", "all", "apex", "bullpivot", "manual"):
        eng.signal_mode = body["signal_mode"]
        if eng.signal_mode != "ai":
            eng._ai_enable_macross = False   # clear the AI's macross-alongside flag off 'ai' mode
    # AI supervision cadence (seconds). Clamp to sane ranges. ai_supervisor_seconds
    # = fast mode-check; ai_tune_seconds = slow deep tune/strategy cycle.
    cadence = {}
    if "ai_supervisor_seconds" in body:
        eng.ai_supervisor_seconds = max(30, min(1800, int(body["ai_supervisor_seconds"])))
        cadence["ai_supervisor_seconds"] = eng.ai_supervisor_seconds
    if "ai_tune_seconds" in body:
        eng.ai_tune_seconds = max(60, min(3600, int(body["ai_tune_seconds"])))
        cadence["ai_tune_seconds"] = eng.ai_tune_seconds
    # Operator-only risk/sizing controls (NOT AI-tunable — the supervisor must
    # never change position size or the kill switch). Clamped to sane floors.
    risk = {}
    if "per_trade_usd" in body:
        eng.per_trade_usd = max(10.0, float(body["per_trade_usd"]))
        risk["per_trade_usd"] = eng.per_trade_usd
    if "max_concurrent" in body:
        eng.max_concurrent = max(1, int(body["max_concurrent"]))
        risk["max_concurrent"] = eng.max_concurrent
    if "daily_loss_kill_usd" in body:
        eng.daily_loss_kill_usd = max(10.0, float(body["daily_loss_kill_usd"]))
        risk["daily_loss_kill_usd"] = eng.daily_loss_kill_usd
    if "budget_usd" in body:
        eng.budget_usd = max(10.0, float(body["budget_usd"]))
        risk["budget_usd"] = eng.budget_usd
    # Trading window (minutes since UTC midnight). Gates entries to regular hours
    # so it can't fire into illiquid pre/post-market. null clears that side.
    if "daily_entry_start_utc" in body:
        v = body["daily_entry_start_utc"]
        eng.daily_entry_start_utc = int(v) if v is not None else None
        risk["daily_entry_start_utc"] = eng.daily_entry_start_utc
    if "daily_entry_end_utc" in body:
        v = body["daily_entry_end_utc"]
        eng.daily_entry_end_utc = int(v) if v is not None else None
        risk["daily_entry_end_utc"] = eng.daily_entry_end_utc
    # Post-open blackout: minutes to wait after the 09:30 ET open before scalping
    # (operator setting, 0-120; 0 = trade from the bell).
    if "open_blackout_minutes" in body:
        eng.open_blackout_minutes = max(0, min(120, int(body["open_blackout_minutes"])))
        risk["open_blackout_minutes"] = eng.open_blackout_minutes
    # Per-rule MANUAL-trade exit toggles (operator-only — the AI supervisor must never
    # override which exits apply to the user's discretionary trades). Each is a plain bool.
    _MANUAL_EXIT_KEYS = ("manual_exit_failed_reversal", "manual_exit_cross_back",
                         "manual_exit_fast_exit", "manual_exit_peak_lock",
                         "manual_exit_profit_target", "manual_exit_stop")
    for k in _MANUAL_EXIT_KEYS:
        if k in body:
            setattr(eng, k, bool(body[k]))
            risk[k] = bool(body[k])
    skip = ("signal_mode", "ai_supervisor_seconds", "ai_tune_seconds",
            "per_trade_usd", "max_concurrent", "daily_loss_kill_usd", "budget_usd",
            "open_blackout_minutes") + _MANUAL_EXIT_KEYS
    applied = eng._apply_runtime_params({k: v for k, v in body.items() if k not in skip})
    applied.update(cadence)
    applied.update(risk)
    eng.save_state()  # persist across restarts
    return {"signal_mode": eng.signal_mode, "applied": applied}


@app.get("/scalper/chart/{symbol}")
async def scalper_chart(symbol: str):
    """Live price + session-mean (VWAP-ish) + ±2σ band series for the chart."""
    eng = _scalper["engine"]
    if eng is None:
        return {"symbol": symbol, "series": []}
    return {"symbol": symbol, "series": eng.chart_history(symbol)}


@app.get("/scalper/candles/{symbol}")
async def scalper_candles(symbol: str):
    """1-min OHLC candles + 9/21/50 EMA overlays + 9×21 crossover markers for the
    redesigned candlestick chart. All on the closed-bar basis the engine trades."""
    eng = _scalper["engine"]
    if eng is None:
        return {"symbol": symbol, "bars": [], "emas": [], "live": None,
                "ema_now": {}, "markers": []}
    return eng.candles(symbol.upper())


@app.get("/scalper/markers/{symbol}")
async def scalper_markers(symbol: str):
    """Entry/exit markers for a symbol's underlying chart, from trade history.
    Each: {entry_t, entry_px, exit_t, exit_px, direction, pnl, reason}."""
    try:
        import json as _json
        sc = sqlite3.connect(_os.path.join(DATA_DIR, "scalper.db"))
        sc.row_factory = sqlite3.Row
        rows = sc.execute("SELECT * FROM scalp_trades WHERE underlying=? ORDER BY ts DESC LIMIT 50", (symbol,)).fetchall()
        sc.close()
        out = []
        for r in rows:
            r = dict(r)
            ectx = _json.loads(r.get("entry_ctx") or "{}") if r.get("entry_ctx") else {}
            xctx = _json.loads(r.get("exit_ctx") or "{}") if r.get("exit_ctx") else {}
            out.append({
                "entry_t": int((r.get("opened_ts") or r["ts"]) / 1000),
                "entry_px": ectx.get("underlying"),
                "exit_t": int(r["ts"] / 1000),
                "exit_px": xctx.get("underlying"),
                "direction": r.get("direction"), "pnl": r.get("pnl"), "reason": r.get("reason"),
            })
        return {"symbol": symbol, "markers": out}
    except Exception:
        return {"symbol": symbol, "markers": []}


@app.get("/scalper/trades")
async def scalper_trades(limit: int = 100):
    try:
        sc = sqlite3.connect(_os.path.join(DATA_DIR, "scalper.db"))
        sc.row_factory = sqlite3.Row
        rows = sc.execute("SELECT * FROM scalp_trades ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        sc.close()
        return {"trades": [dict(r) for r in rows]}
    except Exception:
        return {"trades": []}


# ── premium-selling core: put credit spreads ─────────────────────────────────
@app.get("/spreads/status")
async def spreads_status():
    eng = _spreads["engine"]
    if eng is None:
        return {"available": False}
    return eng.status()


@app.post("/spreads/toggle")
async def spreads_toggle(body: dict):
    eng = _spreads["engine"]
    if eng is None:
        raise HTTPException(503, "Spread engine not initialized")
    eng.set_enabled(bool(body.get("enabled")))
    return {"enabled": eng.enabled}


@app.post("/spreads/config")
async def spreads_config(body: dict):
    """Set spread strategy knobs (IVR gate, delta, width, DTE, management, sizing)."""
    eng = _spreads["engine"]
    if eng is None:
        raise HTTPException(503, "Spread engine not initialized")
    _float = {"ivr_min", "short_delta", "width", "profit_take_frac", "stop_credit_mult",
              "max_loss_per_spread_usd", "min_credit"}
    _int = {"target_dte", "dte_min", "dte_max", "dte_close", "max_concurrent",
            "daily_entry_start_utc", "daily_entry_end_utc"}
    applied = {}
    for k, v in body.items():
        if k in _float and v is not None:
            setattr(eng, k, float(v)); applied[k] = float(v)
        elif k in _int:
            setattr(eng, k, int(v) if v is not None else None); applied[k] = getattr(eng, k)
        elif k == "use_scanner":
            eng.use_scanner = bool(v); applied[k] = eng.use_scanner
    eng.save_state()
    return {"applied": applied}


@app.post("/spreads/live")
async def spreads_live(body: dict):
    eng = _spreads["engine"]
    if eng is None:
        raise HTTPException(503, "Spread engine not initialized")
    res = eng.set_live_trading(bool(body.get("live")))
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "live switch refused"))
    out = {"live_trading": eng.live_trading}
    if eng.live_trading and eng._alpaca_live is not None:
        h = await __import__("asyncio").to_thread(eng._alpaca_live.account_health)
        out["live_equity"] = h.get("equity")
    return out


@app.get("/spreads/trades")
async def spreads_trades(limit: int = 50):
    eng = _spreads["engine"]
    if eng is None:
        return {"trades": []}
    return {"trades": eng.recent_trades(limit)}


@app.get("/scanner/status")
async def scanner_status():
    sc = _scanner["scanner"]
    if sc is None:
        return {"available": False}
    out = sc.results()
    out["available"] = True
    return out


@app.post("/scanner/scan")
async def scanner_scan():
    """Force an immediate rescan (don't wait for the 10m loop)."""
    sc = _scanner["scanner"]
    if sc is None:
        raise HTTPException(503, "Scanner not initialized")
    res = await __import__("asyncio").to_thread(sc.scan_once)
    return {"scanned": len(res), "top_tradeable": [r for r in res if r.get("tradeable")][:5]}


# ── 0DTE credit-spread scanner, ranked by probability of expiring OTM ──
@app.get("/zdte/status")
async def zdte_status():
    sc = _zdte["scanner"]
    if sc is None:
        return {"available": False}
    out = sc.results()
    out["available"] = True
    return out


@app.post("/zdte/scan")
async def zdte_scan():
    """Run the 0DTE P(OTM) scan now. Returns spreads ranked by probability of profit."""
    sc = _zdte["scanner"]
    if sc is None:
        raise HTTPException(503, "0DTE scanner not initialized")
    res = await __import__("asyncio").to_thread(sc.scan_once)
    return {"scanned": len(res), "opportunities": res}


@app.post("/zdte/config")
async def zdte_config(body: dict):
    """Tune the 0DTE scanner: pop_min, width, min_credit, min_credit_pct."""
    sc = _zdte["scanner"]
    if sc is None:
        raise HTTPException(503, "0DTE scanner not initialized")
    return {"applied": sc.set_params(**body)}


@app.get("/")
async def root():
    return {"message": "Market Depth API is running"}

@app.get("/active_symbols")
async def get_active_symbols():
    """Get the list of symbols currently being streamed."""
    with symbols_lock:
        return {"symbols": list(active_symbols)}

@app.get("/symbols")
async def get_symbols():
    """Get all available symbols in the database."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT DISTINCT symbol FROM options_book_data")
        symbols = [row[0] for row in cursor.fetchall()]
        
        conn.close()
        print(f"Found {len(symbols)} symbols in the database")
        return {"symbols": symbols}
    except Exception as e:
        error_details = f"Database error: {str(e)}\n{traceback.format_exc()}"
        print(error_details)
        raise HTTPException(status_code=500, detail=error_details)

@app.get("/depth/{symbol}", response_model=DepthResponse)
async def get_depth(symbol: str, limit: int = 10):
    """Get market depth data for a specific symbol."""
    try:
        # Normalize symbol (replace %20 with space)
        symbol = symbol.replace("%20", " ").strip()
        
        # Add symbol to active streams
        with symbols_lock:
            if symbol not in active_symbols:
                active_symbols.add(symbol)
                print(f"Added symbol to stream from depth request: {symbol}")
                
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Get the latest timestamp for the symbol
        cursor.execute(
            "SELECT MAX(timestamp) as latest_ts FROM options_book_data WHERE symbol = ?", 
            (symbol,)
        )
        result = cursor.fetchone()
        
        if not result or not result['latest_ts']:
            # If no data found, we're streaming it now, but return empty result
            conn.close()
            return {
                "symbol": symbol,
                "timestamp": int(time.time() * 1000),
                "levels": [],
                "last_price": None,
                "last_size": None,
                "underlying_price": None
            }
        
        latest_timestamp = result['latest_ts']
        
        # Get the price levels for the latest timestamp
        cursor.execute(
            """
            SELECT price, quantity, side 
            FROM options_book_data 
            WHERE symbol = ? AND timestamp = ?
            ORDER BY 
                CASE WHEN side = 'ASK' THEN price ELSE -price END
            """, 
            (symbol, latest_timestamp)
        )
        
        levels = [
            {"price": row['price'], "quantity": row['quantity'], "side": row['side']} 
            for row in cursor.fetchall()
        ]
        
        # Get the latest level one data
        cursor.execute(
            """
            SELECT last_price, last_size, underlying_price, timestamp
            FROM level_one_data
            WHERE symbol = ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (symbol,)
        )
        level_one = cursor.fetchone()
        
        conn.close()
        
        return {
            "symbol": symbol,
            "timestamp": latest_timestamp,
            "levels": levels,
            "last_price": level_one['last_price'] if level_one else None,
            "last_size": level_one['last_size'] if level_one else None,
            "underlying_price": level_one['underlying_price'] if level_one else None
        }
    except sqlite3.Error as e:
        error_details = f"Database error: {str(e)}\n{traceback.format_exc()}"
        print(error_details)
        raise HTTPException(status_code=500, detail=error_details)

@app.get("/historical_full/{symbol}")
async def get_historical_full(symbol: str, limit: Optional[int] = None):
    """Get ALL historical market depth snapshots for a specific symbol without sampling."""
    try:
        # Normalize symbol (replace %20 with space)
        symbol = symbol.replace("%20", " ").strip()
        
        # Add symbol to active streams
        with symbols_lock:
            if symbol not in active_symbols:
                active_symbols.add(symbol)
                print(f"Added symbol to stream from historical_full request: {symbol}")
                
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Get all snapshot timestamps for this symbol
        cursor.execute(
            """
            SELECT DISTINCT timestamp 
            FROM options_book_data 
            WHERE symbol = ?
            ORDER BY timestamp
            """,
            (symbol,)
        )
        
        all_timestamps = [row['timestamp'] for row in cursor.fetchall()]
        
        if not all_timestamps:
            # Return empty result if no data found
            return {"symbol": symbol, "snapshots": []}
        
        # Apply limit if specified
        if limit and len(all_timestamps) > limit:
            # Take evenly distributed samples if we need to limit
            step = len(all_timestamps) // limit
            sampled_timestamps = all_timestamps[::step]
            # Always include the latest timestamp
            if all_timestamps[-1] not in sampled_timestamps:
                sampled_timestamps.append(all_timestamps[-1])
        else:
            sampled_timestamps = all_timestamps
            
        print(f"Found {len(all_timestamps)} timestamps for {symbol}, using {len(sampled_timestamps)} samples")
        
        # Fetch data for each timestamp
        result = []
        for ts in sampled_timestamps:
            cursor.execute(
                """
                SELECT price, quantity, side 
                FROM options_book_data 
                WHERE symbol = ? AND timestamp = ?
                """,
                (symbol, ts)
            )
            
            levels = [
                {"price": row['price'], "quantity": row['quantity'], "side": row['side']} 
                for row in cursor.fetchall()
            ]
            
            if not levels:
                # Skip timestamps with no levels data
                continue
            
            # Get the nearest level one data
            cursor.execute(
                """
                SELECT last_price, last_size, underlying_price
                FROM level_one_data
                WHERE symbol = ? AND timestamp <= ?
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                (symbol, ts)
            )
            level_one = cursor.fetchone()
            
            snapshot = {
                "timestamp": ts,
                "levels": levels,
                "last_price": level_one['last_price'] if level_one else None,
                "last_size": level_one['last_size'] if level_one else None,
                "underlying_price": level_one['underlying_price'] if level_one else None
            }
            
            result.append(snapshot)
        
        conn.close()
        return {"symbol": symbol, "snapshots": result}
    
    except sqlite3.Error as e:
        error_details = f"Database error: {str(e)}\n{traceback.format_exc()}"
        print(error_details)
        raise HTTPException(status_code=500, detail=error_details)

if __name__ == "__main__":
    import uvicorn

    # Schwab allows only ONE stream per login. The scalper sandbox opens its own
    # equity L1+L2 stream in-process (startup event), so the options-depth stream
    # subprocess must NOT also run by default or they'll conflict. Set
    # RUN_OPTIONS_STREAM=1 to run the original options bookmap stream instead.
    stream_process = None
    if str(_os.getenv("RUN_OPTIONS_STREAM", "")).lower() in ("1", "true", "yes"):
        stream_process = multiprocessing.Process(target=stream_main)
        stream_process.start()
        print("Options-depth stream process started")
    else:
        print("Options-depth stream disabled (scalper equity feed runs in-process). "
              "Set RUN_OPTIONS_STREAM=1 to enable the options bookmap stream.")

    try:
        uvicorn.run(app, host="0.0.0.0", port=8080)
    finally:
        if stream_process is not None:
            stream_process.terminate()
            stream_process.join()
            print("Stream process stopped")
