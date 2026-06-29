"""
RiskManager — daily loss limit.
Resets PnL counter at UTC midnight.
"""

import logging
from datetime import datetime, timezone

log = logging.getLogger("risk_mgr")


class RiskManager:
    def __init__(self, cfg):
        self.cfg             = cfg
        self._day_pnl        = 0.0
        self._day_start_eq   = None
        self._today          = None

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
            log.info(f"New day — start equity: {equity:.2f} USDT")
