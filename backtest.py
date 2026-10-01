"""Backtest con datos reales de BingX usando EXACTAMENTE el mismo motor y tracker.

  python backtest.py                     # top 60 pares, 30 días, config actual (env)
  python backtest.py --days 60 --top 100
  python backtest.py --set MIN_SCORE=60 --set USE_ROC=1
  python backtest.py --variants          # compara v7 original vs cada mejora
"""
import argparse
import asyncio
import os
import time

import strategy
import tracker
from bingx import BingX, to_cols
from config import TF_MIN, Config

VARIANTS = [
    ("v7 original (Pine)", {"REQUIRE_CLOSE_INSIDE": "0", "MIN_RISK_PCT": "0"}),
    ("+ cierre dentro", {"REQUIRE_CLOSE_INSIDE": "1", "MIN_RISK_PCT": "0"}),
    ("+ riesgo mín 0.3%", {"REQUIRE_CLOSE_INSIDE": "0", "MIN_RISK_PCT": "0.3"}),
    ("DEFAULT bot (ambas)", {}),
    ("default + score≥55", {"MIN_SCORE": "55"}),
    ("default + prima VWAP", {"USE_PREMIUM": "1"}),
    ("default + ROC", {"USE_ROC": "1"}),
    ("default + TP1/2/3", {"USE_MULTI_TP": "1"}),
    ("default sin trailing", {"USE_TRAILING": "0"}),
]


def simulate(data, btc_ctx, cfg):
    trades = []
    for sym, (b, htf) in data.items():
        sigs = [s for s in strategy.run(b, btc_ctx, cfg, htf) if s["ok"]]
        busy_until = -1
        for s in sigs:
            if s["i"] <= busy_until:
                continue
            tr = tracker.new_trade(s, cfg, sym)
            j = s["i"] + 1
            while j < len(b["c"]) and tr["status"] == "open":
                tracker.step(tr, b["h"][j], b["l"][j], b["c"][j], cfg)
                j += 1
            if tr["status"] == "open":
                break  # sin cerrar al final de los datos
            busy_until = j - 1
            trades.append(tr)
    return trades


def report(name, trades):
    if not trades:
        print(f"{name:<24} sin trades")
        return
    nr = [tracker.net_r(t) for t in trades]
    w = [x for x in nr if x > 0]
    ls = [x for x in nr if x <= 0]
    pf = sum(w) / abs(sum(ls)) if ls and sum(ls) != 0 else float("inf")
    eq = peak = dd = 0.0
    for x in sorted(trades, key=lambda t: t["t"]):
        eq += tracker.net_r(x)
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    sh = [tracker.net_r(t) for t in trades if t["side"] == "SHORT"]
    lg = [tracker.net_r(t) for t in trades if t["side"] == "LONG"]
    print(f"{name:<24} n={len(nr):<4} win={len(w) / len(nr) * 100:5.1f}%  avg={sum(nr) / len(nr):+.3f}R  "
          f"total={sum(nr):+7.2f}R  PF={pf:4.2f}  maxDD={dd:6.2f}R  "
          f"S {len(sh)}/{sum(sh):+.1f}R  L {len(lg)}/{sum(lg):+.1f}R")


async def load(cfg, top, days):
    end = int(time.time() * 1000)
    start = (end - days * 86_400_000) // 86_400_000 * 86_400_000
    async with BingX(cfg.CONCURRENCY) as bx:
        if cfg.SYMBOLS:
            syms = cfg.SYMBOLS
        else:
            tk = await bx.tickers()
            rows = sorted(((float(t.get("quoteVolume") or 0), t["symbol"]) for t in tk
                           if t.get("symbol", "").endswith("-USDT") and t["symbol"] != cfg.BTC_SYMBOL
                           and t["symbol"] not in cfg.BLACKLIST), reverse=True)
            syms = [s for _, s in rows[:top]]
        print(f"descargando {len(syms)} pares · {days} días · {cfg.TIMEFRAME}…")
        btc = to_cols(await bx.klines_range(cfg.BTC_SYMBOL, cfg.TIMEFRAME, start, end, cfg.TF_MS), end, cfg.TF_MS)
        htf_ms = TF_MIN.get(cfg.CTX_TF, 240) * 60_000

        async def one(s):
            try:
                b = to_cols(await bx.klines_range(s, cfg.TIMEFRAME, start, end, cfg.TF_MS), end, cfg.TF_MS)
                hc = to_cols(await bx.klines_range(s, cfg.CTX_TF, start - 120 * htf_ms, end, htf_ms), end, htf_ms)
                return s, b, hc
            except Exception as e:
                print("  ", s, e)
                return s, None, None

        res = await asyncio.gather(*(one(s) for s in syms))
    return btc, {s: (b, hc) for s, b, hc in res if b and len(b["c"]) > 100}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=60)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--variants", action="store_true")
    a = ap.parse_args()
    for kv in a.set:
        k, v = kv.split("=", 1)
        os.environ[k.strip()] = v.strip()
    base_env = dict(os.environ)

    cfg = Config()
    btc, raw = asyncio.run(load(cfg, a.top, a.days))
    btc_ctx = strategy.btc_context(btc)

    def run_cfg(name, over):
        os.environ.clear()
        os.environ.update(base_env)
        os.environ.update(over)
        c = Config()
        data = {s: (b, strategy.htf_context(h, c) if h and len(h["c"]) > c.CTX_EMA else None) for s, (b, h) in raw.items()}
        report(name, simulate(data, btc_ctx, c))

    print()
    if a.variants:
        for name, over in VARIANTS:
            run_cfg(name, over)
    else:
        run_cfg("config actual", {})


if __name__ == "__main__":
    main()
