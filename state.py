"""
Estado persistente en JSON, escritura atomica (tmp + rename). Igual que
en el resto de bots de este proyecto: monta un volumen de Railway en
STATE_FILE, o el bot "olvida" todo en cada redeploy.

`positions` es un DICT (symbol -> posicion) -- varios simbolos pueden
tener el patron abierto a la vez, hasta MAX_CONCURRENT_POSITIONS.

CIRCUIT BREAKER -- decision de diseno explicita: este bot NO trackea
P&L en dolares (solo cuenta W/L), a diferencia de los scripts de Pine
que si tienen equity real vía strategy.equity. Un "drawdown diario en %"
de verdad exigiria ese dato, que no existe aqui todavia (en MODE=SIGNAL
no hay equity real que trackear; en MODE=LIVE se podria consultar el
balance real, pero eso es un paso mas alla de lo que este circuit
breaker cubre por ahora). En vez de fingir un calculo de % con datos
que no hay, se adapta el ESPIRITU de la proteccion (frenar tras una
mala racha) a lo que el bot SI mide de forma honesta: racha de perdidas
consecutivas Y conteo de perdidas en el dia -- ambos derivados
directamente de win/loss real, sin inventar nada.
"""
import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone

import config as cfg

log = logging.getLogger("state")


class StateManager:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self.positions: dict = {}          # symbol -> {"dir":.., "entry":.., "sl":.., "tp":.., "opened_at":.., "tier":..}
        self.wins = 0
        self.losses = 0
        self.trades_today_date = None
        self.trades_today_count = 0
        self.active_days: set = set()
        self.last_pattern_keys: dict = {}  # symbol -> ultima clave de patron ya procesada
        # Circuit breaker
        self.consecutive_losses = 0
        self.daily_losses_date = None
        self.daily_losses_count = 0
        self.breaker_paused_until = None   # ISO date string, o None si no esta pausado
        # Tier tracking (major/altcoin)
        self.tier_stats: dict = {"major": {"w": 0, "l": 0}, "altcoin": {"w": 0, "l": 0}}
        # Respaldo diario
        self.last_backup_date = None
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            log.info("Sin estado previo en %s -- arrancando en blanco.", self.path)
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self.positions = raw.get("positions", {})
            self.wins = raw.get("wins", 0)
            self.losses = raw.get("losses", 0)
            self.trades_today_date = raw.get("trades_today_date")
            self.trades_today_count = raw.get("trades_today_count", 0)
            self.active_days = set(raw.get("active_days", []))
            self.last_pattern_keys = raw.get("last_pattern_keys", {})
            self.consecutive_losses = raw.get("consecutive_losses", 0)
            self.daily_losses_date = raw.get("daily_losses_date")
            self.daily_losses_count = raw.get("daily_losses_count", 0)
            self.breaker_paused_until = raw.get("breaker_paused_until")
            self.tier_stats = raw.get("tier_stats", {"major": {"w": 0, "l": 0}, "altcoin": {"w": 0, "l": 0}})
            self.last_backup_date = raw.get("last_backup_date")
            log.info("Estado cargado: %d posiciones abiertas, %dW/%dL.", len(self.positions), self.wins, self.losses)
        except Exception as e:
            log.error("Fallo cargando estado (%s) -- arrancando en blanco: %s", self.path, e)

    def save(self) -> None:
        with self._lock:
            data = {
                "positions": self.positions,
                "wins": self.wins,
                "losses": self.losses,
                "trades_today_date": self.trades_today_date,
                "trades_today_count": self.trades_today_count,
                "active_days": sorted(self.active_days),
                "last_pattern_keys": self.last_pattern_keys,
                "consecutive_losses": self.consecutive_losses,
                "daily_losses_date": self.daily_losses_date,
                "daily_losses_count": self.daily_losses_count,
                "breaker_paused_until": self.breaker_paused_until,
                "tier_stats": self.tier_stats,
                "last_backup_date": self.last_backup_date,
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
            d = os.path.dirname(self.path) or "."
            os.makedirs(d, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=d, prefix=".state_", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                os.replace(tmp_path, self.path)
            except Exception:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                raise

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _reset_daily_if_needed(self) -> None:
        today = self._today()
        if self.trades_today_date != today:
            self.trades_today_date = today
            self.trades_today_count = 0

    def _reset_daily_losses_if_needed(self) -> None:
        today = self._today()
        if self.daily_losses_date != today:
            self.daily_losses_date = today
            self.daily_losses_count = 0
            self.breaker_paused_until = None  # un dia nuevo limpia la pausa del dia anterior

    def under_daily_limit(self) -> bool:
        self._reset_daily_if_needed()
        return self.trades_today_count < cfg.MAX_TRADES_PER_DAY

    def under_concurrent_limit(self) -> bool:
        return len(self.positions) < cfg.MAX_CONCURRENT_POSITIONS

    def circuit_breaker_active(self) -> bool:
        """True si el circuit breaker esta frenando NUEVAS señales ahora
        mismo. Nunca toca posiciones YA abiertas -- esas se gestionan
        igual (papel o real) hasta su SL/TP normal."""
        if not cfg.USE_CIRCUIT_BREAKER:
            return False
        self._reset_daily_losses_if_needed()
        if self.consecutive_losses >= cfg.LOSS_STREAK_THRESHOLD:
            return True
        if self.daily_losses_count >= cfg.MAX_DAILY_LOSSES:
            return True
        return False

    def open_position(self, symbol: str, direction: str, entry: float, sl: float, tp: float, pattern_key: str, tier: str = "altcoin") -> None:
        self._reset_daily_if_needed()
        self.trades_today_count += 1
        self.active_days.add(self.trades_today_date)
        self.positions[symbol] = {
            "dir": direction, "entry": entry, "sl": sl, "tp": tp,
            "opened_at": datetime.now(timezone.utc).isoformat(), "tier": tier,
        }
        self.last_pattern_keys[symbol] = pattern_key

    def close_position(self, symbol: str, win: bool) -> None:
        pos = self.positions.pop(symbol, None)
        if win:
            self.wins += 1
            self.consecutive_losses = 0
        else:
            self.losses += 1
            self.consecutive_losses += 1
            self._reset_daily_losses_if_needed()
            self.daily_losses_count += 1

        tier = (pos or {}).get("tier", "altcoin")
        if tier not in self.tier_stats:
            self.tier_stats[tier] = {"w": 0, "l": 0}
        self.tier_stats[tier]["w" if win else "l"] += 1

    def needs_daily_backup(self) -> bool:
        return self.last_backup_date != self._today()

    def mark_backup_sent(self) -> None:
        self.last_backup_date = self._today()

    def backup_snapshot_json(self, include_positions: bool = True) -> str:
        """JSON del estado agregado, para copiar/pegar a mano si el
        volumen persistente se pierde. include_positions=False para el
        mensaje de Telegram (positions es lo unico sin tope predecible
        de tamaño; hasta MAX_CONCURRENT_POSITIONS es pequeño en este bot,
        pero se mantiene la misma cautela que en el scanner hermano)."""
        data = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "wins": self.wins,
            "losses": self.losses,
            "trades_today_date": self.trades_today_date,
            "trades_today_count": self.trades_today_count,
            "active_days": sorted(self.active_days),
            "consecutive_losses": self.consecutive_losses,
            "daily_losses_count": self.daily_losses_count,
            "tier_stats": self.tier_stats,
        }
        if include_positions:
            data["positions"] = self.positions
        return json.dumps(data, indent=2, ensure_ascii=False)
