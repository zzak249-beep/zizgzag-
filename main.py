"""
main.py — PUMP FADE BOT: short al agotamiento de los ganadores del día.

Loop:
1. Scan de ganadores 24h (scanner.get_top_gainers)
2. Por símbolo: velas 5m -> pump_fade_engine.analyze -> señal SHORT o no
3. Riesgo (RiskManager de la flota: breaker diario, racha, riesgo concurrente)
4. Ejecución con SL/TP verificados — SI EL SL NO SE COLOCA, LA POSICIÓN SE
   CIERRA A MERCADO AL INSTANTE. Jamás un short desnudo en un parabólico.
5. Monitoreo de cierres con PnL real (income API) -> journal + breaker
6. Estado persistido en /data (sobrevive redeploys... si el Volume está montado)
"""
import asyncio
import os
import logging
import sys
import time

import config
from exchange_client import BingXClient
from journal import TradeJournal
from risk_manager import RiskManager
from scanner import get_top_gainers
from state_store import StateStore
import pump_fade_engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s,%(msecs)03d | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("main")


async def _get_open_positions(client):
    """{symbol: {side, qty, entry}} de las posiciones abiertas en BingX."""
    data = await client._request(
        "GET", "/openApi/swap/v2/user/positions", signed=True
    )
    out = {}
    items = data.get("data", []) if isinstance(data, dict) else []
    for p in items or []:
        try:
            amt = float(p.get("positionAmt", 0))
            if amt == 0:
                continue
            out[p["symbol"]] = {
                "side": p.get("positionSide", "SHORT"),
                "qty": abs(amt),
                "entry": float(p.get("avgPrice", 0) or 0),
            }
        except (KeyError, ValueError, TypeError):
            continue
    return out


async def _realized_pnl_since(client, symbol, since_ms):
    """PnL realizado del símbolo desde since_ms (income API, con comisiones)."""
    total = 0.0
    try:
        data = await client._request(
            "GET", "/openApi/swap/v2/user/income",
            {"symbol": symbol, "startTime": since_ms, "limit": 200},
            signed=True,
        )
        for it in (data.get("data") or []):
            if it.get("incomeType") in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
                total += float(it.get("income", 0) or 0)
    except Exception as e:  # noqa: BLE001
        log.warning("[%s] No pude leer income: %s", symbol, e)
    return total


