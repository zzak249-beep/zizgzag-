"""
Bot "Tres Montañas / Tercer Techo Descendente" -- escanea TODOS los
simbolos USDT-M de BingX (no solo XAUT-USDT), velas de 1h. Ver
gold-three-mountains-bot para la version de un solo simbolo.
"""
import asyncio
import logging
import sys

import config as cfg
from bingx_client import BingXClient
from state import StateManager
from telegram_notifier import TelegramNotifier
from scanner import run_scan_cycle
from healthcheck import start as start_healthcheck

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("main")


async def main_loop() -> None:
    log.info("=" * 50)
    log.info(cfg.CODE_VERSION)
    log.info(
        "MODE=%s  TF=%s  SCAN_INTERVAL=%ds  MAX_CONCURRENT_POSITIONS=%d  MAX_TRADES_PER_DAY=%d",
        cfg.MODE, cfg.TIMEFRAME, cfg.SCAN_INTERVAL_SEC, cfg.MAX_CONCURRENT_POSITIONS, cfg.MAX_TRADES_PER_DAY,
    )
    log.info(
        "Circuit breaker=%s (racha>=%d, perdidas/dia>=%d)  Sesgo HTF=%s (%s, EMA%d)  Tiers=%d majors configurados",
        cfg.USE_CIRCUIT_BREAKER, cfg.LOSS_STREAK_THRESHOLD, cfg.MAX_DAILY_LOSSES,
        cfg.USE_HTF_BIAS, cfg.HTF_TIMEFRAME, cfg.HTF_EMA_LEN, len(cfg.MAJOR_SYMBOLS),
    )
    if cfg.MODE != "LIVE":
        log.info("MODE=SIGNAL -> solo notificaciones, ninguna orden real.")
    log.info("=" * 50)

    start_healthcheck(cfg.HEALTHCHECK_PORT)

    state = StateManager(cfg.STATE_FILE)
    notifier = TelegramNotifier(cfg.TELEGRAM_BOT_TOKEN, cfg.TELEGRAM_CHAT_ID)
    await notifier.start()

    async with BingXClient(cfg.API_KEY, cfg.API_SECRET, cfg.BASE_URL, max_concurrent=cfg.MAX_CONCURRENT_FETCHES + 5) as client:
        while True:
            try:
                await run_scan_cycle(client, state, notifier)
            except Exception as e:
                log.exception("Error inesperado en el ciclo: %s", e)
            await asyncio.sleep(cfg.SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    asyncio.run(main_loop())
