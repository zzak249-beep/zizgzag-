"""
QF×JP Bot v6.4 — Scanner
Escanea todos los pares BingX en paralelo buscando señales QF×JP.
Incluye: OBI boost, funding rate, circuit-breaker blacklist, batches de 20.

CAMBIOS v6.4.1:
  - Cap de notional máximo (MAX_NOTIONAL_USDT=19800) antes de open_trade.
    Evita error 101209 "maximum position value for this leverage is 20000 USDT".
  - Balance fallback eliminado: si availableMargin < 5 USDT → skip símbolo.
    Antes caía a C.CAPITAL y el sizing calculaba como si hubiera margen libre,
    causando error 101204 "Insufficient margin" con muchas posiciones abiertas.
"""
import asyncio
import logging
import time
from typing import Optional

import config as C
from bingx_client import BingXClient
from indicators import analyze, Signal, composite_score, score_to_tier
from risk_manager import RiskManager
from position_manager import PositionManager, OpenTrade
import telegram_client as tg

log = logging.getLogger("scanner")

# Notional máximo por operación (BingX limita según par/leverage)
# Se usa 19800 para tener margen de seguridad bajo el límite de 20000 USDT
MAX_NOTIONAL_USDT = 19800.0

# Blacklist por circuit-breaker: symbol → timestamp del último CB
_cb_blacklist: dict[str, float] = {}
CB_COOLDOWN = 600   # 10 min fuera tras CB

# ── Fetch paralelo de klines + order book + funding ───────────────────────────

async def _fetch_klines_all(client: BingXClient, symbol: str):
    """
    Descarga klines de 4 TFs + order book + funding rate en paralelo.
    Devuelve (k3m, k15m, k1h, k4h, order_book, funding_rate).
    """
    results = await asyncio.gather(
        client.get_klines(symbol, C.TIMEFRAME,      200),
        client.get_klines(symbol, C.HTF_TIMEFRAME,  100),
        client.get_klines(symbol, C.HTF2_TIMEFRAME, 100),
        client.get_klines(symbol, C.HTF5_TIMEFRAME, 100),
        client.get_order_book(symbol, 10),
        client.get_funding_rate(symbol),
        return_exceptions=True,
    )
    def _lst(r): return r if isinstance(r, list) else []
    def _dct(r): return r if isinstance(r, dict) else {}
    def _flt(r): return r if isinstance(r, float) else 0.0

    return (
        _lst(results[0]),
        _lst(results[1]),
        _lst(results[2]),
        _lst(results[3]),
        _dct(results[4]),
        _flt(results[5]),
    )

# ── Order Book Imbalance ──────────────────────────────────────────────────────

def _calc_obi(order_book: dict) -> float:
    """
    Order Book Imbalance: (bid_vol - ask_vol) / (bid_vol + ask_vol)
    Rango: -1 (presión vendedora total) a +1 (presión compradora total).
    Usa top-5 niveles de cada lado.
    """
    try:
        bids    = order_book.get("bids", [])
        asks    = order_book.get("asks", [])
        bid_vol = sum(float(b[1]) for b in bids[:5] if len(b) >= 2)
        ask_vol = sum(float(a[1]) for a in asks[:5] if len(a) >= 2)
        total   = bid_vol + ask_vol
        return (bid_vol - ask_vol) / total if total > 0 else 0.0
    except Exception:
        return 0.0

# ── Procesar símbolo individual ───────────────────────────────────────────────

