"""FED + WER v7 — escáner de señales BingX → Telegram (Railway worker)."""
import asyncio
import json
import logging
import math
import os
import time
from datetime import datetime, timezone

import aiohttp

import strategy
import tracker
from bingx import BingX, to_cols
from config import CODE_VERSION, TF_MIN, Config
from telegram import Telegram

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

DAY_MS = 86_400_000
EMO = {"TP": "✅", "TP1": "✅", "TP2": "✅", "TP3": "🏁", "SL": "❌", "BE": "⚪", "TRAIL": "🟢", "TIME": "⏱", "TRAIL_ON": "🔒"}
NAMES = {"TP": "TP alcanzado", "TP1": "TP1 alcanzado", "TP2": "TP2 alcanzado", "TP3": "TP3 alcanzado", "SL": "Stop loss",
         "BE": "Cerrada en breakeven", "TRAIL": "Cerrada por trailing", "TIME": "Time stop", "TRAIL_ON": "Trailing activado"}


# ───────────── estado persistente ─────────────
class State:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.d = {"active": {}, "closed": [], "sent": {}, "day": None, "day_signals": 0,
                  "consec_losses": 0, "pause_until": 0}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    self.d.update(json.load(f))
            except Exception as e:
                log.error("estado corrupto, empiezo limpio: %s", e)

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.d, f)
        os.replace(tmp, self.path)


# ───────────── formato ─────────────
def fmt(px, prec):
    return f"{px:.{prec}f}" if prec is not None else f"{px:.6g}"


def link(sym):
    return f"https://bingx.com/es-es/perpetual/{sym}/"


def signal_msg(cfg, sym, sig, tr, prec):
    short = sig["side"] == "SHORT"
    e = tr["entry"]
    pct = lambda p: (p - e) / e * 100
    head = f"{'🔴 SHORT' if short else '🟢 LONG'} · <b>{sym}</b> · {cfg.TIMEFRAME}"
    lines = [head, f"<i>{cfg.BOT_NAME} · fade {'techo' if short else 'suelo'} · score {sig['score']:.0f}/100</i>", "",
             f"Entrada: <code>{fmt(e, prec)}</code>",
             f"SL: <code>{fmt(tr['sl'], prec)}</code> ({pct(tr['sl']):+.2f}%)"]
    if cfg.USE_MULTI_TP:
        lines += [f"TP1: <code>{fmt(tr['tp1'], prec)}</code> ({pct(tr['tp1']):+.2f}%) · cerrar {cfg.Q1}%",
                  f"TP2: <code>{fmt(tr['tp2'], prec)}</code> ({pct(tr['tp2']):+.2f}%) · cerrar {cfg.Q2}%"]
        if cfg.USE_TRAILING:
            lines.append(f"Resto: trailing desde TP2, distancia {fmt(tr['atr'] * cfg.TRAIL_OFF_ATR, prec)}")
        else:
            lines.append(f"TP3: <code>{fmt(tr['tp3'], prec)}</code> ({pct(tr['tp3']):+.2f}%) · resto")
        if cfg.BE_AFTER_TP1:
            lines.append("Tras TP1 → SL a breakeven")
    else:
        lines.append(f"TP: <code>{fmt(tr['tp'], prec)}</code> ({pct(tr['tp']):+.2f}%)")
        if cfg.USE_TRAILING:
            lines.append(f"Trailing: activa a {fmt(e + tr['dir'] * tr['atr'] * cfg.TRAIL_ACT_ATR, prec)}, "
                         f"distancia {fmt(tr['atr'] * cfg.TRAIL_OFF_ATR, prec)}")
    rr = (abs(tr.get("tp", tr.get("tp2", e)) - e)) / tr["risk"] if tr["risk"] else 0
    lines += ["",
              f"Día {sig['coin_pct']:+.1f}% · RS {sig['rs']:+.1f} · WER {sig['wer']:.2f}",
              f"Retroceso {sig['retrace']:.0f}% · RVOL ext {sig['ext_rvol']:.1f}x · Prima VWAP {sig['premium']:+.2f}%",
              f"R:R {rr:.2f} · comisión ≈ {tr['fee_r']:.2f}R",
              f'<a href="{link(sym)}">Abrir en BingX</a>']
    return "\n".join(lines)


