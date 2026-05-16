"""
bot/utils.py — Utilidades generales.
"""
import logging
import sys
from pathlib import Path


def setup_logging(level: str = "INFO") -> None:
    """Configura logging con colores en consola y archivo."""
    try:
        import colorlog
        formatter = colorlog.ColoredFormatter(
            "%(log_color)s%(asctime)s [%(levelname)s]%(reset)s %(name)s: %(message)s",
            log_colors={
                "DEBUG":    "cyan",
                "INFO":     "green",
                "WARNING":  "yellow",
                "ERROR":    "red",
                "CRITICAL": "bold_red",
            }
        )
    except ImportError:
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        )

    Path("logs").mkdir(exist_ok=True)
    file_handler = logging.FileHandler("logs/bot.log", encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=[file_handler, stream_handler]
    )


def timeframe_to_seconds(tf: str) -> int:
    """Convierte '15m' → 900, '1h' → 3600, etc."""
    unit = tf[-1].lower()
    val  = int(tf[:-1])
    mul  = {"m": 60, "h": 3600, "d": 86400}.get(unit, 60)
    return val * mul
