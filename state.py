"""
BotState — tracks open trades, daily limits, circuit breaker.
"""
import logging
from datetime import date

log = logging.getLogger("qfjp.state")


class BotState:
    def __init__(self):
        self.open_trades:    int   = 0
        self.open_symbols:   set   = set()
        self.daily_trades:   int   = 0
        self.daily_pnl:      float = 0.0
        self.circuit_broken: bool  = False
        self._today:         date  = date.today()
        self._status_counter: int  = 0

    def _rollover(self):
        today = date.today()
        if today != self._today:
            log.info("📅 Day rollover — resetting daily counters")
            self.daily_trades   = 0
            self.daily_pnl      = 0.0
            self.circuit_broken = False
            self._today         = today

    def record_trade(self, signal: dict, order: dict):
        self._rollover()
        self.open_trades  += 1
        self.daily_trades += 1
        sym = signal.get("symbol", "")
        if sym:
            self.open_symbols.add(sym)
        log.info(f"Trade recorded {sym}. Open:{self.open_trades} Daily:{self.daily_trades}")

    def close_trade(self, symbol: str, pnl: float = 0.0):
        self._rollover()
        self.open_trades = max(0, self.open_trades - 1)
        self.open_symbols.discard(symbol)
        self.daily_pnl  += pnl
        log.info(f"Trade closed {symbol}. PnL:{pnl:.2f} DailyPnL:{self.daily_pnl:.2f}")
        if self.daily_pnl < -50:
            self.circuit_broken = True
            log.warning("⚡ Circuit breaker triggered — daily loss > $50")

    def should_send_status(self, every: int = 20) -> bool:
        self._status_counter += 1
        if self._status_counter >= every:
            self._status_counter = 0
            return True
        return False

    @property
    def summary(self) -> str:
        self._rollover()
        return (
            f"Open: {self.open_trades} | Daily: {self.daily_trades} | "
            f"PnL día: ${self.daily_pnl:.2f} | "
            f"Símbolos: {', '.join(self.open_symbols) or '—'} | "
            f"CB: {'🔴ON' if self.circuit_broken else '🟢OFF'}"
        )
