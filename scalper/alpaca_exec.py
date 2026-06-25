"""
Alpaca execution shim for the standalone scalper — options chain discovery,
NBBO quotes, and order placement/closing on the paper account. Thin wrapper over
alpaca-py 0.43.x so the engine stays broker-agnostic.

Synchronous SDK calls are wrapped in asyncio.to_thread by the engine.
"""

from __future__ import annotations

import logging
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class AlpacaExec:
    def __init__(self, api_key: str, api_secret: str, paper: bool = True):
        from alpaca.trading.client import TradingClient
        from alpaca.data.historical.option import OptionHistoricalDataClient
        self._trading = TradingClient(api_key, api_secret, paper=paper)
        self._opt_data = OptionHistoricalDataClient(api_key, api_secret)

    # ── account / equity ──────────────────────────────────────────────────
    def equity(self) -> Optional[float]:
        try:
            return float(self._trading.get_account().equity)
        except Exception as e:
            logger.warning("Alpaca equity fetch failed: %s", e)
            return None

    # ── chain discovery ───────────────────────────────────────────────────
    def find_contracts(self, underlying: str, opt_type: str, max_dte: int) -> List[Dict[str, Any]]:
        """Near-the-money 0..max_dte contracts for the underlying. opt_type: call|put."""
        from alpaca.trading.requests import GetOptionContractsRequest
        from alpaca.trading.enums import ContractType, AssetStatus
        today = date.today()
        try:
            req = GetOptionContractsRequest(
                underlying_symbols=[underlying],
                status=AssetStatus.ACTIVE,
                type=ContractType.CALL if opt_type == "call" else ContractType.PUT,
                expiration_date_gte=today,
                expiration_date_lte=today + timedelta(days=max_dte),
                limit=200,
            )
            resp = self._trading.get_option_contracts(req)
            out = []
            for c in (resp.option_contracts or []):
                out.append({
                    "symbol": c.symbol,
                    "strike": float(c.strike_price),
                    "expiration": str(c.expiration_date),
                    "type": opt_type,
                })
            return out
        except Exception as e:
            logger.warning("Alpaca chain fetch failed for %s: %s", underlying, e)
            return []

    # ── quotes ────────────────────────────────────────────────────────────
    def option_quote(self, occ: str) -> Dict[str, Any]:
        from alpaca.data.requests import OptionLatestQuoteRequest
        try:
            req = OptionLatestQuoteRequest(symbol_or_symbols=occ)
            q = self._opt_data.get_option_latest_quote(req).get(occ)
            if q is None:
                return {}
            bid = float(getattr(q, "bid_price", 0) or 0)
            ask = float(getattr(q, "ask_price", 0) or 0)
            mid = (bid + ask) / 2 if bid > 0 and ask > 0 else max(bid, ask)
            return {"bid": bid, "ask": ask, "mid_price": mid}
        except Exception as e:
            logger.debug("Alpaca option quote failed %s: %s", occ, e)
            return {}

    # ── orders ────────────────────────────────────────────────────────────
    def buy_to_open(self, occ: str, qty: int, limit_price: float) -> Optional[Dict[str, Any]]:
        return self._submit(occ, qty, "buy", limit_price)

    def sell_to_close(self, occ: str, qty: int, limit_price: Optional[float]) -> Optional[Dict[str, Any]]:
        return self._submit(occ, qty, "sell", limit_price)

    def _submit(self, occ: str, qty: int, side: str, limit_price: Optional[float]) -> Optional[Dict[str, Any]]:
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        oside = OrderSide.BUY if side == "buy" else OrderSide.SELL
        try:
            if limit_price and limit_price > 0:
                order = self._trading.submit_order(LimitOrderRequest(
                    symbol=occ, qty=qty, side=oside,
                    time_in_force=TimeInForce.DAY, limit_price=round(limit_price, 2),
                ))
            else:
                order = self._trading.submit_order(MarketOrderRequest(
                    symbol=occ, qty=qty, side=oside, time_in_force=TimeInForce.DAY,
                ))
            return {"order_id": str(order.id), "status": str(order.status)}
        except Exception as e:
            logger.warning("Alpaca %s order failed for %s: %s", side, occ, e)
            return None

    def fill_price(self, order_id: str, fallback: float) -> float:
        try:
            od = self._trading.get_order_by_id(order_id)
            fap = getattr(od, "filled_avg_price", None)
            return float(fap) if fap and float(fap) > 0 else fallback
        except Exception:
            return fallback
