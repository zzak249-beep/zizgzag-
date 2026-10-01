"""Cliente público BingX Perpetual (solo datos de mercado, sin claves)."""
import asyncio
import logging

import aiohttp

BASE = "https://open-api.bingx.com"
log = logging.getLogger("bingx")


class BingX:
    def __init__(self, concurrency=6):
        self.sem = asyncio.Semaphore(concurrency)
        self.session = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        return self

    async def __aexit__(self, *a):
        await self.session.close()

    async def _get(self, path, params=None, retries=3):
        for k in range(retries):
            try:
                async with self.sem:
                    async with self.session.get(BASE + path, params=params) as r:
                        js = await r.json(content_type=None)
                if js.get("code", 0) != 0:
                    raise RuntimeError(f"{path} {params} → {js.get('code')} {js.get('msg')}")
                return js.get("data")
            except Exception as e:
                if k == retries - 1:
                    raise
                log.debug("retry %s: %s", path, e)
                await asyncio.sleep(1.5 * (k + 1))

    async def contracts(self):
        data = await self._get("/openApi/swap/v2/quote/contracts") or []
        return {d["symbol"]: d for d in data}

    async def tickers(self):
        return await self._get("/openApi/swap/v2/quote/ticker") or []

    async def klines(self, symbol, interval, limit=500, start=None, end=None):
        p = {"symbol": symbol, "interval": interval, "limit": min(int(limit), 1440)}
        if start is not None:
            p["startTime"] = int(start)
        if end is not None:
            p["endTime"] = int(end)
        data = await self._get("/openApi/swap/v3/quote/klines", p) or []
        rows = {}
        for k in data:
            if isinstance(k, dict):
                ts = int(k["time"])
                rows[ts] = (ts, float(k["open"]), float(k["high"]), float(k["low"]), float(k["close"]), float(k["volume"]))
            else:  # formato lista
                ts = int(k[0])
                rows[ts] = (ts, float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]))
        return [rows[t] for t in sorted(rows)]

    async def klines_range(self, symbol, interval, start, end, step_ms):
        """Paginado hacia delante para backtests largos."""
        out, cur = {}, start
        while cur < end:
            chunk = await self.klines(symbol, interval, 1000, start=cur, end=min(end, cur + step_ms * 1000))
            if not chunk:
                cur += step_ms * 1000
                continue
            for r in chunk:
                out[r[0]] = r
            nxt = chunk[-1][0] + step_ms
            cur = nxt if nxt > cur else cur + step_ms * 1000
        return [out[t] for t in sorted(out)]


def to_cols(rows, now_ms=None, tf_ms=None):
    """Filas → columnas, descartando la vela en formación."""
    if now_ms is not None and tf_ms is not None:
        rows = [r for r in rows if r[0] + tf_ms <= now_ms]
    return {
        "t": [r[0] for r in rows], "o": [r[1] for r in rows], "h": [r[2] for r in rows],
        "l": [r[3] for r in rows], "c": [r[4] for r in rows], "v": [r[5] for r in rows],
    }
