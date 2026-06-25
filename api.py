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
        engine = ScalperEngine(SCALPER_SYMBOLS, stream_getter=lambda: feed, alpaca=alpaca,
                               anthropic_key=_os.getenv("ANTHROPIC_API_KEY"))
        _scalper["feed"], _scalper["engine"] = feed, engine
        await feed.start()   # opens Schwab L1+L2 stream (re-auth in browser on first run)
        await engine.start() # loop runs; enabled=False until toggled
        print("Scalper engine + Schwab equity feed started (engine disabled)")
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


@app.post("/scalper/config")
async def scalper_config(body: dict):
    """Set signal_mode and/or any tunable knob from the UI."""
    eng = _scalper["engine"]
    if eng is None:
        raise HTTPException(503, "Scalper not initialized")
    if "signal_mode" in body and body["signal_mode"] in ("momentum", "fade", "auto", "ai"):
        eng.signal_mode = body["signal_mode"]
    # AI supervision cadence (seconds). Clamp to sane ranges. ai_supervisor_seconds
    # = fast mode-check; ai_tune_seconds = slow deep tune/strategy cycle.
    cadence = {}
    if "ai_supervisor_seconds" in body:
        eng.ai_supervisor_seconds = max(30, min(1800, int(body["ai_supervisor_seconds"])))
        cadence["ai_supervisor_seconds"] = eng.ai_supervisor_seconds
    if "ai_tune_seconds" in body:
        eng.ai_tune_seconds = max(60, min(3600, int(body["ai_tune_seconds"])))
        cadence["ai_tune_seconds"] = eng.ai_tune_seconds
    skip = ("signal_mode", "ai_supervisor_seconds", "ai_tune_seconds")
    applied = eng._apply_runtime_params({k: v for k, v in body.items() if k not in skip})
    applied.update(cadence)
    return {"signal_mode": eng.signal_mode, "applied": applied}


@app.get("/scalper/chart/{symbol}")
async def scalper_chart(symbol: str):
    """Live price + session-mean (VWAP-ish) + ±2σ band series for the chart."""
    eng = _scalper["engine"]
    if eng is None:
        return {"symbol": symbol, "series": []}
    return {"symbol": symbol, "series": eng.chart_history(symbol)}


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
