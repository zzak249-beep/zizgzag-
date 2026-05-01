"""
main.py — Punto de entrada del ZigZag Breakout Bot.

Ejecuta la estrategia al cierre de cada vela de 15 minutos.
Railway lo mantiene vivo 24/7.
"""

import logging
import signal
import sys
import time
from datetime import datetime

import schedule

import config
import bingx_client as bx
import telegram_notifier as tg
from strategy import run_strategy, get_stats_summary, state

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level   = getattr(logging, config.LOG_LEVEL, logging.INFO),
    format  = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("main")


# ─────────────────────────────────────────────────────────────────────────────
# Graceful shutdown
# ─────────────────────────────────────────────────────────────────────────────

def _handle_signal(sig, frame):
    logger.info("Señal de apagado recibida (%s). Cerrando bot...", sig)
    tg.send_bot_stopped("Señal del sistema (SIGTERM/SIGINT)")
    sys.exit(0)

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)


# ─────────────────────────────────────────────────────────────────────────────
# Tareas programadas
# ─────────────────────────────────────────────────────────────────────────────

def run_with_error_handling():
    """Wrapper que captura excepciones no controladas en la estrategia."""
    try:
        run_strategy()
    except Exception as e:
        logger.exception("Error no controlado en run_strategy: %s", e)
        tg.send_error(f"Error crítico: {e}")


def daily_report():
    """Envía un resumen diario a Telegram a las 00:00 UTC."""
    try:
        summary = get_stats_summary()
        balance = bx.get_balance()
        balance_usdt = float(balance.get("balance", 0))
        full_msg = summary + f"\n💼 Balance: <code>{balance_usdt:.2f} USDT</code>"
        tg.send_balance(balance_usdt)
        logger.info("Reporte diario enviado.")
    except Exception as e:
        logger.error("Error en reporte diario: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# Arranque
# ─────────────────────────────────────────────────────────────────────────────

def wait_for_next_candle():
    """
    Espera hasta el cierre de la próxima vela de 15 minutos.
    Las velas de 15m cierran en: :00, :15, :30, :45
    """
    now    = datetime.utcnow()
    minute = now.minute
    second = now.second
    # Calcular minutos hasta el próximo múltiplo de 15
    next_multiple = ((minute // 15) + 1) * 15
    wait_seconds  = (next_multiple - minute) * 60 - second + 2  # +2s de margen
    logger.info(
        "Esperando %ds hasta el cierre de la próxima vela de 15m (a las :%02d)...",
        wait_seconds, next_multiple % 60,
    )
    time.sleep(wait_seconds)


def main():
    logger.info("=" * 60)
    logger.info("  ZigZag Breakout Bot — Iniciando")
    logger.info("  Símbolo:    %s", config.SYMBOL)
    logger.info("  Timeframe:  %s", config.TIMEFRAME)
    logger.info("  Leverage:   %dx", config.LEVERAGE)
    logger.info("  TP:         %s pips", config.TP_PIPS)
    logger.info("  SL:         %s pips", config.SL_PIPS)
    logger.info("  Capital:    %s USDT/op", config.CAPITAL_PER_TRADE)
    logger.info("=" * 60)

    # Verificar conectividad con BingX
    try:
        balance = bx.get_balance()
        balance_usdt = float(balance.get("balance", 0))
        logger.info("✅ BingX conectado. Balance: %.2f USDT", balance_usdt)
    except Exception as e:
        logger.error("❌ No se pudo conectar a BingX: %s", e)
        tg.send_error(f"No se pudo conectar a BingX al iniciar: {e}")
        sys.exit(1)

    # Verificar conectividad con Telegram
    tg.send_bot_started(config.SYMBOL, config.LEVERAGE)
    logger.info("✅ Telegram conectado.")

    # Programar tareas
    # La estrategia se ejecuta cada 15 minutos, sincronizada con las velas
    schedule.every(15).minutes.do(run_with_error_handling)
    schedule.every().day.at("00:01").do(daily_report)

    logger.info("Scheduler configurado. Esperando primera vela...")

    # Primera ejecución alineada con el cierre de vela
    wait_for_next_candle()
    run_with_error_handling()

    # Loop principal
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