async def run():
    log.info(
        "Bot iniciado | CODE_VERSION=%s | DRY_RUN=%s | ENTRY_TF=%s | "
        "PUMP_MIN_24H_PCT=%s | MIN_VOL=%s | RR=%s | JUMP_GUARD=%s | "
        "MAX_RETEST=%s | riesgo/trade=%s%% | max_pos=%s",
        config.CODE_VERSION, config.DRY_RUN, config.ENTRY_TF,
        config.PUMP_MIN_24H_PCT, config.MIN_24H_VOLUME_USDT, config.RR,
        config.JUMP_GUARD_MODE, config.PUMP_MAX_RETEST,
        config.RISK_PCT_PER_TRADE, config.MAX_ACTIVE_POSITIONS,
    )
    if config.DRY_RUN:
        log.warning("DRY_RUN=True — modo observación: señales sin ejecutar. "
                    "Pasá DRY_RUN=False en Railway recién tras 3-5 días de "
                    "señales razonables en el journal.")
    if config.MIN_24H_VOLUME_USDT < 5_000_000:
        log.warning("⚠️ MIN_24H_VOLUME_USDT=%s (< 5M) — ¿te faltó un cero en "
                    "Railway? Con el piso bajo, los pares ilíquidos tipo LAB "
                    "(slippage 3x el riesgo en el stop) vuelven al radar.",
                    config.MIN_24H_VOLUME_USDT)
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        _probe = os.path.join(config.DATA_DIR, ".write_probe")
        with open(_probe, "w") as _fh:
            _fh.write("ok")
        os.remove(_probe)
    except OSError as _e:
        log.warning("⚠️ %s NO es escribible (%s) — el Volume de Railway no "
                    "está montado: journal y estado se borran en cada "
                    "redeploy. Settings -> Attach Volume -> mount path %s",
                    config.DATA_DIR, _e, config.DATA_DIR)
    if config.RR < 2.0:
        log.warning("⚠️ RR=%.2f (< 2.0) — env var pisando el default. A 1.5R "
                    "el breakeven sube de 33%% a 40%% de win rate.", config.RR)

    journal = TradeJournal(config.JOURNAL_FILE)
    risk = RiskManager(config)
    store = StateStore(config.STATE_FILE)

    tracked = {}          # symbol -> meta de posiciones abiertas por ESTE bot
    done_setups = set()   # setup_keys ya operados (dedupe)
    watch = {}            # symbol -> {first_ms, peak_gain}: radar persistente
                          # (el +24h cae tras el desplome; el setup sigue vivo)

    snap = store.load()
    if snap:
        tracked = snap.get("tracked", {}) or {}
        _ro = snap.get("recently_opened") or {}
        done_setups = set(_ro.keys() if isinstance(_ro, dict) else _ro)
        risk.restore(snap.get("risk_snapshot") or snap.get("risk") or {})
        watch.update(snap.get("corr_exposure") or {})
        log.info("Estado restaurado de %s | %d posiciones trackeadas | "
                 "%d setups en dedupe", config.STATE_FILE, len(tracked),
                 len(done_setups))
    else:
        log.info("Sin estado previo en %s — arranque en frío "
                 "(¿Volume montado en /data?)", config.STATE_FILE)

    # Construcción defensiva: si el exchange_client.py del repo es otra
    # versión (deploy mixto / editado por un agente), se filtran los kwargs
    # que esa versión no soporte en vez de crashear el arranque.
    import inspect
    # Nota v1.4: el cliente canónico de la flota NO tiene max_concurrency
    # (maneja el rate limit internamente con min_request_interval). El
    # parámetro se elimina; el guard de firma queda como seguro barato.
    _wanted = {"dry_run": config.DRY_RUN}
    try:
        _params = inspect.signature(BingXClient.__init__).parameters
        _kw = {k: v for k, v in _wanted.items() if k in _params}
        _dropped = set(_wanted) - set(_kw)
        if _dropped:
            log.warning("⚠️ exchange_client.py de OTRA versión en el repo "
                        "(no soporta: %s) — deploy mixto: subí el ZIP "
                        "completo para restaurar el árbol canónico",
                        ", ".join(sorted(_dropped)))
    except (TypeError, ValueError):
        _kw = {}
    async with BingXClient(
        config.BINGX_API_KEY, config.BINGX_API_SECRET,
        config.BINGX_BASE_URL, **_kw,
    ) as client:
        while True:
            try:
                await _cycle(client, journal, risk, store, tracked, done_setups, watch)
            except Exception:  # noqa: BLE001
                log.exception("Error en el ciclo — sigo en el próximo")
            await asyncio.sleep(config.SCAN_INTERVAL_S)


