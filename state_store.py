"""
State Store — persistencia del estado runtime en Railway Volume
==================================================================
config.STATE_FILE existía desde el día uno pero nadie lo usaba: TODO el
estado anti-repetición y de riesgo vivía en RAM y cada redeploy de Railway
lo borraba (mismo bug "RAM-only counters" ya arreglado en otros bots de la
flota). Confirmado en el deploy 2026-07-09 08:47: posiciones abiertas antes
del redeploy quedaban huérfanas en position_monitor, open_risk_pct y la
exposición correlacionada arrancaban en cero, el circuit breaker diario se
reseteaba y los cooldowns dedup/post-close se perdían.

Qué persiste:
  - recently_opened / recently_closed (cooldowns dedup y post-cierre)
  - pos_monitor.tracked (metadata de posiciones abiertas: setup_key,
    risk_pct, opened_at_ms, side, SL/TP y flags de auto-repair)
  - risk_mgr (daily_pnl, daily_start_balance, current_day, open_risk_pct)
  - corr_mgr.open_exposure (exposición correlacionada a BTC)

Mismo patrón de escritura atómica que journal.py / setup_memory.py.
"""
import json
import logging
import os
import threading
import time

log = logging.getLogger("state_store")

# Entradas de cooldown más viejas que esto se descartan al guardar — ningún
# cooldown configurado se acerca a 24h, y así el archivo no crece sin límite
# con 519 símbolos rotando.
_PRUNE_MS = 24 * 3600 * 1000


class StateStore:
    def __init__(self, filepath):
        self.filepath = filepath
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(filepath), exist_ok=True)

    def load(self):
        """Devuelve el snapshot guardado, o {} si no hay/está corrupto."""
        try:
            with open(self.filepath, "r") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save(self, recently_opened, recently_closed, tracked, risk_snapshot, corr_exposure):
        now_ms = int(time.time() * 1000)

        def _prune(entries):
            # FIX (Claude, 2026-07-15): main.py llama a save() pasando
            # done_setups como list(set(...)) -- claves de dedupe SIN
            # timestamp por elemento, no un dict {symbol: timestamp} como
            # este método esperaba originalmente. Sin este chequeo,
            # entries.items() explota con "'list' object has no attribute
            # 'items'" en TODO ciclo que llega a guardar estado (confirmado
            # en producción, primer ciclo tras arreglar las credenciales).
            # Si es dict: podar por antigüedad como antes. Si es lista:
            # guardar tal cual -- main.py ya acota done_setups a mano
            # (lo limpia cuando supera 500 entradas), así que no necesita
            # poda por tiempo acá.
            if isinstance(entries, dict):
                return {s: t for s, t in entries.items()
                         if now_ms - t < _PRUNE_MS}
            return list(entries)

        snapshot = {
            "saved_at_ms": now_ms,
            "recently_opened": _prune(recently_opened),
            "recently_closed": _prune(recently_closed),
            "tracked": tracked,
            "risk": risk_snapshot,
            "corr_exposure": corr_exposure,
        }
        with self._lock:
            tmp = self.filepath + ".tmp"
            try:
                with open(tmp, "w") as f:
                    json.dump(snapshot, f, indent=2, default=str)
                os.replace(tmp, self.filepath)
            except OSError as e:
                # No tirar el bot por un fallo de disco — se reintenta en el
                # próximo save (hay uno por ciclo como mínimo).
                log.warning("No se pudo guardar el estado en %s: %s", self.filepath, e)
