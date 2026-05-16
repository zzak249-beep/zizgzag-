"""
Sniper Bot V50.6 — Entry Point
Run: python main.py
"""
import asyncio
import logging
import sys
import os
from pathlib import Path

# Ensure project root is on PYTHONPATH
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from config.settings import Settings
from src.bot import SniperBot

# ── Logging setup ─────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)

settings = Settings()

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(settings.LOG_FILE),
    ],
)
logger = logging.getLogger(__name__)


async def main():
    try:
        settings.validate()
    except ValueError as exc:
        logger.error(f"❌ Configuration error: {exc}")
        logger.error("Copy .env.example to .env and fill in your credentials.")
        sys.exit(1)

    bot = SniperBot(settings)

    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt — stopping bot.")
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
