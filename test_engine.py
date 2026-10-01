"""Test sintético: fabrica un pump + extensión fallida y verifica señal y seguimiento."""
import os
import random

os.environ.setdefault("MIN_RISK_PCT", "0")
import strategy  # noqa: E402
import tracker  # noqa: E402
from config import Config  # noqa: E402

TF = 15 * 60_000
DAY = 86_400_000


def build():
    random.seed(1)
    t0 = 1_780_000_000_000 // DAY * DAY - DAY  # medianoche UTC
    rows, px = [], 100.0
    # día anterior plano
    for k in range(96):
        o = px; c = px * (1 + random.uniform(-0.002, 0.002))
        rows.append([t0 + k * TF, o, max(o, c) * 1.002, min(o, c) * 0.998, c, 1000])
        px = c
    day0 = t0 + DAY
    px = rows[-1][4]
    open_day = px
    # subida progresiva +12% en 40 velas
    for k in range(40):
        o = px; c = px * 1.003
        rows.append([day0 + k * TF, o, c * 1.001, o * 0.999, c, 1000])
        px = c
    k = 40
    # vela de extensión: nuevo máximo grande con volumen alto
    o = px; hi = px * 1.04; c = px * 1.005
    rows.append([day0 + k * TF, o, hi, o * 0.999, c, 4000]); k += 1
    # vela de fallo: no supera, retrocede >56%, cierra dentro, mechas largas
    o = c; c2 = o * 0.995
    ext_base = max(r[2] for r in rows[-17:-1])
    lo = min(o * 0.97, ext_base * 0.999)
    rows.append([day0 + k * TF, o, o * 1.005, lo, min(c2, ext_base * 0.998), 2500]); k += 1
    px = rows[-1][4]
    # después: cae (para que el SHORT llegue a objetivo)
    for j in range(30):
        o = px; c = px * 0.996
        rows.append([day0 + k * TF, o, o * 1.001, c * 0.999, c, 1200]); k += 1; px = c
    cols = {key: [r[idx] for r in rows] for idx, key in enumerate("tohlcv")}
    btc = {"t": cols["t"], "o": [50000.0] * len(rows), "c": [50000.0] * len(rows)}
    return cols, btc, open_day


def main():
    cfg = Config()
    b, btc, od = build()
    sigs = strategy.run(b, strategy.btc_context(btc), cfg)
    print("señales:", [(s["side"], s["i"], s["ok"], s["why"], s["score"]) for s in sigs])
    assert sigs and sigs[-1]["side"] == "SHORT", "esperaba un SHORT"
    s = sigs[-1]
    tr = tracker.new_trade(s, cfg, "TEST-USDT")
    evs = []
    for j in range(s["i"] + 1, len(b["c"])):
        evs += tracker.step(tr, b["h"][j], b["l"][j], b["c"][j], cfg)
        if tr["status"] != "open":
            break
    print("eventos:", evs, "status", tr["status"], "R", round(tr["r"], 3), "neto", round(tracker.net_r(tr), 3))
    assert tr["status"] != "open" and tr["r"] > 0

    # multi TP
    os.environ["USE_MULTI_TP"] = "1"
    cfg2 = Config()
    tr = tracker.new_trade(s, cfg2, "TEST-USDT")
    evs = []
    for j in range(s["i"] + 1, len(b["c"])):
        evs += tracker.step(tr, b["h"][j], b["l"][j], b["c"][j], cfg2)
        if tr["status"] != "open":
            break
    print("multiTP:", evs, tr["status"], round(tr["r"], 3))

    # REQUIRE_CLOSE_INSIDE apagado / encendido no rompe; HTF y filtros activados no rompen
    for k, v in [("USE_PREMIUM", "1"), ("USE_ROC", "1"), ("USE_RSI_FILTER", "1"), ("USE_SESSION", "1"), ("MIN_SCORE", "50")]:
        os.environ[k] = v
    print("con filtros:", [(x["side"], x["ok"], x["why"]) for x in strategy.run(b, strategy.btc_context(btc), Config())])
    print("OK")


if __name__ == "__main__":
    main()
