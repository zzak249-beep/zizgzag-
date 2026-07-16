"""
backtest_pump_fade.py — Backtest walk-forward del PUMP FADE BOT.

Objetivo: juntar cientos de "trades" en minutos, sin esperar semanas de
DRY_RUN en vivo, para poder juzgar el setup con muestra real en vez de
intuición. No toca nada del bot en producción (journal.py, state_store.py,
main.py) — es un script aparte, de solo lectura contra la API pública.

Cómo funciona (sin lookahead, mismo criterio que main.py en vivo):
  1. Para cada símbolo del universo, se descarga tanta historia como la
     API permita (paginando hacia atrás con endTime; si BingX ignora ese
     parámetro, se degrada solo a un batch único sin romper).
  2. Se recorre la historia vela por vela: en cada paso i se llama
     pump_fade_engine.analyze(candles[:i+1], config) — EXACTAMENTE lo que
     hace main.py en cada ciclo con candles[:-1]. Nunca se le muestra al
     motor una vela futura.
  3. Cada vez que aparece signal == "SHORT", se abre un trade virtual con
     el entry/sl/tp que devolvió el motor, y se camina hacia adelante
     vela por vela buscando cuál se toca primero: si high >= sl y
     low <= tp en la MISMA vela, se cuenta como pérdida (mismo criterio
     conservador "ambigüedad = en contra del sistema" que usa el dashboard
     de Smart Breakout Targets — nunca a favor por casualidad).
  4. Se tabula además el estado devuelto en CADA paso (no solo cuando hay
     señal) -> el embudo: cuántas veces el motor se queda en "sin_techo",
     "techo_sin_choch", "esperando_retest", "retest_gastado",
     "invalidado", "bloqueada_chase", "sl_fuera_de_rango", "senal".

Uso:
    python3 backtest_pump_fade.py                  # universo automático
    python3 backtest_pump_fade.py SYM1-USDT SYM2-USDT   # símbolos puntuales

Universo automático: todos los símbolos USDT-perpetual con volumen 24h
actual >= config.MIN_24H_VOLUME_USDT (mismo piso de liquidez que el bot
real), sin exigir que estén "pumpeando" HOY -- el motor encuentra sus
propios techos locales dentro de la ventana rolling de DAY_BARS, así que
un símbolo que pumpeó hace 3 días y ya lo tenés en la historia sirve igual
para el backtest, aunque hoy esté plano.
"""
import asyncio
import json
import logging
import sys
import time
from collections import Counter

import config
import pump_fade_engine as eng
import scanner
from exchange_client import BingXClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s,%(msecs)03d | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("backtest")

# ── Parámetros del backtest (no confundir con config.py del bot real) ──
MAX_BARS_PER_SYMBOL = 6000     # ~20 días de velas 5m si la paginación funciona
BATCH_SIZE = 1000              # tamaño de página por request
MAX_SYMBOLS = 40                # tope de símbolos a bajar (cuidar rate limit)
OUT_FILE = "backtest_pumpfade_results.json"


async def fetch_full_history(client, symbol, interval, max_bars=MAX_BARS_PER_SYMBOL,
                              batch=BATCH_SIZE):
    """Pagina hacia atrás con endTime. Si el endpoint lo ignora (devuelve
    siempre el mismo tramo reciente), se detecta por falta de velas NUEVAS
    y se corta ahí sin reintentar al infinito -- degrada a lo que haya."""
    all_candles = {}
    end_time = None
    stalls = 0
    while len(all_candles) < max_bars and stalls < 2:
        params = {"symbol": symbol, "interval": interval, "limit": batch}
        if end_time is not None:
            params["endTime"] = end_time
        data = await client._request(
            "GET", "/openApi/swap/v3/quote/klines", params, signed=False
        )
        raw = data.get("data", []) if isinstance(data, dict) else []
        if not raw:
            break
        before = len(all_candles)
        oldest_ts = None
        for k in raw:
            try:
                ts = int(k["time"])
                all_candles[ts] = {
                    "open": float(k["open"]), "high": float(k["high"]),
                    "low": float(k["low"]), "close": float(k["close"]),
                    "volume": float(k["volume"]), "time": ts,
                }
                if oldest_ts is None or ts < oldest_ts:
                    oldest_ts = ts
            except (KeyError, ValueError, TypeError):
                continue
        gained = len(all_candles) - before
        if gained == 0 or oldest_ts is None:
            stalls += 1  # el server no está devolviendo velas nuevas -> cortar
        else:
            stalls = 0
        if end_time is not None and oldest_ts is not None and oldest_ts >= end_time:
            break  # no retrocedió -> endTime ignorado por el endpoint
        end_time = (oldest_ts - 1) if oldest_ts is not None else None
        await asyncio.sleep(client._min_request_interval)

    candles = sorted(all_candles.values(), key=lambda c: c["time"])
    return candles[-max_bars:] if len(candles) > max_bars else candles


