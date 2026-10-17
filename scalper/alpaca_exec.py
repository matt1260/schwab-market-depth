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

import re as _re
_OCC_RE = _re.compile(r"^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


def _parse_occ(occ: str):
    """(strike float, expiry date) from an OCC symbol, else (None, None)."""
    m = _OCC_RE.match(occ or "")
    if not m:
        return None, None
    _, yy, mm, dd, _cp, strike8 = m.groups()
    try:
        return int(strike8) / 1000.0, date(2000 + int(yy), int(mm), int(dd))
    except Exception:
        return None, None


class AlpacaExec:
    def __init__(self, api_key: str, api_secret: str, paper: bool = True):
        from alpaca.trading.client import TradingClient
        from alpaca.data.historical.option import OptionHistoricalDataClient
        self._trading = TradingClient(api_key, api_secret, paper=paper)
        self._opt_data = OptionHistoricalDataClient(api_key, api_secret)
        self._api_key = api_key
        self._api_secret = api_secret
        self._paper = paper

    # ── account / equity ──────────────────────────────────────────────────
    def equity(self) -> Optional[float]:
        try:
            return float(self._trading.get_account().equity)
        except Exception as e:
            logger.warning("Alpaca equity fetch failed: %s", e)
            return None

    def account_health(self) -> Dict[str, Any]:
        """Full account reachability/readiness — surfaces the real error instead
        of swallowing it. Used to gate live trading (don't go live on an account
        that's unauthorized, unfunded, blocked, or not options-approved)."""
        try:
            a = self._trading.get_account()
            equity = float(a.equity)
            opt_lvl = getattr(a, "options_trading_level", None) or getattr(a, "options_approved_level", None)
            ok = (str(a.status) in ("AccountStatus.ACTIVE", "ACTIVE")
                  and not a.account_blocked and not a.trading_blocked
                  and equity > 0 and (opt_lvl or 0) >= 1)
            return {"ok": ok, "status": str(a.status), "equity": equity,
                    "cash": float(a.cash), "buying_power": float(a.buying_power),
                    "options_level": opt_lvl,
                    "account_blocked": a.account_blocked, "trading_blocked": a.trading_blocked}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── chain discovery ───────────────────────────────────────────────────
    def find_contracts(self, underlying: str, opt_type: str, max_dte: int,
                       spot: Optional[float] = None, band_pct: float = 3.0) -> List[Dict[str, Any]]:
        """Near-the-money 0..max_dte contracts for the underlying. opt_type: call|put.
        When `spot` is given, filter strikes to ±band_pct% of spot so the result is
        CENTERED ON THE MONEY — without this, limit=200 returns the lowest strikes and
        the true ATM strike is excluded (the engine then picks a deep-ITM contract and
        the qty math rounds to 0 = no trade)."""
        from alpaca.trading.requests import GetOptionContractsRequest
        from alpaca.trading.enums import ContractType, AssetStatus
        today = date.today()
        try:
            kwargs = dict(
                underlying_symbols=[underlying],
                status=AssetStatus.ACTIVE,
                type=ContractType.CALL if opt_type == "call" else ContractType.PUT,
                expiration_date_gte=today,
                expiration_date_lte=today + timedelta(days=max_dte),
                limit=200,
            )
            if spot and spot > 0:
                kwargs["strike_price_gte"] = str(round(spot * (1 - band_pct / 100.0), 2))
                kwargs["strike_price_lte"] = str(round(spot * (1 + band_pct / 100.0), 2))
            req = GetOptionContractsRequest(**kwargs)
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

    # ── positions (for rehydrating open spreads across restarts) ───────────
    def get_option_positions(self) -> List[Dict[str, Any]]:
        """Open OPTION positions: [{occ, qty(signed: -short/+long), avg_entry_price,
        strike, type, expiration}]. Used to rebuild open spreads after a restart."""
        try:
            poss = self._trading.get_all_positions()
        except Exception as e:
            logger.warning("Alpaca positions fetch failed: %s", e)
            return []
        out = []
        for p in (poss or []):
            occ = getattr(p, "symbol", "")
            strike, expiry = _parse_occ(occ)
            if strike is None:
                continue  # not an OCC option symbol (equity etc.)
            try:
                qty = int(float(getattr(p, "qty", 0)))
            except Exception:
                qty = 0
            out.append({
                "occ": occ, "qty": qty,
                "avg_entry_price": float(getattr(p, "avg_entry_price", 0) or 0),
                "strike": strike, "expiration": str(expiry) if expiry else None,
                "type": "put" if _OCC_RE.match(occ).group(5) == "P" else "call",
            })
        return out

    # ── chain with greeks (for spread strike selection) ────────────────────
    def get_put_chain(self, underlying: str, dte_min: int, dte_max: int,
                      strike_lo: Optional[float] = None,
                      strike_hi: Optional[float] = None) -> List[Dict[str, Any]]:
        return self.get_chain(underlying, "put", dte_min, dte_max, strike_lo, strike_hi)

    def get_call_chain(self, underlying: str, dte_min: int, dte_max: int,
                       strike_lo: Optional[float] = None,
                       strike_hi: Optional[float] = None) -> List[Dict[str, Any]]:
        return self.get_chain(underlying, "call", dte_min, dte_max, strike_lo, strike_hi)

    def get_chain(self, underlying: str, opt_type: str, dte_min: int, dte_max: int,
                  strike_lo: Optional[float] = None,
                  strike_hi: Optional[float] = None) -> List[Dict[str, Any]]:
        """Option snapshots (opt_type: put|call) in [dte_min, dte_max] with greeks
        (delta), IV, and NBBO — one call. Returns [{occ, strike, expiration, dte,
        delta, iv, bid, ask, mid}] sorted by strike. Greeks come back even on the
        indicative feed (verified)."""
        from alpaca.data.requests import OptionChainRequest
        from alpaca.trading.enums import ContractType
        today = date.today()
        try:
            kwargs = dict(
                underlying_symbol=underlying,
                type=ContractType.CALL if opt_type == "call" else ContractType.PUT,
                expiration_date_gte=str(today + timedelta(days=dte_min)),
                expiration_date_lte=str(today + timedelta(days=dte_max)),
            )
            if strike_lo is not None:
                kwargs["strike_price_gte"] = strike_lo
            if strike_hi is not None:
                kwargs["strike_price_lte"] = strike_hi
            ch = self._opt_data.get_option_chain(OptionChainRequest(**kwargs))
        except Exception as e:
            logger.warning("Alpaca %s chain failed for %s: %s", opt_type, underlying, e)
            return []
        out = []
        for occ, snap in (ch or {}).items():
            g = getattr(snap, "greeks", None)
            q = getattr(snap, "latest_quote", None)
            delta = getattr(g, "delta", None) if g else None
            bid = float(getattr(q, "bid_price", 0) or 0) if q else 0.0
            ask = float(getattr(q, "ask_price", 0) or 0) if q else 0.0
            if delta is None:
                continue
            # parse strike + expiry from the OCC symbol (…YYMMDD[C/P]00000000)
            strike, expiry = _parse_occ(occ)
            if strike is None:
                continue
            dte = (expiry - today).days if expiry else None
            out.append({
                "occ": occ, "strike": strike, "expiration": str(expiry) if expiry else None,
                "dte": dte, "delta": float(delta),
                "iv": float(getattr(snap, "implied_volatility", 0) or 0) or None,
                "bid": bid, "ask": ask,
                "mid": round((bid + ask) / 2, 2) if (bid > 0 and ask > 0) else (bid or ask),
            })
        out.sort(key=lambda c: c["strike"])
        return out

    def atm_iv(self, underlying: str, spot: float, dte_min: int = 30, dte_max: int = 45) -> Optional[float]:
        """Current ATM implied vol (nearest-strike put, ~30-45 DTE) for the scanner's
        IV-rank. One chain call. Returns None if unavailable."""
        chain = self.get_put_chain(underlying, dte_min, dte_max,
                                   strike_lo=spot * 0.97, strike_hi=spot * 1.03)
        if not chain:
            return None
        atm = min(chain, key=lambda c: abs(c["strike"] - spot))
        return atm.get("iv")

    # ── multi-leg credit spread (MLEG) — proven pattern from the platform ───
    def _mleg(self, legs: List[Dict[str, Any]], qty: int, signed_limit: float) -> Optional[Dict[str, Any]]:
        """Raw MLEG order. signed_limit: NEGATIVE = net credit (we receive),
        POSITIVE = net debit (we pay). Alpaca's risk engine validates the sign
        against the position intent — do NOT strip it."""
        import httpx
        base = "https://paper-api.alpaca.markets" if self._paper else "https://api.alpaca.markets"
        payload = {
            "order_class": "mleg", "qty": str(qty), "type": "limit",
            "limit_price": str(round(float(signed_limit), 2)), "time_in_force": "day",
            "legs": [{"symbol": l["symbol"], "ratio_qty": "1",
                      "side": l["side"], "position_intent": l["position_intent"]} for l in legs],
        }
        headers = {"APCA-API-KEY-ID": self._api_key, "APCA-API-SECRET-KEY": self._api_secret,
                   "accept": "application/json", "content-type": "application/json"}
        try:
            r = httpx.post(f"{base}/v2/orders", headers=headers, json=payload, timeout=10)
            if r.status_code not in (200, 201):
                logger.warning("MLEG order failed [%s]: %s", r.status_code, r.text[:300])
                return None
            d = r.json()
            return {"order_id": d.get("id", ""), "status": d.get("status", ""),
                    "limit_price": float(d.get("limit_price") or signed_limit)}
        except Exception as e:
            logger.warning("MLEG order error: %s", e)
            return None

    def order_status(self, order_id: str) -> Dict[str, Any]:
        """{status, filled_qty, filled_avg_price} for an order id (MLEG parent or simple)."""
        try:
            o = self._trading.get_order_by_id(order_id)
            return {"status": str(getattr(o, "status", "")),
                    "filled_qty": int(float(getattr(o, "filled_qty", 0) or 0)),
                    "filled_avg_price": float(getattr(o, "filled_avg_price", 0) or 0) or None}
        except Exception as e:
            logger.debug("order_status failed %s: %s", order_id, e)
            return {"status": "unknown"}

    def cancel_order(self, order_id: str) -> bool:
        try:
            self._trading.cancel_order_by_id(order_id)
            return True
        except Exception as e:
            logger.debug("cancel_order failed %s: %s", order_id, e)
            return False

    def open_put_credit_spread(self, short_occ: str, long_occ: str, qty: int,
                               net_credit: float) -> Optional[Dict[str, Any]]:
        """Sell the short put, buy the long put (lower strike) for ONE net credit.
        limit submitted NEGATIVE = the credit we want to receive."""
        legs = [{"symbol": short_occ, "side": "sell", "position_intent": "sell_to_open"},
                {"symbol": long_occ, "side": "buy", "position_intent": "buy_to_open"}]
        return self._mleg(legs, qty, -abs(round(net_credit, 2)))

    def close_put_credit_spread(self, short_occ: str, long_occ: str, qty: int,
                                net_debit: float) -> Optional[Dict[str, Any]]:
        """Buy back the short put, sell the long put — pay a net debit to close.
        limit submitted POSITIVE = the debit we'll pay."""
        legs = [{"symbol": short_occ, "side": "buy", "position_intent": "buy_to_close"},
                {"symbol": long_occ, "side": "sell", "position_intent": "sell_to_close"}]
        return self._mleg(legs, qty, abs(round(net_debit, 2)))

    def open_call_credit_spread(self, short_occ: str, long_occ: str, qty: int,
                                net_credit: float) -> Optional[Dict[str, Any]]:
        """Sell the short call (lower strike), buy the long call (higher strike) for a
        net credit — a bearish/neutral bet (profits if price stays DOWN). Mirror of
        the put credit spread; same signed-limit convention (NEGATIVE = credit)."""
        legs = [{"symbol": short_occ, "side": "sell", "position_intent": "sell_to_open"},
                {"symbol": long_occ, "side": "buy", "position_intent": "buy_to_open"}]
        return self._mleg(legs, qty, -abs(round(net_credit, 2)))

    def close_call_credit_spread(self, short_occ: str, long_occ: str, qty: int,
                                 net_debit: float) -> Optional[Dict[str, Any]]:
        legs = [{"symbol": short_occ, "side": "buy", "position_intent": "buy_to_close"},
                {"symbol": long_occ, "side": "sell", "position_intent": "sell_to_close"}]
        return self._mleg(legs, qty, abs(round(net_debit, 2)))

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
