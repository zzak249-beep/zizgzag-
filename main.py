"""
main.py — ZigZag Multi-Symbol Bot para BingX.
Escanea todas las monedas disponibles cada 15 minutos.
"""
import logging, signal, sys, time
from datetime import datetime
import schedule

import config
import bingx_client as bx
import telegram_notifier as tg
from strategy import run_scan_cycle, stats, positions

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level   = getattr(logging, config.LOG_LEVEL, logging.INFO),
    format  = "%(asctime)s │ %(levelname)-8s │ %(name)s │ %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("main")

# ── Lista de símbolos (se refresca cada hora) ──────────────────────────────
SYMBOLS: list = []
CYCLE_COUNT = 0

def refresh_symbols() -> None:
    global SYMBOLS
    logger.info("Actualizando lista de símbolos...")
    try:
        SYMBOLS = bx.get_top_symbols_by_volume(config.MAX_SYMBOLS, config.MIN_VOLUME_USDT)
        logger.info("✅ %d símbolos cargados.", len(SYMBOLS))
    except Exception as e:
        logger.error("Error actualizando símbolos: %s", e)
        tg.error(f"No se pudo actualizar lista de símbolos: {e}")

def run_cycle() -> None:
    global CYCLE_COUNT
    CYCLE_COUNT += 1
    if not SYMBOLS:
        refresh_symbols()
    try:
        run_scan_cycle(SYMBOLS)
    except Exception as e:
        logger.exception("Error en ciclo: %s", e)
        tg.error(f"Error en ciclo #{CYCLE_COUNT}: {e}")

def daily_report() -> None:
    try:
        balance_data = bx.get_balance()
        balance_usdt = float(balance_data.get("balance", balance_data.get("equity", 0)))
        tg.daily_report(
            stats["trades"], stats["wins"], stats["losses"],
            stats["pnl"], balance_usdt,
        )
    except Exception as e:
        logger.error("Error reporte diario: %s", e)

# ── Graceful shutdown ──────────────────────────────────────────────────────
def _shutdown(sig, frame):
    logger.info("Apagando bot (señal %s)...", sig)
    tg.bot_stopped("Señal del sistema")
    sys.exit(0)

signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT,  _shutdown)

# ── Alineación con vela ────────────────────────────────────────────────────
def wait_next_candle() -> None:
    now  = datetime.utcnow()
    mins = now.minute
    secs = now.second
    nxt  = ((mins // 15) + 1) * 15
    wait = (nxt - mins) * 60 - secs + 3
    logger.info("Esperando %ds hasta el cierre de vela (:%02d)...", wait, nxt % 60)
    time.sleep(wait)

# ── Main ───────────────────────────────────────────────────────────────────
def main():
    logger.info("=" * 55)
    logger.info("  🤖  ZigZag Multi-Symbol Bot")
    logger.info("  Leverage:    %dx", config.LEVERAGE)
    logger.info("  TP/SL:       %g / %g pips", config.TP_PIPS, config.SL_PIPS)
    logger.info("  Max symbols: %d", config.MAX_SYMBOLS)
    logger.info("  Max pos.:    %d", config.MAX_OPEN_POSITIONS)
    logger.info("=" * 55)

    # Verificar BingX
    try:
        bal  = bx.get_balance()
        usdt = float(bal.get("balance", bal.get("equity", 0)))
        logger.info("✅ BingX conectado. Balance: %.2f USDT", usdt)
    except Exception as e:
        logger.error("❌ No se pudo conectar a BingX: %s", e)
        tg.error(f"Fallo al conectar BingX: {e}")
        sys.exit(1)

    # Cargar símbolos
    refresh_symbols()
    if not SYMBOLS:
        logger.error("No se encontraron símbolos. Revisa MIN_VOLUME_USDT.")
        sys.exit(1)

    # Notificar inicio
    tg.bot_started(len(SYMBOLS))

    # Programar tareas
    schedule.every(15).minutes.do(run_cycle)
    schedule.every(1).hours.do(refresh_symbols)
    schedule.every().day.at("00:01").do(daily_report)

    # Primera ejecución alineada con el cierre de vela
    wait_next_candle()
    run_cycle()

    # Loop
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    main()