def simulate_trade(candles, start_idx, entry, sl, tp):
    """Camina hacia adelante desde start_idx+1 buscando el primer toque de
    SL o TP. Ambigüedad en la misma vela (toca ambos) -> SL primero, mismo
    criterio conservador que el resto del stack (nunca a favor por azar).
    Devuelve (resultado, velas_hasta_salida) o (None, None) si la historia
    se acaba antes de resolver (trade todavía "abierto" al final de los
    datos -- no cuenta ni como win ni como loss)."""
    for j in range(start_idx + 1, len(candles)):
        c = candles[j]
        hit_sl = c["high"] >= sl
        hit_tp = c["low"] <= tp
        if hit_sl:
            return "loss", j - start_idx
        if hit_tp:
            return "win", j - start_idx
    return None, None


async def backtest_symbol(candles, symbol):
    """Walk-forward sobre un símbolo. Devuelve (trades, state_counter)."""
    trades = []
    state_counter = Counter()
    n = len(candles)
    min_bars = max(config.DAY_BARS // 2, config.JUMP_WIN + 10,
                   2 * config.STRUCT_PIVOT_LEN + 10)
    if n < min_bars + 5:
        return trades, state_counter

    i = min_bars
    while i < n:
        window = candles[: i + 1]
        res = eng.analyze(window, config)
        state_counter[res["state"]] += 1

        if res["signal"] == "SHORT":
            result, bars_held = simulate_trade(candles, i, res["entry"],
                                                 res["sl"], res["tp"])
            trades.append({
                "symbol": symbol,
                "entry_idx": i,
                "entry_time": candles[i]["time"],
                "entry": res["entry"], "sl": res["sl"], "tp": res["tp"],
                "sl_dist_pct": res["sl_dist_pct"],
                "sl_widened": res["sl_widened"],
                "retest_count": res["retest_count"],
                "jump_L": (res.get("jump") or {}).get("L_last"),
                "result": result,           # "win" | "loss" | None (sin resolver)
                "bars_held": bars_held,
            })
            # una vez abierto el trade virtual, saltar hasta su resolución
            # (o al final) antes de seguir buscando el próximo -- evita
            # contar señales superpuestas del mismo setup ya "en curso",
            # mismo criterio de main.py (un símbolo, una posición a la vez)
            if bars_held:
                i += bars_held
            else:
                break
        i += 1
    return trades, state_counter


async def pick_universe(client):
    all_syms = await client.get_all_symbols_with_volume()
    candidates = [
        s for s in all_syms
        if s["volume_24h_usdt"] >= config.MIN_24H_VOLUME_USDT
        and scanner._is_valid_symbol(s["symbol"], config.NON_CRYPTO_PREFIXES,
                                      config.REQUIRE_USDT_QUOTE)
    ]
    candidates.sort(key=lambda s: s["volume_24h_usdt"], reverse=True)
    picked = [s["symbol"] for s in candidates[:MAX_SYMBOLS]]
    log.info("Universo automático: %d símbolos (de %d candidatos con volumen >= %s)",
              len(picked), len(candidates), config.MIN_24H_VOLUME_USDT)
    return picked


