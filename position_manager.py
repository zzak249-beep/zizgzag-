"""
PositionManager — wraps BingXClient with sizing logic
and in-memory ATR trailing stop state.
"""

import logging
import math

from bingx_client import BingXClient
from strategy import update_trailing_stop, trail_stop_hit

log = logging.getLogger("pos_mgr")


class PositionManager:
    def __init__(self, client: BingXClient, cfg):
        self.client = client
        self.cfg    = cfg
        # in-memory trail stop: key = "SYMBOL_SIDE"
        self._trail: dict[str, float] = {}

    # ── Queries ───────────────────────────────────────────────

    def get_position(self, symbol: str, side: str) -> dict | None:
        for p in self.client.get_positions(symbol):
            if p["positionSide"] == side:
                return p
        return None

    def has_position(self, symbol: str, side: str) -> bool:
        return self.get_position(symbol, side) is not None

    # ── Quantity calculation ───────────────────────────────────

    def calc_qty(self, symbol: str, mark_price: float, atr: float, equity: float) -> float:
        """
        Risk-based sizing:
          qty = (equity * risk_pct/100) / (atr * atr_mult)
        Capped by MAX_NOTIONAL_USDT / mark_price.
        Rounded to symbol precision.
        """
        risk_usdt = equity * (self.cfg.RISK_PCT / 100.0)
        sl_usdt   = atr * self.cfg.ATR_MULT
        if sl_usdt <= 0:
            log.warning("sl_usdt=0, using min qty")
            return self._min_qty(symbol)

        qty = risk_usdt / sl_usdt

        # Notional cap
        if mark_price > 0:
            qty = min(qty, self.cfg.MAX_NOTIONAL_USDT / mark_price)

        # Round to symbol precision
        qty = self._round_qty(symbol, qty)
        return max(qty, self._min_qty(symbol))

    def _sym_info(self, symbol: str) -> dict:
        try:
            return self.client.get_symbol_info(symbol)
        except Exception:
            return {}

    def _min_qty(self, symbol: str) -> float:
        info = self._sym_info(symbol)
        return float(info.get("tradeMinQuantity", 0.001))

    def _round_qty(self, symbol: str, qty: float) -> float:
        info  = self._sym_info(symbol)
        scale = int(info.get("quantityScale", 3))
        factor = 10 ** scale
        return math.floor(qty * factor) / factor

    # ── Entries ───────────────────────────────────────────────

    def open_long(self, symbol: str, qty: float) -> dict:
        log.info(f"OPEN LONG  {symbol}  qty={qty}")
        self.client.set_leverage(symbol, self.cfg.LEVERAGE)
        result = self.client.place_market_order(symbol, "BUY", "LONG", qty)
        self._trail.pop(f"{symbol}_LONG", None)
        return result

    def open_short(self, symbol: str, qty: float) -> dict:
        log.info(f"OPEN SHORT {symbol}  qty={qty}")
        self.client.set_leverage(symbol, self.cfg.LEVERAGE)
        result = self.client.place_market_order(symbol, "SELL", "SHORT", qty)
        self._trail.pop(f"{symbol}_SHORT", None)
        return result

    # ── Exits ─────────────────────────────────────────────────

    def close_long(self, symbol: str, qty: float, reason: str = "") -> dict:
        log.info(f"CLOSE LONG  {symbol}  qty={qty}  reason={reason}")
        result = self.client.close_position(symbol, "LONG", qty)
        self._trail.pop(f"{symbol}_LONG", None)
        return result

    def close_short(self, symbol: str, qty: float, reason: str = "") -> dict:
        log.info(f"CLOSE SHORT {symbol}  qty={qty}  reason={reason}")
        result = self.client.close_position(symbol, "SHORT", qty)
        self._trail.pop(f"{symbol}_SHORT", None)
        return result

    # ── Trailing stop ─────────────────────────────────────────

    def tick_trail(self, symbol: str, side: str, price: float, atr: float) -> tuple:
        """
        Update in-memory trail stop.
        Returns (new_stop: float, is_hit: bool).
        """
        key     = f"{symbol}_{side}"
        current = self._trail.get(key)
        new_stop = update_trailing_stop(side, price, atr, self.cfg.ATR_MULT, current)
        self._trail[key] = new_stop
        hit = trail_stop_hit(side, price, new_stop)
        return new_stop, hit

    def get_trail_stop(self, symbol: str, side: str) -> float | None:
        return self._trail.get(f"{symbol}_{side}")

    def reset_trail(self, symbol: str, side: str):
        self._trail.pop(f"{symbol}_{side}", None)