async def _process_symbol(
    symbol:  str,
    client:  BingXClient,
    risk:    RiskManager,
    pos_mgr: PositionManager,
) -> Optional[Signal]:
    """
    Analiza un símbolo y, si hay señal válida, notifica o ejecuta trade.
    Retorna Signal si hay señal, None en caso contrario.
    """
    # Saltar si ya tenemos posición abierta
    if pos_mgr.is_trading(symbol):
        return None

    # Saltar si está en cooldown de circuit-breaker
    now = time.time()
    if symbol in _cb_blacklist and now - _cb_blacklist[symbol] < CB_COOLDOWN:
        return None

    # Fetch datos
    try:
        k3m, k15m, k1h, k4h, ob, fr = await _fetch_klines_all(client, symbol)
    except Exception as e:
        log.debug("[%s] fetch error: %s", symbol, e)
        return None

    if len(k3m) < 60:
        return None

    # OBI
    obi = _calc_obi(ob)

    # Análisis
    try:
        sig = analyze(symbol, k3m, k15m, k1h, k4h, funding_rate=fr)
    except Exception as e:
        log.warning("[%s] analyze error: %s", symbol, e)
        return None

    if sig.direction == "NONE":
        return None

    # OBI boost: presión de libro confirma dirección → boost proporcional
    if abs(obi) > 0.1:
        boost = 0.0
        if sig.direction == "SHORT" and obi < -0.1:
            boost = abs(obi) * 5
        elif sig.direction == "LONG" and obi > 0.1:
            boost = obi * 5
        if boost > 0:
            sig.score = min(sig.score + boost, 100.0)
            sig.tier  = score_to_tier(sig.score)
            log.debug("[%s] OBI boost +%.1f → score=%.1f tier=%s",
                      symbol, boost, sig.score, sig.tier)

    # Circuit-breaker
    if sig.circuit_breaker:
        _cb_blacklist[symbol] = now
        await tg.notify_circuit_breaker(symbol)
        return None

    # Filtro de tier mínimo
    if not risk.tier_ok(sig.tier):
        return None

    log.info("[%s] Señal %s tier=%s score=%.1f fr=%.4f",
             symbol, sig.direction, sig.tier, sig.score, fr)

    # ── Modo SIGNAL ───────────────────────────────────────────────────────────
    if C.MODE == "SIGNAL":
        await tg.notify_signal(sig)
        return sig

    # ── Modo LIVE ─────────────────────────────────────────────────────────────
    can, reason = await risk.can_trade()
    if not can:
        log.info("[%s] Bloqueado por risk: %s", symbol, reason)
        return None

    # ── FIX v6.4.1: balance real sin fallback ─────────────────────────────────
    #
    # Antes: si availableMargin=0 (margen agotado por posiciones abiertas),
    # caía a C.CAPITAL y el sizing calculaba como si hubiera capital libre.
    # Resultado: 101209 (notional > límite) y 101204 (insufficient margin).
    #
    # Ahora: si el margen disponible real es < 5 USDT → skip símbolo.
    # No tiene sentido intentar abrir si no hay margen libre.
    # ─────────────────────────────────────────────────────────────────────────
    try:
        balance = await client.get_balance()
    except Exception as e:
        log.error("[%s] get_balance error: %s", symbol, e)
        return None

    if balance < 5.0:
        log.info(
            "[%s] Skip — margen disponible insuficiente (%.4f USDT < 5)",
            symbol, balance,
        )
        return None

    log.info("Balance activo: %.4f USDT", balance)

    qty = risk.kelly_position_size(balance, sig.entry, sig.sl, sig.score, sig.tier)
    if qty <= 0:
        log.warning("[%s] qty=0, skip", symbol)
        return None

    notional = qty * sig.entry

    # ── FIX v6.4.1: cap de notional máximo ───────────────────────────────────
    #
    # BingX limita el valor nocional máximo por posición (normalmente 20000 USDT
    # dependiendo del par y el apalancamiento). Si el sizing da un notional
    # superior, recalculamos qty para quedarnos en MAX_NOTIONAL_USDT=19800.
    # ─────────────────────────────────────────────────────────────────────────
    if notional > MAX_NOTIONAL_USDT:
        qty      = client._round_qty(symbol, MAX_NOTIONAL_USDT / sig.entry)
        notional = qty * sig.entry
        log.info(
            "[%s] Notional capeado → qty=%s notional=%.2f USDT (límite BingX)",
            symbol, qty, notional,
        )

    log.info("[%s] qty=%s notional=%.2f USDT (entry=%s)", symbol, qty, notional, sig.entry)

    await tg.notify_signal(sig)

    try:
        trade_results = await client.open_trade(
            symbol=symbol, direction=sig.direction, quantity=qty,
            sl_price=sig.sl, tp1_price=sig.tp1, tp2_price=sig.tp2,
        )
    except Exception as e:
        log.error("[%s] open_trade error: %s", symbol, e)
        await tg.notify_error(f"open_trade({symbol})", str(e))
        return None

    entry_resp = trade_results.get("entry", {})
    if entry_resp.get("code", -1) != 0:
        log.error("[%s] Entrada rechazada: %s", symbol, entry_resp)
        await tg.notify_error(f"entrada_rechazada({symbol})", str(entry_resp))
        return None

    order_id = str(
        entry_resp.get("data", {}).get("order", {}).get("orderId", "unknown")
        or entry_resp.get("data", {}).get("orderId", "unknown")
    )

    trade = OpenTrade(
        symbol=symbol, direction=sig.direction,
        entry=sig.entry, sl=sig.sl, tp1=sig.tp1, tp2=sig.tp2,
        qty=qty, atr=sig.atr, order_id=order_id,
    )
    await pos_mgr.register_trade(trade)
    await tg.notify_trade_opened(sig, qty, order_id)
    return sig

