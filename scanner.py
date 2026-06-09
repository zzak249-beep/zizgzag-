"""
Scanner — fetches candles for every BingX perpetual and runs the strategy engine.
Returns a list of actionable signals sorted by score.
"""
import asyncio
import logging
from typing import Optional

from .bingx_client import BingXClient
from .engine import StrategyEngine

log = logging.getLogger("qfjp.scanner")


class MultiSymbolScanner:
    def __init__(self, settings):
        self.s = settings

    async def scan_symbol(
        self,
        symbol: str,
        bingx: BingXClient,
        engine: StrategyEngine,
        semaphore: asyncio.Semaphore,
    ) -> Optional[dict]:
        async with semaphore:
            try:
                candles = await bingx.get_klines(symbol, self.s.TIMEFRAME, limit=150)
                if len(candles) < 60:
                    return None

                # Optional HTF candles (15m, 1h, 4h) for alignment
                htf = {}
                for tf in (self.s.HTF_15M, self.s.HTF_1H, self.s.HTF_4H):
                    c = await bingx.get_klines(symbol, tf, limit=30)
                    if len(c) >= 22:
                        htf[tf] = c

                sig = engine.evaluate(candles, htf if htf else None)
                sig["symbol"] = symbol

                # Only return if actionable
                if sig["tier"] not in ("STD", "FUEL", "SUP", "PRE"):
                    return None

                log.info(
                    f"  📌 {symbol:18s} {sig['side']:5s} {sig['tier']:4s} "
                    f"L:{sig['score_long']:3d} S:{sig['score_short']:3d} "
                    f"TL_L:{sig['tl_break_long']} TL_S:{sig['tl_break_short']}"
                )
                return sig

            except Exception as exc:
                log.debug(f"scan_symbol {symbol}: {exc}")
                return None

    async def scan_all(
        self,
        bingx: BingXClient,
        engine: StrategyEngine,
        symbols: list[str],
    ) -> list[dict]:
        semaphore = asyncio.Semaphore(self.s.CONCURRENT_SCANS)
        tasks = [
            self.scan_symbol(sym, bingx, engine, semaphore)
            for sym in symbols
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        signals = []
        for r in results:
            if isinstance(r, dict) and r is not None:
                signals.append(r)

        # Sort: SUP first, then FUEL, then STD, by score descending
        tier_rank = {"SUP": 3, "FUEL": 2, "STD": 1, "PRE": 0}
        signals.sort(
            key=lambda x: (
                tier_rank.get(x["tier"], 0),
                max(x["score_long"], x["score_short"]),
            ),
            reverse=True,
        )
        return signals
