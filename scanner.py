"""
Escanea TODOS los simbolos USDT-M de BingX cada SCAN_INTERVAL_SEC,
buscando el patron "tres montañas" (pattern.py) en velas de 1h. Misma
arquitectura de concurrencia controlada (semaforo) que bingx-ict-scanner,
adaptada a este patron especifico.
"""
import asyncio
import logging
import time

import config as cfg
from bingx_client import BingXClient, BingXError, normalize_symbol
from state import StateManager
from telegram_notifier import TelegramNotifier, format_backup
from pattern import detect_three_mountains, check_breakdown_confirmed, compute_atr
from executor import open_signal, check_paper_exit

log = logging.getLogger("scanner")

_symbol_cache: list = []
_symbol_cache_at: float = 0.0


def _is_tradable(contract: dict) -> bool:
    for key in ("status", "apiStateOpen", "state"):
        if key in contract:
            v = contract[key]
            if isinstance(v, bool):
                return v
            sv = str(v).upper()
            if sv in ("1", "TRADING", "ONLINE", "TRUE"):
                return True
            if sv in ("0", "OFFLINE", "FALSE", "DELISTED", "PAUSED"):
                return False
    return True  # campo desconocido -> no filtramos por si acaso


async def get_symbol_universe(client: BingXClient, force: bool = False) -> list:
    global _symbol_cache, _symbol_cache_at
    now = time.time()
    if not force and _symbol_cache and (now - _symbol_cache_at) < cfg.SYMBOL_REFRESH_MIN * 60:
        return _symbol_cache

    try:
        contracts = await client.get_contracts()
    except BingXError as e:
        log.error("No se pudo obtener la lista de contratos: %s", e)
        return _symbol_cache

    total = len(contracts)
    out = []
    excluded_tokenized = 0
    for c in contracts:
        sym = c.get("symbol")
        if not sym:
            continue
        norm = normalize_symbol(sym, cfg.QUOTE_ASSET)
        if not norm.endswith("-" + cfg.QUOTE_ASSET):
            continue
        if not _is_tradable(c):
            continue
        if cfg.SYMBOL_WHITELIST and norm not in cfg.SYMBOL_WHITELIST:
            continue
        if norm in cfg.SYMBOL_BLACKLIST:
            continue
        if cfg.EXCLUDE_TOKENIZED_ASSETS:
            base = norm.split("-")[0]
            if base.startswith(cfg._TOKENIZED_PREFIXES):
                excluded_tokenized += 1
                continue
        if cfg.MIN_24H_VOLUME_USDT > 0:
            vol = float(c.get("quoteVolume24h", c.get("volume24h", 0)) or 0)
            if vol < cfg.MIN_24H_VOLUME_USDT:
                continue
        out.append(sym)

    log.info(
        "Universo de simbolos: %d contratos -> %d tras filtros (blacklist=%d whitelist=%d tokenizados_excluidos=%d).",
        total, len(out), len(cfg.SYMBOL_BLACKLIST), len(cfg.SYMBOL_WHITELIST), excluded_tokenized,
    )
    if total > 0 and len(out) < total * 0.1:
        log.warning("Se filtro mas del 90%% del universo. Revisa MIN_24H_VOLUME_USDT / el campo de estado del contrato.")

    _symbol_cache = out
    _symbol_cache_at = now
    return out


def _pattern_key(pattern) -> str:
    return f"{pattern.peak3.open_time}"


def _ema_simple(closes: list, length: int) -> float:
    """EMA manual sobre una lista de cierres -- sin pandas/numpy, mismo
    criterio de dependencias minimas que el resto de bots del proyecto."""
    if len(closes) < length:
        return closes[-1] if closes else 0.0
    k = 2.0 / (length + 1)
    ema = sum(closes[:length]) / length
    for c in closes[length:]:
        ema = c * k + ema * (1 - k)
    return ema


def _classify_tier(symbol: str) -> str:
    return "major" if symbol in cfg.MAJOR_SYMBOLS else "altcoin"


async def _htf_bias_bearish(client: BingXClient, symbol: str) -> bool:
    """True si el sesgo de timeframe superior NO contradice un SHORT
    (precio por debajo de la EMA del HTF). Solo se llama para simbolos
    donde el patron YA confirmo la ruptura -- una llamada API extra por
    señal candidata, no por cada simbolo del universo en cada ciclo."""
    if not cfg.USE_HTF_BIAS:
        return True
    try:
        htf_candles = await client.get_klines(symbol, cfg.HTF_TIMEFRAME, cfg.HTF_EMA_LEN + 20)
    except BingXError:
        return True  # sin datos HTF -> no bloquea, mismo criterio permisivo que el resto del proyecto
    if len(htf_candles) < cfg.HTF_EMA_LEN:
        return True
    closes = [c.close for c in htf_candles]
    ema = _ema_simple(closes, cfg.HTF_EMA_LEN)
    return closes[-1] < ema