# ── Loop principal ────────────────────────────────────────────────────────────

async def scan_loop(client: BingXClient, risk: RiskManager, pos_mgr: PositionManager):
    log.info(
        "Scanner v6.4 iniciado | Modo=%s | Interval=%ds | TOP_N=%s | Batch=20",
        C.MODE, C.SCAN_INTERVAL,
        C.TOP_N_SYMBOLS if C.TOP_N_SYMBOLS > 0 else "TODAS",
    )

    symbols:   list[str] = []
    iteration: int       = 0

    while True:
        start      = time.time()
        iteration += 1

        # Refrescar lista de símbolos cada 10 ciclos
        if iteration == 1 or iteration % 10 == 0 or not symbols:
            try:
                new = await client.get_all_symbols()
                if new:
                    symbols = new
                    log.info("Símbolos activos: %d", len(symbols))
                else:
                    log.warning("get_all_symbols devolvió lista vacía (iter=%d)", iteration)
            except Exception as e:
                log.error("get_all_symbols error: %s", e)
                if not symbols:
                    await asyncio.sleep(30)
                    continue

        if not symbols:
            await asyncio.sleep(10)
            continue

        # Status periódico (cada 20 ciclos)
        if iteration % 20 == 0:
            try:
                balance = await client.get_balance()
                await tg.notify_status(risk.status(), balance, len(symbols))
            except Exception as e:
                log.warning("status notify error: %s", e)

        # Procesar en batches de 20 símbolos en paralelo
        BATCH         = 20
        signals_found = 0

        for i in range(0, len(symbols), BATCH):
            batch   = symbols[i : i + BATCH]
            results = await asyncio.gather(
                *[_process_symbol(s, client, risk, pos_mgr) for s in batch],
                return_exceptions=True,
            )
            for r in results:
                if isinstance(r, Signal) and r.direction != "NONE":
                    signals_found += 1
            await asyncio.sleep(0.2)

        elapsed = time.time() - start
        log.info(
            "Iter %d | %d símbolos | %d señales | %.1fs",
            iteration, len(symbols), signals_found, elapsed,
        )

        # Aviso de configuración si no hay señales en primeras iteraciones
        if signals_found == 0 and iteration <= 3:
            log.info(
                "Sin señales — revisa: REQUIRE_TL_BREAK=%s HTF_MIN_ALIGNED=%s MIN_SCORE=%.0f",
                C.REQUIRE_TL_BREAK, C.HTF_MIN_ALIGNED, C.MIN_SCORE,
            )

        # Esperar hasta el siguiente ciclo
        await asyncio.sleep(max(0.0, C.SCAN_INTERVAL - elapsed))
