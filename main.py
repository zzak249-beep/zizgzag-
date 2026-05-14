import asyncio, logging, sys
from datetime import datetime, timezone
import aiohttp
import config
import telegram_notifier as tg
from bingx_client import BingXClient
from scanner import scan_explosive_pairs
from trader import Trader

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])
log=logging.getLogger("main")

async def main():
    conn=aiohttp.TCPConnector(limit=50,ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=conn) as session:
        client=BingXClient(session)
        trader=Trader(client,session)

        balance=await client.get_balance()
        log.info(f"💵 Balance: {balance:.2f} USDT")
        await tg.bot_start(session)

        pairs=await scan_explosive_pairs(client,session,balance)
        last_day=datetime.now(timezone.utc).day
        log.info(f"🚀 {len(pairs)} pares activos")

        while True:
            try:
                now=datetime.now(timezone.utc)
                if now.day!=last_day:
                    balance=await client.get_balance()
                    await tg.daily_summary(session,trader.daily_trades,
                                           trader.daily_wins,trader.daily_pnl,balance)
                    trader.reset_daily()
                    pairs=await scan_explosive_pairs(client,session,balance)
                    last_day=now.day

                balance=await client.get_balance()
                await trader.refresh_live_positions()

                if pairs and not trader.paused:
                    res=await asyncio.gather(
                        *[trader.process_pair(s,balance) for s in pairs],
                        return_exceptions=True)
                    for s,r in zip(pairs,res):
                        if isinstance(r,Exception):
                            log.error(f"[{s}] {r}")

            except Exception as e:
                log.exception(f"Main loop: {e}")
                await tg.error_alert(session,f"Main: {e}")
                await asyncio.sleep(10)

            await asyncio.sleep(config.CANDLE_SLEEP)

if __name__=="__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: log.info("Detenido.")
