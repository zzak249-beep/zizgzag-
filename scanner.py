"""
scanner.py — PUMP FADE: universo = los ganadores del día.

A diferencia del scanner de renewed-love (todo el universo líquido), acá el
universo son SOLO las monedas que subieron PUMP_MIN_24H_PCT o más en 24h,
con el piso de liquidez aplicado SIEMPRE (lección LAB-USDT: en un par
ilíquido el propio stop a mercado ejecuta con slippage de 3x el riesgo).
"""
import logging

log = logging.getLogger("scanner")


def _is_valid_symbol(symbol, non_crypto_prefixes, require_usdt_quote=True):
    if require_usdt_quote and not symbol.endswith("-USDT"):
        return False
    base = symbol.split("-")[0]
    return not any(base.startswith(p) for p in non_crypto_prefixes)


async def get_top_gainers(client, config):
    """
    Devuelve [{symbol, gain_24h_pct, volume_24h_usdt, last_price}, ...]
    ordenado por ganancia 24h descendente, ya filtrado por:
    - quote USDT y no-cripto fuera
    - volumen 24h >= MIN_24H_VOLUME_USDT (SIEMPRE)
    - PUMP_MIN_24H_PCT <= ganancia <= PUMP_MAX_24H_PCT
    - top TOP_GAINERS_N
    """
    data = await client._request("GET", "/openApi/swap/v2/quote/ticker", signed=False)
    items = data.get("data", []) if isinstance(data, dict) else []
    out = []
    for it in items:
        try:
            symbol = it["symbol"]
            if not _is_valid_symbol(symbol, config.NON_CRYPTO_PREFIXES,
                                    config.REQUIRE_USDT_QUOTE):
                continue
            vol = float(it.get("quoteVolume", 0) or 0)
            last = float(it.get("lastPrice", 0) or 0)
            # priceChangePercent viene como "12.34" (%) o a veces "0.1234";
            # BingX swap v2 lo da en % directo. Fallback: open->last.
            raw_chg = it.get("priceChangePercent")
            if raw_chg is not None and str(raw_chg).strip() != "":
                gain = float(raw_chg)
                # defensa por si algún día lo devuelven como fracción
                if abs(gain) <= 5 and it.get("openPrice"):
                    op = float(it["openPrice"])
                    if op > 0 and abs((last - op) / op * 100 - gain) > abs(gain) * 3:
                        gain = (last - op) / op * 100
            else:
                op = float(it.get("openPrice", 0) or 0)
                gain = (last - op) / op * 100 if op > 0 else 0.0
        except (KeyError, ValueError, TypeError):
            continue
        if vol < config.MIN_24H_VOLUME_USDT:
            continue
        out.append({"symbol": symbol, "gain_24h_pct": gain,
                    "volume_24h_usdt": vol, "last_price": last})

    # mapa completo (todos los validos, sin filtro de gain) para que el
    # radar persistente pueda chequear volumen/gain actual de sus simbolos
    ticker_map = {t["symbol"]: t for t in out}
    out2 = [t for t in out
            if config.PUMP_MIN_24H_PCT <= t["gain_24h_pct"] <= config.PUMP_MAX_24H_PCT]
    out2.sort(key=lambda x: x["gain_24h_pct"], reverse=True)
    top = out2[: config.TOP_GAINERS_N]
    if top:
        log.info(
            "Ganadores 24h en radar: %d | top 5: %s",
            len(top),
            ", ".join(f"{t['symbol']} +{t['gain_24h_pct']:.0f}%" for t in top[:5]),
        )
    return top, ticker_map
