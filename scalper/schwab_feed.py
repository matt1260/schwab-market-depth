"""
Schwab equity L1+L2 feed for the standalone scalper engine.

The app's existing stream.py streams OPTIONS depth; the scalper signal layer needs
the UNDERLYING equity NBBO (price series) + book imbalance (NASDAQ_BOOK). This
adapter opens its own StreamAsync subscription for LEVELONE_EQUITIES + NASDAQ_BOOK
and keeps an in-memory per-symbol snapshot the engine polls (latest_quote /
book_imbalance) — same surface the platform's SchwabDataProvider exposes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _install_certifi_ws_ssl() -> None:
    """Force websockets.connect to verify against certifi's clean CA bundle.

    On machines with an SSL-intercepting proxy/AV, the system trust store carries
    a self-signed root that breaks verification of streamer-api.schwab.com, while
    certifi's bundle verifies it fine (issuer DigiCert). schwabdev calls
    websockets.connect() with no ssl= arg, so we wrap it to inject a certifi
    context by default. This KEEPS verification on (not a downgrade) — it just
    uses the correct CA bundle. Idempotent."""
    try:
        import certifi
        import websockets
        if getattr(websockets, "_certifi_patched", False):
            return
        _orig = websockets.connect
        _ctx = ssl.create_default_context(cafile=certifi.where())

        def _connect(uri, *args, **kwargs):
            if str(uri).startswith("wss://") and "ssl" not in kwargs:
                kwargs["ssl"] = _ctx
            return _orig(uri, *args, **kwargs)

        websockets.connect = _connect
        websockets._certifi_patched = True
        logger.info("Patched websockets.connect to use certifi CA bundle for wss.")
    except Exception as e:
        logger.warning("Could not install certifi ws SSL context: %s", e)

_L1_FIELDS = "0,1,2,4,5,8"      # 1=bid 2=ask 4=bidSize 5=askSize
_BOOK_FIELDS = "0,1,2,3"        # 2=bid levels, 3=ask levels; level 0=price 1=size
_BOOK_DEPTH = 5


def occ_to_schwab(occ: str) -> str:
    """Convert a plain OCC symbol (Alpaca form, e.g. SPY260624C00737000) to the
    space-padded form Schwab's quote API expects (root left-justified to 6:
    'SPY   260624C00737000'). Returns the input unchanged if it doesn't match."""
    import re
    m = re.match(r"^([A-Z]{1,6})(\d{6}[CP]\d{8})$", occ or "")
    if not m:
        return occ
    return m.group(1).ljust(6) + m.group(2)


