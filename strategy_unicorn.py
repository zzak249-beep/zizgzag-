"""
Unicorn Model Strategy — KIBITO
================================
Basado en el indicador Unicorn Model (SMC/ICT):
1. HTF Swing High/Low → niveles de liquidez (1H y 4H)
2. Sweep → precio barre el nivel con mecha
3. Breaker Block → 2+ velas opuestas tras el sweep
4. FVG Overlap → Fair Value Gap en el breaker (filtro Unicorn)
5. Confirmación → precio cierra a través del breaker → ENTRADA

LONG:  barrido de swing low → breaker alcista + FVG → close > breaker top
SHORT: barrido de swing high → breaker bajista + FVG → close < breaker bottom
"""
import logging

log = logging.getLogger("unicorn")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _atr(candles, period=14):
    trs = [max(c["high"] - c["low"],
               abs(c["high"] - candles[i-1]["close"]),
               abs(c["low"]  - candles[i-1]["close"]))
           for i, c in enumerate(candles) if i > 0]
    if not trs:
        return 0.0
    a = trs[0]
    for t in trs[1:]:
        a = t / period + a * (1 - 1 / period)
    return a


def _find_swing_highs(candles, pivot_len=5):
    """Pivots highs: máximo local con N velas a cada lado."""
    highs = []
    for i in range(pivot_len, len(candles) - pivot_len):
        h = candles[i]["high"]
        if all(candles[j]["high"] < h for j in range(i - pivot_len, i)) and \
           all(candles[j]["high"] < h for j in range(i + 1, i + pivot_len + 1)):
            highs.append((i, h))
    return highs


def _find_swing_lows(candles, pivot_len=5):
    """Pivot lows: mínimo local con N velas a cada lado."""
    lows = []
    for i in range(pivot_len, len(candles) - pivot_len):
        l = candles[i]["low"]
        if all(candles[j]["low"] > l for j in range(i - pivot_len, i)) and \
           all(candles[j]["low"] > l for j in range(i + 1, i + pivot_len + 1)):
            lows.append((i, l))
    return lows


def _find_sweep(candles, level, is_high, lookback=20):
    """
    Detecta si el precio barrió el nivel en las últimas N velas.
    Para is_high=True: mecha por encima del nivel pero cierra por debajo.
    Para is_high=False: mecha por debajo del nivel pero cierra por encima.
    Devuelve el índice de la vela de sweep o None.
    """
    check = candles[-lookback:]
    for i in range(len(check) - 1, -1, -1):
        c = check[i]
        if is_high:
            if c["high"] >= level and c["close"] < level:
                return len(candles) - lookback + i  # índice absoluto
        else:
            if c["low"] <= level and c["close"] > level:
                return len(candles) - lookback + i
    return None


def _find_breaker(candles, sweep_idx, direction, max_search=40):
    """
    Busca serie de 2+ velas en la dirección correcta tras el sweep.
    direction="BULL": busca velas alcistas (close > open)
    direction="BEAR": busca velas bajistas (close < open)
    Devuelve (start_idx, end_idx, top, bottom) o None.
    """
    search_start = sweep_idx + 1
    search_end   = min(search_start + max_search, len(candles) - 1)

    for i in range(search_start, search_end):
        c = candles[i]
        is_match = (c["close"] > c["open"]) if direction == "BULL" else (c["close"] < c["open"])
        if not is_match:
            continue
        # Busca extensión de la serie
        end = i
        for j in range(i + 1, min(i + 20, search_end)):
            nxt = candles[j]
            nxt_match = (nxt["close"] > nxt["open"]) if direction == "BULL" else (nxt["close"] < nxt["open"])
            if nxt_match:
                end = j
            else:
                break

        run = end - i + 1
        if run >= 2:
            top    = max(c["high"] for c in candles[i:end+1])
            bottom = min(c["low"]  for c in candles[i:end+1])
            return i, end, top, bottom

    return None


def _find_fvg(candles, breaker_top, breaker_bottom, direction):
    """
    Busca un Fair Value Gap que solape con el breaker.
    BULL FVG: low[i] > high[i+2] (gap alcista hacia arriba)
    BEAR FVG: high[i] < low[i+2] (gap bajista hacia abajo)
    Devuelve (fvg_top, fvg_bottom) o (None, None).
    """
    search = candles[-100:] if len(candles) > 100 else candles

    if direction == "BULL":
        for i in range(2, len(search)):
            fvg_top    = search[i]["low"]
            fvg_bottom = search[i-2]["high"]
            if fvg_top <= fvg_bottom:
                continue
            # FVG solapa con breaker?
            overlap_top    = min(fvg_top,    breaker_top)
            overlap_bottom = max(fvg_bottom, breaker_bottom)
            if overlap_top > overlap_bottom:
                # Verificar que no ha sido mitigado
                mitigated = any(c["low"] < fvg_bottom
                                for c in search[i:])
                if not mitigated:
                    return fvg_top, fvg_bottom
    else:
        for i in range(2, len(search)):
            fvg_top    = search[i-2]["low"]
            fvg_bottom = search[i]["high"]
            if fvg_bottom <= fvg_top:
                continue
            overlap_top    = min(fvg_top,    breaker_top)
            overlap_bottom = max(fvg_bottom, breaker_bottom)
            if overlap_top > overlap_bottom:
                mitigated = any(c["high"] > fvg_top
                                for c in search[i:])
                if not mitigated:
                    return fvg_top, fvg_bottom

    return None, None