async def run(symbols_arg):
    async with BingXClient(config.BINGX_API_KEY, config.BINGX_API_SECRET,
                            config.BINGX_BASE_URL, dry_run=True) as client:
        symbols = symbols_arg or await pick_universe(client)

        all_trades = []
        total_states = Counter()
        for idx, symbol in enumerate(symbols, 1):
            log.info("[%d/%d] %s — descargando historia...", idx, len(symbols), symbol)
            candles = await fetch_full_history(client, symbol, config.ENTRY_TF)
            if len(candles) < 100:
                log.warning("[%s] historia insuficiente (%d velas) — salteado",
                            symbol, len(candles))
                continue
            trades, states = await backtest_symbol(candles, symbol)
            all_trades.extend(trades)
            total_states.update(states)
            resolved = [t for t in trades if t["result"] is not None]
            wins = sum(1 for t in resolved if t["result"] == "win")
            log.info("[%s] %d velas | %d señales (%d resueltas, %d win) ",
                      symbol, len(candles), len(trades), len(resolved), wins)

        report(all_trades, total_states)
        with open(OUT_FILE, "w") as f:
            json.dump({"trades": all_trades,
                       "state_funnel": dict(total_states),
                       "generated_at_ms": int(time.time() * 1000),
                       "config_snapshot": {
                           "RR": config.RR, "MIN_SL_DIST_PCT": config.MIN_SL_DIST_PCT,
                           "MAX_SL_DIST_PCT": config.MAX_SL_DIST_PCT,
                           "JUMP_GUARD_MODE": config.JUMP_GUARD_MODE,
                           "JUMP_THRESH": config.JUMP_THRESH,
                           "PUMP_MAX_RETEST": config.PUMP_MAX_RETEST,
                       }}, f, indent=2, default=str)
        log.info("Resultados completos guardados en %s", OUT_FILE)


def report(trades, state_counter):
    resolved = [t for t in trades if t["result"] is not None]
    wins = [t for t in resolved if t["result"] == "win"]
    losses = [t for t in resolved if t["result"] == "loss"]
    n_resolved = len(resolved)
    win_rate = (len(wins) / n_resolved * 100) if n_resolved else 0.0
    breakeven_wr = 1 / (1 + config.RR) * 100

    print("\n" + "=" * 60)
    print("BACKTEST PUMP FADE — RESUMEN")
    print("=" * 60)
    print(f"Señales totales generadas : {len(trades)}")
    print(f"  resueltas (SL o TP)     : {n_resolved}")
    print(f"  sin resolver (historia "
          f"se acabó antes)          : {len(trades) - n_resolved}")
    print(f"Wins  : {len(wins)}")
    print(f"Losses: {len(losses)}")
    print(f"Win rate               : {win_rate:.1f}%")
    print(f"Win rate de breakeven a RR={config.RR}: {breakeven_wr:.1f}%")
    edge = win_rate - breakeven_wr
    print(f"Edge sobre breakeven    : {edge:+.1f} puntos "
          f"({'RENTABLE en la muestra' if edge > 0 else 'NO alcanza breakeven en la muestra'})")
    if resolved:
        avg_bars = sum(t["bars_held"] for t in resolved) / n_resolved
        print(f"Barras promedio hasta resolver: {avg_bars:.1f} "
              f"(~{avg_bars * 5:.0f} min en velas de 5m)")
        widened = sum(1 for t in resolved if t["sl_widened"])
        print(f"SL ensanchado al piso (MIN_SL_DIST_PCT): {widened}/{n_resolved} "
              f"({widened / n_resolved * 100:.0f}%)")

    print("\n--- Embudo (estado en CADA paso evaluado, no solo señales) ---")
    total_steps = sum(state_counter.values())
    for state, count in state_counter.most_common():
        pct = count / total_steps * 100 if total_steps else 0
        print(f"  {state:22s} {count:6d}  ({pct:5.1f}%)")
    print("=" * 60)
    print("Lectura del embudo: si 'bloqueada_chase' o 'sl_fuera_de_rango' "
          "acumulan un % alto sobre el total de 'esperando_retest' + "
          "'senal', esos son los filtros que más están recortando -- "
          "candidatos a revisar primero (JUMP_THRESH / MAX_SL_DIST_PCT).")


if __name__ == "__main__":
    syms = sys.argv[1:] or None
    asyncio.run(run(syms))