class SchwabEquityFeed:
    def __init__(self, symbols: List[str]):
        self.symbols = symbols
        self._snap: Dict[str, Dict[str, Any]] = {}
        self._stream = None
        self._running = False
        self._client = None  # schwabdev Client for REST options quotes
        self._watchdog_task = None

    @property
    def running(self) -> bool:
        return self._running

    async def start(self):
        if self._running:
            return
        _install_certifi_ws_ssl()  # fix intercepting-proxy SSL on the streamer wss
        import schwabdev  # uses .env SCHWAB_APP_KEY/SECRET + tokens.json
        import os
        client = schwabdev.Client(os.getenv("SCHWAB_APP_KEY"), os.getenv("SCHWAB_APP_SECRET"))
        self._client = client
        from schwabdev.stream import StreamAsync
        self._stream = StreamAsync(client)
        self._running = True
        await self._stream.start(receiver=self._on_message)
        await self._subscribe()
        logger.info("Schwab equity feed started for %s", self.symbols)
        self._watchdog_task = asyncio.create_task(self._watchdog(), name="schwab-feed-watchdog")

    async def _subscribe(self):
        keys = ",".join(self.symbols)
        await self._stream.send(self._stream.level_one_equities(keys, _L1_FIELDS))
        await self._stream.send(self._stream.nasdaq_book(keys, _BOOK_FIELDS))

    async def _watchdog(self):
        """Keep the access token fresh and recover a stalled feed. The streamer's
        internal reconnect can't survive a 7-hour+ access-token expiry, so we
        refresh tokens periodically and, if quotes go stale, tear down + rebuild
        the stream (re-auth in browser only if the 7-day refresh token expired)."""
        STALE_S = 60
        while self._running:
            try:
                await asyncio.sleep(30)
                if not self._running:
                    break
                # keep the access token fresh (refreshes if <~1min to expiry)
                try:
                    if self._client:
                        self._client.update_tokens()
                except Exception as e:
                    logger.warning("feed token refresh failed: %s", e)
                # staleness → rebuild the stream
                ages = [self.quote_age_seconds(s) for s in self.symbols]
                fresh = [a for a in ages if a is not None]
                if fresh and min(fresh) > STALE_S:
                    logger.warning("Feed stale (min age %.0fs) — rebuilding stream", min(fresh))
                    try:
                        await self._stream.stop()
                    except Exception:
                        pass
                    try:
                        await self._stream.start(receiver=self._on_message)
                        await self._subscribe()
                        logger.info("Feed stream rebuilt")
                    except Exception as e:
                        logger.warning("Feed rebuild failed: %s", e)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("feed watchdog error: %s", e)

    async def stop(self):
        self._running = False
        wd = getattr(self, "_watchdog_task", None)
        if wd:
            wd.cancel()
        if self._stream:
            try:
                await self._stream.stop()
            except Exception:
                pass
        self._stream = None

    async def _on_message(self, raw: Any):
        try:
            msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except Exception:
            return
        for block in (msg.get("data") or []):
            service = block.get("service")
            for item in (block.get("content") or []):
                sym = item.get("key")
                if not sym:
                    continue
                s = self._snap.setdefault(sym, {})
                if service == "LEVELONE_EQUITIES":
                    if "1" in item: s["bid"] = float(item["1"])
                    if "2" in item: s["ask"] = float(item["2"])
                    if "4" in item: s["bid_size"] = int(float(item["4"])) * 100
                    if "5" in item: s["ask_size"] = int(float(item["5"])) * 100
                    s["ts"] = datetime.now(timezone.utc)
                elif service == "NASDAQ_BOOK":
                    imb = self._imbalance(item)
                    if imb is not None:
                        s["book_imbalance"] = imb

    @staticmethod
    def _imbalance(item) -> Optional[float]:
        bids, asks = item.get("2") or [], item.get("3") or []
        if not bids and not asks:
            return None
        bsz = sum(float(l.get("1", 0) or 0) for l in bids[:_BOOK_DEPTH])
        asz = sum(float(l.get("1", 0) or 0) for l in asks[:_BOOK_DEPTH])
        tot = bsz + asz
        return ((bsz - asz) / tot) if tot > 0 else None

    # engine-facing accessors
    def latest_quote(self, sym) -> Optional[Dict[str, Any]]:
        s = self._snap.get(sym)
        if not s or not s.get("bid") or not s.get("ask"):
            return None
        return {"bid": s["bid"], "ask": s["ask"], "mid": (s["bid"] + s["ask"]) / 2.0}

    def book_imbalance(self, sym) -> Optional[float]:
        s = self._snap.get(sym)
        return s.get("book_imbalance") if s else None

    def quote_age_seconds(self, sym) -> Optional[float]:
        """Seconds since the last L1 update for a symbol (None if never). Used by
        the engine to refuse trading on a stalled/frozen feed."""
        s = self._snap.get(sym)
        ts = s.get("ts") if s else None
        if ts is None:
            return None
        return (datetime.now(timezone.utc) - ts).total_seconds()

    def option_quote(self, occ: str) -> Dict[str, Any]:
        """Real-time options quote from Schwab (bid/ask/mid/mark + greeks). Blocking
        REST call — the engine wraps it in asyncio.to_thread. Returns {} on failure."""
        if self._client is None:
            return {}
        sym = occ_to_schwab(occ)
        try:
            r = self._client.quote(sym).json()
            q = (r.get(sym, {}) or {}).get("quote", {}) or {}
            bid = float(q.get("bidPrice", 0) or 0)
            ask = float(q.get("askPrice", 0) or 0)
            mark = q.get("mark")
            mid = (bid + ask) / 2 if bid > 0 and ask > 0 else (float(mark) if mark else max(bid, ask))
            return {
                "bid": bid, "ask": ask, "mid_price": mid,
                "last": float(q.get("lastPrice", 0) or 0) or None,
                "delta": q.get("delta"), "gamma": q.get("gamma"),
            }
        except Exception as e:
            logger.debug("Schwab option quote failed %s: %s", occ, e)
            return {}
