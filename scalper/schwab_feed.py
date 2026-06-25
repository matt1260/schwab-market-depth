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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_L1_FIELDS = "0,1,2,4,5,8"      # 1=bid 2=ask 4=bidSize 5=askSize
_BOOK_FIELDS = "0,1,2,3"        # 2=bid levels, 3=ask levels; level 0=price 1=size
_BOOK_DEPTH = 5


class SchwabEquityFeed:
    def __init__(self, symbols: List[str]):
        self.symbols = symbols
        self._snap: Dict[str, Dict[str, Any]] = {}
        self._stream = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    async def start(self):
        if self._running:
            return
        import schwabdev  # uses .env SCHWAB_APP_KEY/SECRET + tokens.json
        import os
        client = schwabdev.Client(os.getenv("SCHWAB_APP_KEY"), os.getenv("SCHWAB_APP_SECRET"))
        from schwabdev.stream import StreamAsync
        self._stream = StreamAsync(client)
        self._running = True
        await self._stream.start(receiver=self._on_message)
        keys = ",".join(self.symbols)
        await self._stream.send(self._stream.level_one_equities(keys, _L1_FIELDS))
        await self._stream.send(self._stream.nasdaq_book(keys, _BOOK_FIELDS))
        logger.info("Schwab equity feed started for %s", self.symbols)

    async def stop(self):
        if self._stream and self._running:
            try:
                await self._stream.stop()
            except Exception:
                pass
        self._running = False
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
