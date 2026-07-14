"""
pump_fade_engine.py — Techo del día -> CHoCH bajista -> PRIMER retest = SHORT.

Secuencia exigida (las tres, en orden, sobre velas 5m cerradas):

1. TECHO CON RECHAZO: una vela hace el máximo de la ventana del día
   (DAY_BARS) y cierra en el tramo inferior de su propio rango
   (REJECT_MAX_CLOSE_POS) con rango >= CEILING_MIN_RANGE_ATR * ATR.
   Los compradores empujaron al high y los vendedores se lo comieron.

2. CHoCH BAJISTA: después del techo, un CIERRE por debajo del último
   swing low confirmado (STRUCT_PIVOT_LEN a cada lado). El nivel roto
   queda como resistencia.

3. PRIMER RETEST (Raschke): el precio vuelve a tocar el nivel roto desde
   abajo (high >= nivel - RETEST_TOUCH_ATR*ATR) y una vela cierra de
   nuevo por debajo (close < nivel - RETEST_BREAK_ATR*ATR). Solo el
   episodio #1..PUMP_MAX_RETEST vale: cada toque gasta la zona.

Invalidaciones (setup muerto, esperar uno nuevo):
- cierre por encima del techo
- cierre > nivel + RECLAIM_ATR*ATR (reclaim: el quiebre era falso)

La señal solo dispara si la vela de retest es LA ÚLTIMA CERRADA (frescura).
SL = máx(high desde el CHoCH, nivel) + SL_ATR_BUFFER*ATR, con piso
MIN_SL_DIST_PCT y techo MAX_SL_DIST_PCT (más de eso = parabólico
demasiado salvaje, se pasa). TP = RR fijo.

Todo stateless: se recalcula desde las velas en cada scan, cero memoria
entre ciclos (el patrón que nunca se corrompe con reinicios de Railway).
"""
import logging

import jump_detector

log = logging.getLogger("pump_fade_engine")


def _atr(candles, period=14):
    n = len(candles)
    if n < period + 1:
        return None
    trs = []
    for i in range(n - period, n):
        h, l = candles[i]["high"], candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)


def _swing_lows(candles, pl):
    """Índices de swing lows confirmados (pl velas a cada lado)."""
    out = []
    for i in range(pl, len(candles) - pl):
        lo = candles[i]["low"]
        if all(candles[j]["low"] >= lo for j in range(i - pl, i + pl + 1) if j != i):
            out.append(i)
    return out


def _find_ceiling(candles, atr, config):
    """Última vela-techo con rechazo dentro de CEILING_MAX_AGE_BARS."""
    n = len(candles)
    day_start = max(0, n - config.DAY_BARS)
    min_idx = max(day_start, n - config.CEILING_MAX_AGE_BARS)
    best = None
    for i in range(min_idx, n):
        c = candles[i]
        rng = c["high"] - c["low"]
        if rng <= 0 or rng < config.CEILING_MIN_RANGE_ATR * atr:
            continue
        # máximo del día HASTA esa vela inclusive
        if c["high"] < max(x["high"] for x in candles[day_start: i + 1]):
            continue
        close_pos = (c["close"] - c["low"]) / rng
        if close_pos <= config.REJECT_MAX_CLOSE_POS:
            best = i  # nos quedamos con el techo más reciente
    return best