# ── Main signal ───────────────────────────────────────────────────────────────

def get_signal(candles_5m: list, candles_1h: list, config) -> dict:
    """
    Returns dict:
      signal:      "LONG" | "SHORT" | None
      entry_price: float
      sl_price:    float
      tp_price:    float   (2R default)
      swept_level: float   (nivel de liquidez barrido)
      breaker_top: float
      breaker_bottom: float
      fvg_top:     float | None
      fvg_bottom:  float | None
      has_fvg:     bool
      atr:         float
    """
    result = {
        "signal": None, "entry_price": 0, "sl_price": 0, "tp_price": 0,
        "swept_level": 0, "breaker_top": 0, "breaker_bottom": 0,
        "fvg_top": None, "fvg_bottom": None, "has_fvg": False, "atr": 0,
    }

    pivot_len   = getattr(config, "UNICORN_PIVOT_LEN",   5)
    sweep_lb    = getattr(config, "UNICORN_SWEEP_LB",   30)   # velas 5m para buscar sweep
    unicorn_req = getattr(config, "UNICORN_REQUIRE_FVG", True) # requiere FVG
    rr          = getattr(config, "UNICORN_RR",          2.0)  # target R/R

    if len(candles_5m) < 80 or len(candles_1h) < 20:
        return result

    atr = _atr(candles_5m, 14)
    result["atr"] = atr

    # ── 1. Swing levels desde 1H ───────────────────────────────────────────────
    sh_1h = _find_swing_highs(candles_1h, pivot_len)
    sl_1h = _find_swing_lows(candles_1h,  pivot_len)

    # Usa los 3 más recientes de cada tipo
    recent_highs = [p for _, p in sh_1h[-3:]]
    recent_lows  = [p for _, p in sl_1h[-3:]]

    direction = getattr(config, "DIRECTION", "BOTH")

    # ── 2A. BULLISH — barrido de swing low ────────────────────────────────────
    if direction in ("LONG", "BOTH"):
        for level in sorted(recent_lows, reverse=True):  # del más cercano al más lejano
            sweep_idx = _find_sweep(candles_5m, level, is_high=False, lookback=sweep_lb)
            if sweep_idx is None:
                continue

            # 3. Breaker block — velas alcistas tras el sweep
            breaker = _find_breaker(candles_5m, sweep_idx, "BULL")
            if breaker is None:
                continue

            b_start, b_end, b_top, b_bottom = breaker

            # Validación: breaker bottom no puede estar por debajo del sweep extreme
            sweep_extreme = min(c["low"] for c in candles_5m[sweep_idx:sweep_idx+5])
            if b_bottom < sweep_extreme:
                continue

            # 4. FVG overlap
            fvg_top, fvg_bottom = _find_fvg(candles_5m, b_top, b_bottom, "BULL")
            has_fvg = fvg_top is not None

            if unicorn_req and not has_fvg:
                continue

            # 5. Confirmación — última vela cerrada cierra SOBRE el breaker top
            last_close = candles_5m[-2]["close"]
            if last_close <= b_top:
                continue

            # Señal confirmada
            entry = last_close
            sl    = b_bottom - atr * 0.3
            tp    = entry + rr * (entry - sl)

            result.update({
                "signal": "LONG",
                "entry_price": entry,
                "sl_price": sl,
                "tp_price": tp,
                "swept_level": level,
                "breaker_top": b_top,
                "breaker_bottom": b_bottom,
                "fvg_top": fvg_top,
                "fvg_bottom": fvg_bottom,
                "has_fvg": has_fvg,
            })
            return result

    # ── 2B. BEARISH — barrido de swing high ───────────────────────────────────
    if direction in ("SHORT", "BOTH"):
        for level in sorted(recent_highs):  # del más cercano (menor) al más alto
            sweep_idx = _find_sweep(candles_5m, level, is_high=True, lookback=sweep_lb)
            if sweep_idx is None:
                continue

            breaker = _find_breaker(candles_5m, sweep_idx, "BEAR")
            if breaker is None:
                continue

            b_start, b_end, b_top, b_bottom = breaker

            sweep_extreme = max(c["high"] for c in candles_5m[sweep_idx:sweep_idx+5])
            if b_top > sweep_extreme:
                continue

            fvg_top, fvg_bottom = _find_fvg(candles_5m, b_top, b_bottom, "BEAR")
            has_fvg = fvg_top is not None

            if unicorn_req and not has_fvg:
                continue

            last_close = candles_5m[-2]["close"]
            if last_close >= b_bottom:
                continue

            entry = last_close
            sl    = b_top + atr * 0.3
            tp    = entry - rr * (sl - entry)

            result.update({
                "signal": "SHORT",
                "entry_price": entry,
                "sl_price": sl,
                "tp_price": tp,
                "swept_level": level,
                "breaker_top": b_top,
                "breaker_bottom": b_bottom,
                "fvg_top": fvg_top,
                "fvg_bottom": fvg_bottom,
                "has_fvg": has_fvg,
            })
            return result

    return result


def check_tp_exit(candles: list, side: str, tp_price: float) -> bool:
    """Precio alcanzó el target (R/R proyectado)."""
    if not tp_price or not candles:
        return False
    last = candles[-2]["close"]
    if side == "LONG"  and last >= tp_price * 0.999:
        return True
    if side == "SHORT" and last <= tp_price * 1.001:
        return True
    return False