async def _evaluate_symbol(client: BingXClient, state: StateManager, notifier: TelegramNotifier, symbol: str) -> None:
    try:
        candles = await client.get_klines(symbol, cfg.TIMEFRAME, cfg.CANDLE_LIMIT)
    except BingXError:
        return  # simbolo con datos no disponibles -- se omite, no es un fallo del ciclo entero

    if len(candles) < cfg.PIVOT_LEN * 2 + 10:
        return

    last_close = candles[-1].close

    # ── Si ya hay posicion en este simbolo, solo revisar salida (papel) ──
    if symbol in state.positions:
        if cfg.MODE != "LIVE":
            await check_paper_exit(state, notifier, symbol, last_close)
        return

    if not state.under_daily_limit() or not state.under_concurrent_limit():
        return

    if state.circuit_breaker_active():
        return

    pattern = detect_three_mountains(
        candles,
        pivot_len=cfg.PIVOT_LEN,
        zone_tolerance_pct=cfg.ZONE_TOLERANCE_PCT,
        peak3_below_zone_pct_min=cfg.PEAK3_BELOW_ZONE_PCT_MIN,
        require_weak_push=cfg.REQUIRE_WEAK_PUSH,
        weak_push_max_ratio=cfg.WEAK_PUSH_MAX_RATIO,
    )
    if pattern is None:
        return

    key = _pattern_key(pattern)
    if key == state.last_pattern_keys.get(symbol):
        return

    if not check_breakdown_confirmed(candles, pattern):
        return

    if not await _htf_bias_bearish(client, symbol):
        state.last_pattern_keys[symbol] = key
        return

    atr = compute_atr(candles, cfg.ATR_LEN)
    entry = last_close
    sl = pattern.peak3.price + atr * cfg.SL_BUFFER_ATR_MULT
    r_dist = sl - entry
    if r_dist <= 0:
        state.last_pattern_keys[symbol] = key
        return

    tp = entry - r_dist * cfg.RR_RATIO
    rr = abs(tp - entry) / r_dist
    if rr < cfg.MIN_RR:
        state.last_pattern_keys[symbol] = key
        return

    tier = _classify_tier(symbol)
    log.info(
        "%s [%s]: SEÑAL SHORT confirmada. Peak1=%.6g Peak2=%.6g Peak3=%.6g (empuje ratio=%.2f) | Entry=%.6g SL=%.6g TP=%.6g R:R=%.2f",
        symbol, tier, pattern.peak1.price, pattern.peak2.price, pattern.peak3.price, pattern.vol_ratio,
        entry, sl, tp, rr,
    )
    await open_signal(client, state, notifier, symbol, "SHORT", entry, sl, tp, key, tier)


async def _send_daily_backup(state: StateManager, notifier: TelegramNotifier) -> None:
    """Envia el respaldo diario y marca como enviado SOLO si de verdad se
    entrego (send_direct confirma via HTTP real, no solo si se encolo).
    Si Telegram esta deshabilitado, se marca igual para no reintentar
    cada ciclo sin sentido -- misma logica que bingx-ict-scanner."""
    if not notifier.enabled:
        state.mark_backup_sent()
        state.save()
        return
    total = state.wins + state.losses
    wr = (state.wins / total * 100.0) if total > 0 else 0.0
    snapshot = state.backup_snapshot_json(include_positions=False)
    msg = format_backup(snapshot, state.wins, state.losses, wr)
    delivered = await notifier.send_direct(msg)
    if delivered:
        state.mark_backup_sent()
        state.save()
        log.info("Respaldo diario confirmado por Telegram (%d caracteres).", len(snapshot))
    else:
        log.warning("Respaldo diario NO confirmado, se reintenta el proximo ciclo.")


async def run_scan_cycle(client: BingXClient, state: StateManager, notifier: TelegramNotifier) -> int:
    t0 = time.time()
    symbols = await get_symbol_universe(client)
    if not symbols:
        log.warning("Universo de simbolos vacio -- nada que escanear este ciclo.")
        return 0

    sem = asyncio.Semaphore(cfg.MAX_CONCURRENT_FETCHES)

    async def _bounded(sym):
        async with sem:
            await _evaluate_symbol(client, state, notifier, sym)

    await asyncio.gather(*(_bounded(s) for s in symbols), return_exceptions=False)

    breaker_txt = " | CIRCUIT BREAKER ACTIVO" if state.circuit_breaker_active() else ""
    dt = time.time() - t0
    log.info(
        "Ciclo completo: %d simbolos, %d posiciones abiertas, %.1fs | %dW/%dL (racha=%d)%s",
        len(symbols), len(state.positions), dt, state.wins, state.losses, state.consecutive_losses, breaker_txt,
    )
    major = state.tier_stats.get("major", {"w": 0, "l": 0})
    alt = state.tier_stats.get("altcoin", {"w": 0, "l": 0})
    log.info("Por tier: major %dW/%dL | altcoin %dW/%dL", major["w"], major["l"], alt["w"], alt["l"])
    n_days = len(state.active_days)
    total_closed = state.wins + state.losses
    if n_days > 0:
        log.info("Muestra: %d cerradas en %d dias distintos, ~%.0f/dia", total_closed, n_days, total_closed / n_days)

    if state.needs_daily_backup():
        await _send_daily_backup(state, notifier)

    return len(symbols)