async def _cycle(client, journal, risk, store, tracked, done_setups, watch):
    balance = await client.get_balance_usdt()
    if not balance or balance <= 0:
        if config.DRY_RUN:
            # En observación no hace falta plata: se simula el balance para
            # que el journal calcule sizing y las señales no se pierdan.
            if not getattr(_cycle, "_warned_balance", False):
                log.warning(
                    "Balance real inválido (%s) — DRY_RUN activo: sigo con "
                    "balance simulado de %.2f USDT (DRY_RUN_BALANCE). Si la "
                    "cuenta debería tener fondos, revisá keys/transferencia.",
                    balance, config.DRY_RUN_BALANCE)
                _cycle._warned_balance = True
            balance = config.DRY_RUN_BALANCE
        else:
            log.warning("Balance inválido (%s) — ciclo salteado", balance)
            return

    # ── 1. Cierres ──
    # DRY_RUN: cierre SIMULADO contra velas reales (paper trading). Sin esto,
    # las señales secas ocupaban los slots para siempre: a la 3ra señal el
    # bot enmudecía y el journal jamás sabría si las señales GANAN.
    open_now = {}
    if config.DRY_RUN:
        for symbol in list(tracked.keys()):
            meta = tracked[symbol]
            kl = await client.get_klines(symbol, config.ENTRY_TF, limit=250)
            exit_price, result = None, None
            for cndl in kl[:-1]:  # solo velas cerradas
                if (cndl.get("time") or 0) < meta["opened_at_ms"]:
                    continue
                # convención conservadora: si SL y TP caben en la misma
                # vela, cuenta como SL (igual que el Smart Breakout)
                if cndl["high"] >= meta["sl"]:
                    exit_price, result = meta["sl"], "sl"
                    break
                if cndl["low"] <= meta["tp"]:
                    exit_price, result = meta["tp"], "tp"
                    break
            if exit_price is None:
                continue
            tracked.pop(symbol)
            pnl = (meta["entry"] - exit_price) * meta.get("qty", 0.0)
            risk.release_open_risk(meta.get("risk_pct", 0))
            risk.register_realized_pnl(pnl, balance)
            journal.record({
                "event": "position_closed", "symbol": symbol, "side": "SHORT",
                "simulated": True, "result": result,
                "pnl_usdt": round(pnl, 4), "exit": exit_price,
                "setup_key": meta.get("setup_key"),
                "gain_24h_pct": meta.get("gain_24h_pct"),
                "retest_count": meta.get("retest_count"),
                "jump": meta.get("jump"), "sl_widened": meta.get("sl_widened"),
                "held_min": round((time.time() * 1000 - meta["opened_at_ms"]) / 60000),
                "ts": int(time.time() * 1000),
            })
            log.info("[%s] PAPER cerrada en %s | PnL simulado=%.4f USDT",
                     symbol, result.upper(), pnl)
    else:
        open_now = await _get_open_positions(client)
    for symbol in list(tracked.keys()):
        if config.DRY_RUN:
            break
        if symbol in open_now:
            continue
        meta = tracked.pop(symbol)
        pnl = await _realized_pnl_since(client, symbol, meta["opened_at_ms"])
        risk.release_open_risk(meta.get("risk_pct", 0))
        risk.register_realized_pnl(pnl, balance)
        journal.record({
            "event": "position_closed", "symbol": symbol, "side": "SHORT",
            "pnl_usdt": round(pnl, 4), "setup_key": meta.get("setup_key"),
            "gain_24h_pct": meta.get("gain_24h_pct"),
            "retest_count": meta.get("retest_count"),
            "jump": meta.get("jump"), "sl_widened": meta.get("sl_widened"),
            "held_min": round((time.time() * 1000 - meta["opened_at_ms"]) / 60000),
            "ts": int(time.time() * 1000),
        })
        log.info("[%s] Posición cerrada | PnL=%.4f USDT", symbol, pnl)

    if risk.daily_loss_breached(balance):
        log.warning("Breaker diario activo — sin entradas nuevas hoy")
        store.save({k: int(time.time() * 1000) for k in done_setups}, {}, tracked, risk.snapshot(), watch)
        return

    # ── 2. Ganadores del día + radar persistente ──
    gainers, ticker_map = await get_top_gainers(client, config)
    now_ms = int(time.time() * 1000)
    ttl_ms = int(config.RADAR_TTL_H * 3600 * 1000)
    for g in gainers:
        w = watch.get(g["symbol"]) or {"first_ms": now_ms, "peak_gain": 0.0}
        w["peak_gain"] = max(w["peak_gain"], g["gain_24h_pct"])
        w["last_gain"] = g["gain_24h_pct"]
        watch[g["symbol"]] = w
    # símbolos que YA no cumplen el +25% pero pumpearon hace < TTL: se
    # siguen evaluando — el desplome post-techo es exactamente el setup
    vivos = {g["symbol"] for g in gainers}
    for sym in list(watch.keys()):
        if now_ms - watch[sym]["first_ms"] > ttl_ms:
            del watch[sym]
        elif sym not in vivos:
            info = ticker_map.get(sym)
            if info is None or info["volume_24h_usdt"] < config.MIN_24H_VOLUME_USDT:
                # la liquidez murió después del pump: fuera del radar
                # (lección LAB — el piso aplica SIEMPRE, también aquí)
                del watch[sym]
                continue
            watch[sym]["last_gain"] = info["gain_24h_pct"]
            gainers.append({"symbol": sym,
                            "gain_24h_pct": info["gain_24h_pct"],
                            "volume_24h_usdt": info["volume_24h_usdt"],
                            "last_price": info["last_price"],
                            "from_radar": True})

    # ── 3. Evaluar cada uno ──
    for g in gainers:
        symbol = g["symbol"]
        if symbol in tracked or symbol in open_now:
            continue  # nunca dos posiciones en el mismo símbolo

        candles = await client.get_klines(symbol, config.ENTRY_TF,
                                          limit=config.KLINES_LIMIT)
        if len(candles) < 50:
            continue
        # velas CERRADAS: descartar la vela en formación (regla [-2] de la flota)
        res = pump_fade_engine.analyze(candles[:-1], config)

        w = watch.get(symbol)
        if w is not None and w.get("state") != res["state"]:
            log.info("[%s] fase: %s -> %s | +%.0f%% 24h",
                     symbol, w.get("state", "?"), res["state"],
                     g["gain_24h_pct"])
            w["state"] = res["state"]
        if res["state"] in ("bloqueada_chase", "sl_fuera_de_rango"):
            log.info("[%s] %s | +%.0f%% 24h | retest#%s | jump=%s",
                     symbol, res["state"], g["gain_24h_pct"],
                     res.get("retest_count"),
                     (res.get("jump") or {}).get("L_last"))
        if res["signal"] != "SHORT":
            continue

        setup_key = f"{symbol}|pumpfade|{res['setup_key_suffix']}"
        if setup_key in done_setups:
            continue

        entry, sl, tp = res["entry"], res["sl"], res["tp"]
        risk_pct = config.RISK_PCT_PER_TRADE
        qty = risk.calc_position_size(balance, entry, sl)
        notional = qty * entry
        if notional < config.MIN_NOTIONAL_USDT:
            qty = config.MIN_NOTIONAL_USDT / entry
            implied_risk = qty * abs(entry - sl) / balance * 100
            if implied_risk > config.MIN_NOTIONAL_MAX_RISK_PCT:
                log.info("[%s] Descartada: min notional implicaría %.2f%% "
                         "de riesgo", symbol, implied_risk)
                continue
            risk_pct = implied_risk

        ok, reason = risk.can_open_new_position(balance, len(tracked), risk_pct)
        if not ok:
            log.warning("[%s] Señal descartada por risk manager: %s",
                        symbol, reason)
            continue

        specs = await client.get_contract_specs(symbol)
        if specs:
            qty = client.round_qty(qty, specs.get("quantityPrecision", 4))
            sl = client.round_price(sl, specs.get("pricePrecision", 6))
            tp = client.round_price(tp, specs.get("pricePrecision", 6))
        if qty <= 0:
            continue

        log.info("[%s] SEÑAL SHORT pump-fade | +%.0f%% 24h | entry=%.6f "
                 "sl=%.6f (%.2f%%%s) tp=%.6f | techo=%.6f nivel_roto=%.6f | "
                 "retest#%d | jump_L=%s",
                 symbol, g["gain_24h_pct"], entry, sl, res["sl_dist_pct"],
                 " ensanchado" if res["sl_widened"] else "", tp,
                 res["ceiling_high"], res["broken_level"],
                 res["retest_count"], (res.get("jump") or {}).get("L_last"))

        await client.set_leverage(symbol, config.LEVERAGE, side="SHORT")
        order = await client.open_position(symbol, "SHORT", qty,
                                           sl_price=sl, tp_price=tp)
        if order.get("code") != 0:
            log.error("[%s] Falló la apertura: %s", symbol, order)
            continue

        # ── SL obligatorio: sin SL colocado NO existe la posición ──
        if not order.get("sl_placed", False) and not config.DRY_RUN:
            log.error("[%s] ¡SL NO COLOCADO! Cerrando a mercado YA — "
                      "jamás un short desnudo en un parabólico", symbol)
            try:
                await client._request(
                    "POST", "/openApi/swap/v2/trade/order",
                    {"symbol": symbol, "side": "BUY", "positionSide": "SHORT",
                     "type": "MARKET", "quantity": qty}, signed=True)
                log.info("[%s] Cierre de emergencia enviado", symbol)
            except Exception as e:  # noqa: BLE001
                log.critical("[%s] CIERRE DE EMERGENCIA FALLÓ: %s — "
                             "CERRAR A MANO", symbol, e)
            continue

        done_setups.add(setup_key)
        tracked[symbol] = {
            "setup_key": setup_key, "risk_pct": risk_pct, "qty": qty,
            "opened_at_ms": int(time.time() * 1000),
            "entry": entry, "sl": sl, "tp": tp,
            "gain_24h_pct": round(g["gain_24h_pct"], 1),
            "retest_count": res["retest_count"],
            "jump": res.get("jump"), "sl_widened": res["sl_widened"],
        }
        risk.register_open_risk(risk_pct)
        journal.record({
            "event": "position_opened", "symbol": symbol, "side": "SHORT",
            "engine": "pump_fade", "entry": entry, "sl": sl, "tp": tp,
            "qty": qty, "risk_pct": risk_pct, "setup_key": setup_key,
            "gain_24h_pct": round(g["gain_24h_pct"], 1),
            "peak_gain_pct": round((watch.get(symbol) or {}).get("peak_gain", 0.0), 1),
            "ceiling_high": res["ceiling_high"],
            "broken_level": res["broken_level"],
            "retest_count": res["retest_count"],
            "sl_dist_pct": res["sl_dist_pct"], "sl_widened": res["sl_widened"],
            "jump": res.get("jump"), "dry_run": config.DRY_RUN,
            "ts": int(time.time() * 1000),
        })

    # dedupe acotado (los setups viejos ya no pueden repetirse igual)
    if len(done_setups) > 500:
        done_setups.clear()
        done_setups.update(m["setup_key"] for m in tracked.values())

    store.save({k: int(time.time() * 1000) for k in done_setups}, {}, tracked, risk.snapshot(), watch)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Detenido a mano")