def analyze(candles, config):
    """
    Devuelve dict:
      signal: "SHORT" | None
      entry, sl, tp (si signal)
      state: "sin_techo" | "techo_sin_choch" | "esperando_retest" |
             "retest_gastado" | "invalidado" | "senal" | "bloqueada_chase" |
             "sl_fuera_de_rango"
      ceiling_idx, ceiling_high, broken_level, choch_idx, retest_count,
      sl_widened, jump (dict del guard), setup_key_suffix
    """
    out = {"signal": None, "state": "sin_techo", "retest_count": 0,
           "ceiling_high": None, "broken_level": None, "sl_widened": False,
           "jump": None, "setup_key_suffix": None}
    n = len(candles)
    min_bars = max(config.DAY_BARS // 2, config.JUMP_WIN + 10,
                   2 * config.STRUCT_PIVOT_LEN + 10)
    if n < min_bars:
        out["state"] = f"datos_insuficientes ({n}/{min_bars})"
        return out

    atr = _atr(candles)
    if not atr or atr <= 0:
        out["state"] = "sin_atr"
        return out

    ceil_idx = _find_ceiling(candles, atr, config)
    if ceil_idx is None:
        return out
    ceiling_high = candles[ceil_idx]["high"]
    out.update(ceiling_idx=ceil_idx, ceiling_high=ceiling_high,
               state="techo_sin_choch")

    # ── CHoCH: primer cierre bajo el último swing low previo al quiebre ──
    lows = _swing_lows(candles, config.STRUCT_PIVOT_LEN)
    choch_idx = None
    broken_level = None
    for i in range(ceil_idx + 1, n):
        # swing lows confirmados hasta la vela i (confirmación = pl velas después)
        confirmed = [j for j in lows if j + config.STRUCT_PIVOT_LEN <= i]
        prior = [j for j in confirmed if j <= i]
        if not prior:
            continue
        level = candles[max(prior)]["low"]
        if candles[i]["close"] < level:
            choch_idx, broken_level = i, level
            break
    if choch_idx is None:
        return out
    out.update(choch_idx=choch_idx, broken_level=broken_level,
               state="esperando_retest",
               setup_key_suffix=f"{candles[choch_idx].get('time', choch_idx)}")

    # ── Post-CHoCH: invalidaciones y episodios de retest ──
    touch_lo = broken_level - config.RETEST_TOUCH_ATR * atr
    break_lv = broken_level - config.RETEST_BREAK_ATR * atr
    reclaim_lv = broken_level + config.RECLAIM_ATR * atr

    episodes = 0
    touching = False
    signal_idx = None
    for i in range(choch_idx + 1, n):
        c = candles[i]
        if c["close"] > ceiling_high or c["close"] > reclaim_lv:
            out["state"] = "invalidado"
            return out
        touch_now = c["high"] >= touch_lo
        if touch_now and not touching:
            episodes += 1
        # vela de rechazo: tocó (esta o venía tocando), cierra debajo Y es
        # bajista — una vela verde que roza el nivel es la aproximación,
        # no el rechazo (espejo del crossunder del superscript)
        if (touch_now or touching) and c["close"] < break_lv and c["close"] < c["open"]:
            if episodes <= config.PUMP_MAX_RETEST:
                signal_idx = i          # el último rechazo válido manda
            touching = False
        else:
            touching = touch_now
    out["retest_count"] = episodes

    if episodes == 0:
        return out
    if signal_idx is None or episodes > config.PUMP_MAX_RETEST and signal_idx != n - 1:
        out["state"] = "retest_gastado"
        return out
    # frescura: la señal es la ÚLTIMA vela cerrada, no una vieja
    if signal_idx != n - 1:
        out["state"] = "retest_gastado" if episodes >= config.PUMP_MAX_RETEST \
            else "esperando_retest"
        return out

    # ── Jump guard: no perseguir el latigazo bajista ──
    jump = jump_detector.confirms_direction(candles, "SHORT", config)
    out["jump"] = {k: jump.get(k) for k in
                   ("confirms", "L_last", "jump_recent", "jump_dir",
                    "jump_share", "reason", "mode")}
    if jump.get("confirms") is False:
        out["state"] = "bloqueada_chase"
        return out

    # ── E / SL / TP ──
    entry = candles[-1]["close"]
    local_high = max(c["high"] for c in candles[choch_idx:])
    sl_raw = max(local_high, broken_level) + config.SL_ATR_BUFFER * atr
    sl_dist_pct = (sl_raw - entry) / entry * 100 if entry > 0 else 0
    sl = sl_raw
    if sl_dist_pct < config.MIN_SL_DIST_PCT:
        sl = entry * (1 + config.MIN_SL_DIST_PCT / 100)
        out["sl_widened"] = True
        sl_dist_pct = config.MIN_SL_DIST_PCT
    if sl_dist_pct > config.MAX_SL_DIST_PCT:
        out["state"] = "sl_fuera_de_rango"
        out["sl_dist_pct"] = round(sl_dist_pct, 2)
        return out

    tp = entry - config.RR * (sl - entry)
    if tp <= 0:
        out["state"] = "tp_invalido"
        return out

    out.update(signal="SHORT", entry=entry, sl=sl, tp=tp,
               sl_dist_pct=round(sl_dist_pct, 2), state="senal")
    return out
