"""
bot/risk_manager.py
Gestión de riesgo profesional:
  - Criterio de Kelly fraccional para sizing óptimo
  - Triple barrera: TP / SL / tiempo
  - Protección de drawdown diario
  - Cooldown post-pérdida
  - Límite de posiciones simultáneas
"""
import logging
from datetime import date, datetime
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from config import Config
from bot.strategy import SignalResult

logger = logging.getLogger(__name__)


@dataclass
class PositionState:
    symbol:         str
    side:           str          # 'LONG' | 'SHORT'
    entry_price:    float
    quantity:       float
    tp_price:       float
    sl_price:       float
    entry_bar:      int
    open_time:      datetime = field(default_factory=datetime.utcnow)


class RiskManager:

    def __init__(self, config: Config):
        self.cfg             = config
        self._daily_loss     = 0.0
        self._last_reset_day = date.today()
        self._cooldown_until: dict[str, datetime] = {}
        self._open_count     = 0

    # ─────────────────────────────────────────
    # GUARDS DE ENTRADA
    # ─────────────────────────────────────────

    def can_trade(self, symbol: str) -> bool:
        """Devuelve True si se pueden abrir nuevas posiciones."""
        self._reset_daily_if_needed()

        if self._open_count >= self.cfg.MAX_OPEN_POSITIONS:
            logger.debug(f"{symbol}: límite de posiciones simultáneas alcanzado")
            return False

        if self._is_cooldown(symbol):
            logger.debug(f"{symbol}: en cooldown post-pérdida")
            return False

        if self._daily_loss >= self.cfg.MAX_DAILY_LOSS_PCT:
            logger.warning(f"Daily loss limit alcanzado ({self._daily_loss:.2f}%) — pausando")
            return False

        return True

    def register_open(self, symbol: str) -> None:
        self._open_count = max(0, self._open_count + 1)

    def register_close(self, symbol: str, pnl_pct: float) -> None:
        self._open_count = max(0, self._open_count - 1)
        self._reset_daily_if_needed()

        if pnl_pct < 0:
            self._daily_loss += abs(pnl_pct)
            # Cooldown de 2 × loop_interval después de una pérdida
            cool_secs = self.cfg.LOOP_INTERVAL * 2
            self._cooldown_until[symbol] = (
                datetime.utcnow().__class__.utcnow()
                .__class__.fromtimestamp(
                    datetime.utcnow().timestamp() + cool_secs
                )
            )
            logger.info(f"{symbol}: cooldown activado {cool_secs}s tras pérdida {pnl_pct:.2f}%")

    # ─────────────────────────────────────────
    # SIZING — KELLY FRACCIONAL
    # ─────────────────────────────────────────

    def calculate_position_size(self, signal: SignalResult,
                                balance_usdt: float) -> float:
        """
        Tamaño de posición usando Kelly fraccional (1/4 Kelly):
          f* = (edge) / (riesgo_por_trade)
          Limitado a cfg.RISK_PER_TRADE % del capital.
        """
        # Ratio TP/SL implícito de la configuración
        rr = self.cfg.ATR_MULT_TP / self.cfg.ATR_MULT_SL   # p.ej. 2.0/1.2 = 1.667

        # Probabilidad bayesiana de ganar (desde Markov)
        p_win = (signal.prob_bull if signal.long else signal.prob_bear) / 100.0
        p_win = max(0.45, min(0.75, p_win))                # clip conservador

        kelly_full = (p_win * rr - (1 - p_win)) / rr
        kelly_frac = max(0.0, kelly_full / 4)              # 1/4 Kelly

        # Capital en riesgo (% del balance)
        risk_pct = min(kelly_frac * 100, self.cfg.RISK_PER_TRADE)
        risk_usdt = balance_usdt * (risk_pct / 100)

        # Convertir a tamaño con apalancamiento
        notional = risk_usdt * self.cfg.LEVERAGE
        if signal.entry_price > 0:
            qty = notional / signal.entry_price
        else:
            qty = 0.0

        logger.info(
            f"Kelly sizing: p_win={p_win:.2%} rr={rr:.2f} "
            f"kelly_frac={kelly_frac:.3f} risk_pct={risk_pct:.2f}% "
            f"qty={qty:.6f}"
        )
        return qty

    # ─────────────────────────────────────────
    # TRIPLE BARRERA
    # ─────────────────────────────────────────

    def compute_barriers(self, entry_price: float, atr14: float,
                         side: str) -> tuple[float, float]:
        """
        Devuelve (tp_price, sl_price).
        """
        tp_dist = atr14 * self.cfg.ATR_MULT_TP
        sl_dist = atr14 * self.cfg.ATR_MULT_SL

        if side == "LONG":
            tp = entry_price + tp_dist
            sl = entry_price - sl_dist
        else:
            tp = entry_price - tp_dist
            sl = entry_price + sl_dist

        return round(tp, 6), round(sl, 6)

    def check_time_exit(self, position_state: PositionState,
                        current_bar: int) -> bool:
        """Barrera de tiempo: cierra si llevamos demasiadas velas."""
        bars_held = current_bar - position_state.entry_bar
        return bars_held >= self.cfg.MAX_BARS_HOLD

    # ─────────────────────────────────────────
    # INTERNOS
    # ─────────────────────────────────────────

    def _reset_daily_if_needed(self) -> None:
        today = date.today()
        if today != self._last_reset_day:
            self._daily_loss     = 0.0
            self._last_reset_day = today
            logger.info("Daily PnL counter reseteado")

    def _is_cooldown(self, symbol: str) -> bool:
        until = self._cooldown_until.get(symbol)
        if until is None:
            return False
        return datetime.utcnow() < until

    @property
    def daily_loss_pct(self) -> float:
        return self._daily_loss

    @property
    def open_positions(self) -> int:
        return self._open_count
