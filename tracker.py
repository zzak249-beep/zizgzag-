"""Seguimiento de la señal vela a vela (mismo código en vivo y en backtest).

Conservador: si una vela toca stop y objetivo, cuenta el stop. El trailing se
actualiza al cierre de cada vela y actúa desde la siguiente.
"""


def new_trade(sig, cfg, symbol):
    e, a = sig["entry"], sig["atr"]
    d = -1 if sig["side"] == "SHORT" else 1
    risk = a * cfg.SL_ATR
    tr = {
        "symbol": symbol, "side": sig["side"], "dir": d, "t": sig["t"], "last_t": sig["t"],
        "entry": e, "atr": a, "risk": risk, "sl": e - d * risk, "stop": e - d * risk,
        "score": sig["score"], "remaining": 1.0, "r": 0.0, "bars": 0, "best": e,
        "be": False, "trail": False, "tp1_done": False, "tp2_done": False, "status": "open",
        "fee_r": 2 * cfg.FEE_PCT / 100 * e / risk if risk > 0 else 0.0,
    }
    if cfg.USE_MULTI_TP:
        tr["tp1"] = e + d * risk * cfg.TP1_R
        tr["tp2"] = e + d * risk * cfg.TP2_R
        tr["tp3"] = e + d * risk * cfg.TP3_R
    else:
        tr["tp"] = e + d * a * cfg.TP_ATR
    return tr


def _close_part(tr, frac, px):
    frac = min(frac, tr["remaining"])
    tr["r"] += frac * (px - tr["entry"]) * tr["dir"] / tr["risk"]
    tr["remaining"] -= frac
    return frac


def step(tr, bh, bl, bc, cfg):
    """Procesa una vela cerrada. Devuelve lista de eventos (tipo, precio)."""
    if tr["status"] != "open":
        return []
    ev = []
    d = tr["dir"]
    tr["bars"] += 1
    fav = bh if d == 1 else bl

    # 1) stop (incluye BE / trailing)
    hit_stop = (bl <= tr["stop"]) if d == 1 else (bh >= tr["stop"])
    if hit_stop:
        kind = "TRAIL" if tr["trail"] else ("BE" if tr["be"] else "SL")
        _close_part(tr, tr["remaining"], tr["stop"])
        tr["status"] = kind
        return [(kind, tr["stop"])]

    reach = (lambda p: bh >= p) if d == 1 else (lambda p: bl <= p)

    # 2) objetivos
    if cfg.USE_MULTI_TP:
        if not tr["tp1_done"] and reach(tr["tp1"]):
            _close_part(tr, cfg.Q1 / 100, tr["tp1"])
            tr["tp1_done"] = True
            ev.append(("TP1", tr["tp1"]))
            if cfg.BE_AFTER_TP1 and not tr["be"]:
                tr["be"] = True
                lock = tr["entry"] + d * tr["atr"] * cfg.BE_LOCK_ATR
                tr["stop"] = max(tr["stop"], lock) if d == 1 else min(tr["stop"], lock)
        if tr["tp1_done"] and not tr["tp2_done"] and cfg.Q2 > 0 and reach(tr["tp2"]):
            _close_part(tr, cfg.Q2 / 100, tr["tp2"])
            tr["tp2_done"] = True
            ev.append(("TP2", tr["tp2"]))
        if cfg.Q2 == 0 and tr["tp1_done"]:
            tr["tp2_done"] = tr["tp2_done"] or reach(tr["tp2"])
        if tr["remaining"] > 1e-9 and not cfg.USE_TRAILING and reach(tr["tp3"]):
            _close_part(tr, tr["remaining"], tr["tp3"])
            ev.append(("TP3", tr["tp3"]))
    else:
        if reach(tr["tp"]):
            _close_part(tr, tr["remaining"], tr["tp"])
            ev.append(("TP", tr["tp"]))

    if tr["remaining"] <= 1e-9:
        tr["status"] = ev[-1][0] if ev else "TP"
        return ev

    # 3) mejor precio, BE por distancia y trailing (para la siguiente vela)
    tr["best"] = max(tr["best"], fav) if d == 1 else min(tr["best"], fav)
    move = (tr["best"] - tr["entry"]) * d
    if cfg.USE_BE and not tr["be"] and move >= tr["atr"] * cfg.BE_TRIGGER_ATR:
        tr["be"] = True
        lock = tr["entry"] + d * tr["atr"] * cfg.BE_LOCK_ATR
        tr["stop"] = max(tr["stop"], lock) if d == 1 else min(tr["stop"], lock)
    if cfg.USE_TRAILING:
        if cfg.USE_MULTI_TP:
            active = tr["tp2_done"] or (tr["best"] - tr["tp2"]) * d >= 0
        else:
            active = move >= tr["atr"] * cfg.TRAIL_ACT_ATR
        if active:
            if not tr["trail"]:
                ev.append(("TRAIL_ON", tr["best"]))
            tr["trail"] = True
            ts = tr["best"] - d * tr["atr"] * cfg.TRAIL_OFF_ATR
            tr["stop"] = max(tr["stop"], ts) if d == 1 else min(tr["stop"], ts)

    # 4) time stop
    if cfg.MAX_BARS_IN_TRADE > 0 and tr["bars"] >= cfg.MAX_BARS_IN_TRADE:
        _close_part(tr, tr["remaining"], bc)
        tr["status"] = "TIME"
        ev.append(("TIME", bc))
    return ev


def net_r(tr):
    return tr["r"] - tr["fee_r"]
