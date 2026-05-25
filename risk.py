"""
Gestión de Riesgo — QF Bot v3.1
FIXES:
  - daily_trades solo cuenta EJECUCIONES reales, no checks del scanner
  - circuit breaker diario se resetea correctamente
  - equity se puede sincronizar con BingX al arrancar
"""
import json
import logging
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)
STATE_FILE = Path("logs/risk_state.json")


@dataclass
class RiskState:
    initial_equity:      float = 500.0
    peak_equity:         float = 500.0
    current_equity:      float = 500.0
    daily_start:         float = 500.0
    daily_pnl:           float = 0.0
    daily_trades:        int   = 0      # SOLO trades ejecutados
    last_reset_date:     str   = ""
    consecutive_losses:  int   = 0
    circuit_open:        bool  = False
    circuit_reason:      str   = ""


class RiskManager:
    def __init__(self, cfg: dict):
        self.c = cfg
        self.state = self._load_state()

    def _load_state(self) -> RiskState:
        STATE_FILE.parent.mkdir(exist_ok=True)
        if STATE_FILE.exists():
            try:
                d = json.loads(STATE_FILE.read_text())
                s = RiskState(**{k: v for k, v in d.items() if k in RiskState.__dataclass_fields__})
                # Sincronizar equity inicial si cambió en config
                if s.initial_equity != self.c['initial_equity'] and s.daily_trades == 0:
                    s.initial_equity = self.c['initial_equity']
                return s
            except Exception as e:
                logger.warning(f"No se pudo cargar estado: {e} — creando nuevo")
        eq = self.c['initial_equity']
        return RiskState(
            initial_equity=eq, peak_equity=eq,
            current_equity=eq, daily_start=eq,
        )

    def _save(self):
        STATE_FILE.write_text(json.dumps(asdict(self.state), indent=2))

    def _check_daily_reset(self):
        today = date.today().isoformat()
        if self.state.last_reset_date != today:
            logger.info("📅 Reset diario — nueva sesión")
            self.state.daily_start  = self.state.current_equity
            self.state.daily_pnl    = 0.0
            self.state.daily_trades = 0
            self.state.last_reset_date = today
            # Solo resetear circuit breakers diarios
            if self.state.circuit_open and any(x in self.state.circuit_reason
               for x in ["diario", "operaciones", "consecutivas"]):
                self.state.circuit_open   = False
                self.state.circuit_reason = ""
                logger.info("✅ Circuit breaker diario reseteado")
            self._save()

    def check_circuit(self) -> tuple[bool, str]:
        """
        Retorna (puede_operar, razon).
        SOLO bloquea si hay razón real — no cuenta intentos del scanner.
        """
        self._check_daily_reset()
        if self.state.circuit_open:
            return False, self.state.circuit_reason

        c = self.c; s = self.state

        # 1. Pérdida diaria
        if s.daily_pnl < 0:
            loss_pct = abs(s.daily_pnl) / max(s.daily_start, 1) * 100
            if loss_pct >= c['max_daily_loss_pct']:
                reason = f"⛔ Pérdida diaria {loss_pct:.1f}% — reset mañana (diario)"
                self._trigger_circuit(reason)
                return False, reason

        # 2. Drawdown máximo
        dd = (s.peak_equity - s.current_equity) / max(s.peak_equity, 1) * 100
        if dd >= c['max_drawdown_pct']:
            reason = f"⛔ Drawdown {dd:.1f}% — bot detenido permanentemente"
            self._trigger_circuit(reason)
            return False, reason

        # 3. Pérdidas consecutivas
        if s.consecutive_losses >= c['max_consecutive_losses']:
            reason = f"⛔ {s.consecutive_losses} pérdidas consecutivas — pausa diaria"
            self._trigger_circuit(reason + " (diario)")
            return False, reason

        # 4. Máx trades diarios EJECUTADOS
        if s.daily_trades >= c['max_daily_trades']:
            return False, f"⏸ Límite diario de {c['max_daily_trades']} trades ejecutados"

        return True, ""

    def _trigger_circuit(self, reason: str):
        self.state.circuit_open   = True
        self.state.circuit_reason = reason
        self._save()
        logger.warning(f"🔴 CIRCUIT BREAKER: {reason}")

    def reset_circuit_manual(self):
        self.state.circuit_open        = False
        self.state.circuit_reason      = ""
        self.state.consecutive_losses  = 0
        self._save()
        logger.info("✅ Circuit breaker reseteado manualmente")

    def calc_position_size(self, entry: float, sl: float,
                            tier: str, conviction: int) -> Optional[float]:
        can, reason = self.check_circuit()
        if not can:
            logger.warning(f"Sizing bloqueado: {reason}")
            return None

        c  = self.c
        eq = self.state.current_equity

        risk_map = {
            "SUPREMA": c['risk_pct_suprema'],
            "FUEL":    c['risk_pct_fuel'],
            "STD":     c['risk_pct_std'],
            # HUNT usa STD (conservador)
            "HUNT_LONG":  c['risk_pct_std'],
            "HUNT_SHORT": c['risk_pct_std'],
        }
        base_risk  = risk_map.get(tier, c['risk_pct_std'])
        conv_scale = 0.8 + 0.04 * min(conviction, 10)
        risk_pct   = min(base_risk * conv_scale, c['risk_pct_max'])
        risk_usdt  = eq * risk_pct / 100.0
        sl_dist    = abs(entry - sl)

        if sl_dist <= 0 or sl_dist / entry < 0.001:
            # SL demasiado cerca — usar ATR mínimo del 0.5%
            sl_dist = entry * 0.005
            logger.warning(f"SL demasiado cerca, usando 0.5% de entry")

        qty_usdt = risk_usdt / (sl_dist / entry)
        qty_base = qty_usdt / entry

        # No usar más del 40% del equity como margen
        leverage       = c['leverage']
        margin_req     = qty_usdt / leverage
        if margin_req > eq * 0.40:
            qty_usdt  = eq * 0.40 * leverage
            qty_base  = qty_usdt / entry

        if qty_base <= 0:
            logger.warning(f"Qty calculada = 0 (equity={eq:.2f} risk={risk_pct:.2f}% sl_dist={sl_dist:.6f})")
            return None

        logger.info(
            f"Sizing: equity={eq:.2f} tier={tier} risk={risk_pct:.2f}% "
            f"risk_usdt={risk_usdt:.2f} sl_dist={sl_dist:.6f} qty={qty_base:.6f} margin={margin_req:.2f}"
        )
        return round(qty_base, 6)

    def calc_tp(self, entry: float, sl: float, direction: str, tier: str) -> float:
        rr_map = {
            "SUPREMA": self.c['rr_suprema'],
            "FUEL":    self.c['rr_fuel'],
            "STD":     self.c['rr_std'],
            "HUNT_LONG":  self.c['rr_std'],
            "HUNT_SHORT": self.c['rr_std'],
        }
        rr   = rr_map.get(tier, self.c['rr_std'])
        dist = abs(entry - sl) * rr
        return (entry + dist) if direction == "LONG" else (entry - dist)

    def record_trade(self, pnl: float):
        """Llamar SOLO cuando se cierra una posición real"""
        s = self.state
        s.daily_pnl      += pnl
        s.daily_trades   += 1    # ← solo aquí se incrementa
        s.current_equity += pnl
        s.consecutive_losses = 0 if pnl > 0 else s.consecutive_losses + 1
        if s.current_equity > s.peak_equity:
            s.peak_equity = s.current_equity
        self._save()
        logger.info(
            f"Trade cerrado: PnL={pnl:+.2f} USDT | "
            f"Equity={s.current_equity:.2f} | "
            f"Daily={s.daily_pnl:+.2f} | "
            f"Trades hoy={s.daily_trades} | "
            f"Consec.losses={s.consecutive_losses}"
        )

    def update_equity(self, equity: float):
        """Sincronizar con balance real de BingX"""
        s = self.state
        if equity > 0:
            s.current_equity = equity
            if equity > s.peak_equity:
                s.peak_equity = equity
            self._save()

    def status_dict(self) -> dict:
        self._check_daily_reset()
        s  = self.state
        c  = self.c
        dd = (s.peak_equity - s.current_equity) / max(s.peak_equity, 1) * 100
        return {
            "equity":           round(s.current_equity, 2),
            "peak_equity":      round(s.peak_equity, 2),
            "drawdown_pct":     round(dd, 2),
            "daily_pnl":        round(s.daily_pnl, 2),
            "daily_trades":     s.daily_trades,
            "max_daily_trades": c['max_daily_trades'],
            "consec_losses":    s.consecutive_losses,
            "circuit_open":     s.circuit_open,
            "circuit_reason":   s.circuit_reason,
            "daily_loss_limit": f"{c['max_daily_loss_pct']}%",
            "max_dd_limit":     f"{c['max_drawdown_pct']}%",
        }
