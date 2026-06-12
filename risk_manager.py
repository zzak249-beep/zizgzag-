"""
QF×JP Bot v6.4 — Risk Manager
Controla:
  - Límite de trades abiertos simultáneos
  - Límite de trades diarios
  - Daily drawdown máximo
  - Sizing Kelly Criterion
  - Tier filter (STD / FUEL / SUP)
"""
import asyncio
import logging
from datetime import datetime, date

import config as C

log = logging.getLogger("risk")


class RiskManager:
    def __init__(self):
        self._lock          = asyncio.Lock()
        self._open_count    = 0
        self._daily_trades  = 0
        self._daily_pnl     = 0.0
        self._last_reset    = date.today()

    # ── Reset diario ──────────────────────────────────────────────────────────

    def _check_daily_reset(self):
        today = date.today()
        if today != self._last_reset:
            log.info("Reset diario: trades=%d pnl=%.2f", self._daily_trades, self._daily_pnl)
            self._daily_trades = 0
            self._daily_pnl    = 0.0
            self._last_reset   = today

    # ── Consultas ─────────────────────────────────────────────────────────────

    async def can_trade(self) -> tuple[bool, str]:
        """Retorna (True, '') si se puede abrir trade, (False, motivo) si no."""
        async with self._lock:
            self._check_daily_reset()

            if self._open_count >= C.MAX_OPEN_TRADES:
                return False, f"max_open_trades ({self._open_count}/{C.MAX_OPEN_TRADES})"

            if self._daily_trades >= C.MAX_DAILY_TRADES:
                return False, f"max_daily_trades ({self._daily_trades}/{C.MAX_DAILY_TRADES})"

            # Daily drawdown: si las pérdidas superan el 5% del capital → parar
            max_dd = C.CAPITAL * 0.05
            if self._daily_pnl < -max_dd:
                return False, f"daily_drawdown (pnl={self._daily_pnl:.2f} < -{max_dd:.2f})"

            return True, ""

    def tier_ok(self, tier: str) -> bool:
        """Filtra por tier mínimo configurado."""
        order = {"NONE": 0, "STD": 1, "FUEL": 2, "SUP": 3}
        return order.get(tier, 0) >= order.get(C.MIN_TIER, 1)

    # ── Eventos ───────────────────────────────────────────────────────────────

    async def on_trade_opened(self):
        async with self._lock:
            self._open_count   += 1
            self._daily_trades += 1
            log.info("Trade abierto — open=%d daily=%d",
                     self._open_count, self._daily_trades)

    async def on_trade_closed(self, pnl: float = 0.0):
        async with self._lock:
            self._open_count = max(0, self._open_count - 1)
            self._daily_pnl += pnl
            log.info("Trade cerrado — pnl=%.4f daily_pnl=%.4f open=%d",
                     pnl, self._daily_pnl, self._open_count)

    async def update_open_count(self, real_count: int):
        """Sincroniza el contador con la realidad de BingX."""
        async with self._lock:
            if self._open_count != real_count:
                log.debug("open_count corregido %d → %d",
                          self._open_count, real_count)
                self._open_count = real_count

    # ── Kelly position sizing ─────────────────────────────────────────────────

    def kelly_position_size(
        self,
        balance:   float,
        entry:     float,
        sl:        float,
        score:     float,
        tier:      str,
    ) -> float:
        """
        Calcula la cantidad de contratos usando Kelly fraccionado.
        Escala el tamaño según tier: STD=1x, FUEL=1.25x, SUP=1.5x.
        Retorna 0.0 si no se puede calcular.
        """
        if entry <= 0 or sl <= 0 or abs(entry - sl) < 1e-12:
            return 0.0

        w = C.KELLY_WIN_RATE
        r = C.KELLY_RR
        kelly = (w * r - (1 - w)) / r   # Kelly completo
        kelly = max(0.0, kelly)
        frac  = kelly * C.KELLY_FRACTION

        # Escalar por tier
        tier_mult = {"STD": 1.0, "FUEL": 1.25, "SUP": 1.5}.get(tier, 1.0)
        frac *= tier_mult

        risk_usdt  = balance * (C.RISK_PCT / 100) * frac
        sl_dist    = abs(entry - sl)
        qty        = (risk_usdt * C.LEVERAGE) / (sl_dist * entry) if sl_dist * entry > 0 else 0.0

        log.debug(
            "Kelly: balance=%.2f kelly=%.4f frac=%.4f tier_mult=%.2f "
            "risk_usdt=%.4f sl_dist=%.6f qty=%.6f",
            balance, kelly, frac, tier_mult, risk_usdt, sl_dist, qty,
        )
        return max(0.0, qty)

    # ── Status ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        self._check_daily_reset()
        return {
            "open_trades":  self._open_count,
            "daily_trades": self._daily_trades,
            "daily_pnl":    round(self._daily_pnl, 4),
            "max_open":     C.MAX_OPEN_TRADES,
            "max_daily":    C.MAX_DAILY_TRADES,
            "mode":         C.MODE,
        }
