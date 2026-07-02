"""
RiskManager — daily loss limit.
Resets PnL counter at UTC midnight.

FIX: _day_pnl / _day_start_eq / _today ahora persisten vía state.py.
Antes vivían solo en RAM — un redeploy a mitad de día reseteaba el
contador a 0, permitiendo saltarse MAX_DAILY_LOSS_PCT de facto si
había varios redeploys en la misma jornada.
"""
import logging
from datetime import datetime, timezone, date

import state

log = logging.getLogger("risk_mgr")


class RiskManager:
    def __init__(self, cfg):
        self.cfg = cfg
        saved_pnl, saved_eq, saved_day = state.get_day_state()
        self._day_pnl      = saved_pnl if saved_pnl is not None else 0.0
        self._day_start_eq = saved_eq
        self._today         = date.fromisoformat(saved_day) if saved_day else None
        if saved_day:
            log.info(f"RiskManager: día restaurado desde state.py — "
                     f"day_pnl={self._day_pnl:+.2f}  day_start_eq={self._day_start_eq}")

    # ── Public ────────────────────────────────────────────────

    def can_trade(self, equity: float) -> tuple:
        """Returns (allowed: bool, reason: str)."""
        self._maybe_reset(equity)
        if self._day_start_eq and self._day_start_eq > 0:
            loss_pct = (-self._day_pnl / self._day_start_eq) * 100.0
            if loss_pct >= self.cfg.MAX_DAILY_LOSS_PCT:
                msg = f"Daily loss limit: {loss_pct:.1f}% >= {self.cfg.MAX_DAILY_LOSS_PCT}%"
                log.warning(msg)
                return False, msg
        return True, "ok"

    def record_trade(self, pnl_usdt: float):
        self._day_pnl += pnl_usdt
        state.save_day_state(self._day_pnl, self._day_start_eq, self._today.isoformat())
        log.info(f"PnL recorded: {pnl_usdt:+.2f}  day_total={self._day_pnl:+.2f}")

    @property
    def day_pnl(self) -> float:
        return self._day_pnl

    # ── Internal ──────────────────────────────────────────────

    def _maybe_reset(self, equity: float):
        today = datetime.now(tz=timezone.utc).date()
        if today != self._today:
            self._today        = today
            self._day_pnl      = 0.0
            self._day_start_eq = equity
            state.save_day_state(self._day_pnl, self._day_start_eq, self._today.isoformat())
            log.info(f"New day — start equity: {equity:.2f} USDT")