def event_msg(cfg, tr, kind, px, prec):
    head = f"{EMO.get(kind, '•')} <b>{tr['symbol']}</b> {tr['side']} — {NAMES.get(kind, kind)} @ <code>{fmt(px, prec)}</code>"
    if tr["status"] != "open":
        return head + f"\nResultado: <b>{tr['r']:+.2f}R</b> (neto {tracker.net_r(tr):+.2f}R) · {tr['bars']} velas"
    if kind == "TP1" and tr["be"]:
        return head + f"\n{tr['r']:+.2f}R asegurado · SL movido a <code>{fmt(tr['stop'], prec)}</code>"
    if kind == "TRAIL_ON":
        return head + f"\nStop ahora en <code>{fmt(tr['stop'], prec)}</code>"
    return head + f"\nAcumulado {tr['r']:+.2f}R · stop <code>{fmt(tr['stop'], prec)}</code>"


# ───────────── bot ─────────────
class Bot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state = State(os.path.join(cfg.STATE_DIR, "state.json"))
        self.tg = Telegram(cfg.TG_TOKEN, cfg.TG_CHAT)
        self.contracts = {}
        self.universe = []
        self.universe_ts = 0
        tfm = TF_MIN[cfg.TIMEFRAME]
        warm = max(cfg.EXT_LOOKBACK, cfg.ATR_LEN, 21) + 60
        self.limit = min(1440, math.ceil(1440 / tfm) + warm)

    def prec(self, sym):
        p = self.contracts.get(sym, {}).get("pricePrecision")
        return int(p) if p is not None else None

    async def refresh_universe(self, bx):
        self.contracts = await bx.contracts()
        if self.cfg.SYMBOLS:
            self.universe = [s for s in self.cfg.SYMBOLS if s != self.cfg.BTC_SYMBOL]
        else:
            rows = []
            for tk in await bx.tickers():
                s = tk.get("symbol", "")
                if not s.endswith("-USDT") or s == self.cfg.BTC_SYMBOL or s in self.cfg.BLACKLIST:
                    continue
                ct = self.contracts.get(s)
                if ct is not None and str(ct.get("status", 1)) not in ("1", "True", "true"):
                    continue
                qv = float(tk.get("quoteVolume") or 0) or float(tk.get("volume") or 0) * float(tk.get("lastPrice") or 0)
                if qv >= self.cfg.MIN_QUOTE_VOL_24H:
                    rows.append((qv, s))
            rows.sort(reverse=True)
            self.universe = [s for _, s in rows[: self.cfg.TOP_N]]
        self.universe_ts = time.time()
        log.info("universo: %d símbolos", len(self.universe))

    async def fetch(self, bx, sym, now_ms):
        try:
            rows = await bx.klines(sym, self.cfg.TIMEFRAME, self.limit)
            b = to_cols(rows, now_ms, self.cfg.TF_MS)
            htf = None
            if self.cfg.need_htf:
                hrows = await bx.klines(sym, self.cfg.CTX_TF, self.cfg.CTX_EMA + self.cfg.PD_LEN + 60)
                htf_ms = TF_MIN.get(self.cfg.CTX_TF, 240) * 60_000
                hc = to_cols(hrows, now_ms, htf_ms)
                if len(hc["c"]) > self.cfg.CTX_EMA:
                    htf = strategy.htf_context(hc, self.cfg)
            return sym, b, htf
        except Exception as e:
            log.warning("%s: %s", sym, e)
            return sym, None, None

    async def scan(self, bx, http):
        cfg, st = self.cfg, self.state.d
        now_ms = int(time.time() * 1000)
        if time.time() - self.universe_ts > 3600:
            await self.refresh_universe(bx)

        btc = to_cols(await bx.klines(cfg.BTC_SYMBOL, cfg.TIMEFRAME, self.limit), now_ms, cfg.TF_MS)
        btc_ctx = strategy.btc_context(btc)
        syms = list(dict.fromkeys(self.universe + list(st["active"].keys())))
        results = await asyncio.gather(*(self.fetch(bx, s, now_ms) for s in syms))

        today = now_ms // DAY_MS
        if st["day"] != today:
            if st["day"] is not None:
                await self.daily_summary(http, st["day"])
            st["day"], st["day_signals"] = today, 0

        new = []
        for sym, b, htf in results:
            if not b or len(b["c"]) < cfg.EXT_LOOKBACK + 30:
                continue
            prec = self.prec(sym)
            # 1) seguimiento de la señal abierta
            tr = st["active"].get(sym)
            if tr:
                for i, tt in enumerate(b["t"]):
                    if tt <= tr["last_t"]:
                        continue
                    for kind, px in tracker.step(tr, b["h"][i], b["l"][i], b["c"][i], cfg):
                        await self.tg.send(http, event_msg(cfg, tr, kind, px, prec))
                    tr["last_t"] = tt
                    if tr["status"] != "open":
                        self.on_close(tr)
                        break
            # 2) señal nueva en la última vela cerrada
            sigs = strategy.run(b, btc_ctx, cfg, htf)
            if not sigs or sigs[-1]["t"] != b["t"][-1]:
                continue
            sig = sigs[-1]
            if st["sent"].get(sym) == sig["t"]:
                continue
            st["sent"][sym] = sig["t"]
            if not sig["ok"]:
                log.info("%s %s bloqueada: %s", sym, sig["side"], ",".join(sig["why"]))
                if cfg.SEND_BLOCKED:
                    await self.tg.send(http, f"⚠️ <b>{sym}</b> {sig['side']} detectada pero bloqueada ({', '.join(sig['why'])}) · score {sig['score']:.0f}")
                continue
            if sym in st["active"]:
                log.info("%s señal ignorada: ya hay una abierta", sym)
                continue
            new.append((sym, sig))

        # 3) límites globales (mejor score primero)
        new.sort(key=lambda x: -x[1]["score"])
        for sym, sig in new:
            if now_ms < st["pause_until"]:
                log.info("pausa por racha: %s ignorada", sym)
                continue
            if cfg.MAX_SIGNALS_DAY and st["day_signals"] >= cfg.MAX_SIGNALS_DAY:
                log.info("máx señales/día: %s ignorada", sym)
                continue
            if cfg.MAX_OPEN_SIGNALS and len(st["active"]) >= cfg.MAX_OPEN_SIGNALS:
                log.info("máx abiertas: %s ignorada", sym)
                continue
            tr = tracker.new_trade(sig, cfg, sym)
            st["active"][sym] = tr
            st["day_signals"] += 1
            log.info("SEÑAL %s %s @ %s score %.0f", sym, sig["side"], sig["entry"], sig["score"])
            await self.tg.send(http, signal_msg(cfg, sym, sig, tr, self.prec(sym)))

        self.state.save()
        log.info("scan ok · %d símbolos · %d abiertas · %d nuevas", len(syms), len(st["active"]), len(new))

    def on_close(self, tr):
        st = self.state.d
        st["active"].pop(tr["symbol"], None)
        tr["closed_ms"] = int(time.time() * 1000)
        st["closed"] = (st["closed"] + [tr])[-1000:]
        if tracker.net_r(tr) < 0:
            st["consec_losses"] += 1
            if self.cfg.PAUSE_AFTER_LOSSES and st["consec_losses"] >= self.cfg.PAUSE_AFTER_LOSSES:
                st["pause_until"] = int(time.time() * 1000 + self.cfg.PAUSE_HOURS * 3_600_000)
                st["consec_losses"] = 0
                log.warning("pausa %.0fh por racha de pérdidas", self.cfg.PAUSE_HOURS)
        else:
            st["consec_losses"] = 0

    async def daily_summary(self, http, day):
        trs = [t for t in self.state.d["closed"] if t.get("closed_ms", 0) // DAY_MS == day]
        date = datetime.fromtimestamp(day * 86400, timezone.utc).strftime("%d/%m")
        if not trs:
            await self.tg.send(http, f"📊 Resumen {date}: sin señales cerradas · abiertas {len(self.state.d['active'])}")
            return
        nr = [tracker.net_r(t) for t in trs]
        wins = sum(1 for x in nr if x > 0)
        await self.tg.send(http, f"📊 <b>Resumen {date}</b>\nCerradas {len(trs)} · ganadoras {wins} ({wins / len(trs) * 100:.0f}%)\n"
                                 f"Total {sum(nr):+.2f}R neto · media {sum(nr) / len(nr):+.2f}R\n"
                                 f"Abiertas: {len(self.state.d['active'])}")

    async def run(self):
        cfg = self.cfg
        log.info("CODE_VERSION=%s · %s", CODE_VERSION, cfg.summary())
        async with BingX(cfg.CONCURRENCY) as bx, aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
            await self.refresh_universe(bx)
            await self.tg.send(http, f"🤖 <b>{cfg.BOT_NAME}</b> online · {CODE_VERSION}\n{cfg.summary()}\n"
                                     f"Vigilando {len(self.universe)} pares · abiertas {len(self.state.d['active'])}")
            while True:
                now = time.time()
                tf = cfg.TF_MS / 1000
                nxt = (now // tf + 1) * tf + cfg.SCAN_DELAY_SEC
                await asyncio.sleep(max(1.0, nxt - now))
                try:
                    await self.scan(bx, http)
                except Exception as e:
                    log.exception("scan falló: %s", e)


async def health_server():
    port = os.getenv("PORT")
    if not port:
        return
    from aiohttp import web
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text=f"ok {CODE_VERSION}"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()


async def main():
    await health_server()
    await Bot(Config()).run()


if __name__ == "__main__":
    asyncio.run(main())
