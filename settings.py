"""
Sniper Bot V50.6 — Configuration
All values overridable via environment variables (Railway / .env)
"""
import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class Settings:
    # ─── Exchange ──────────────────────────────────────────────────────────────
    BINGX_API_KEY: str = field(default_factory=lambda: os.getenv("BINGX_API_KEY", ""))
    BINGX_API_SECRET: str = field(default_factory=lambda: os.getenv("BINGX_API_SECRET", ""))
    EXCHANGE_ID: str = "bingx"

    # ─── Telegram ──────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    TELEGRAM_CHAT_ID: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))

    # ─── Capital & Risk ────────────────────────────────────────────────────────
    INITIAL_CAPITAL: float = field(default_factory=lambda: float(os.getenv("INITIAL_CAPITAL", "1000")))
    RISK_PER_TRADE_PCT: float = field(default_factory=lambda: float(os.getenv("RISK_PER_TRADE_PCT", "2.0")))   # % of capital per trade
    MAX_OPEN_POSITIONS: int = field(default_factory=lambda: int(os.getenv("MAX_OPEN_POSITIONS", "3")))
    LEVERAGE: int = field(default_factory=lambda: int(os.getenv("LEVERAGE", "5")))
    USE_KELLY: bool = field(default_factory=lambda: os.getenv("USE_KELLY", "true").lower() == "true")

    # ─── Symbols & Timeframe ───────────────────────────────────────────────────
    PRIMARY_TF: str = field(default_factory=lambda: os.getenv("PRIMARY_TF", "2h"))
    CONFIRM_TF: str = field(default_factory=lambda: os.getenv("CONFIRM_TF", "4h"))   # MTF confluence
    SYMBOLS: List[str] = field(default_factory=lambda: os.getenv(
        "SYMBOLS",
        "GWEIUSDT,BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,AVAXUSDT,LINKUSDT,ARBUSDT"
    ).split(","))

    # ─── Strategy Core ─────────────────────────────────────────────────────────
    ATR_FAST: int = 5
    ATR_SLOW: int = 20
    ATR_ENTRY_MULT: float = field(default_factory=lambda: float(os.getenv("ATR_ENTRY_MULT", "2.5")))
    EMA_MACRO: int = 200
    EMA_FAST: int = 7
    RVOL_THRESHOLD: float = field(default_factory=lambda: float(os.getenv("RVOL_THRESHOLD", "1.5")))
    SLOPE_THRESHOLD: float = field(default_factory=lambda: float(os.getenv("SLOPE_THRESHOLD", "40.0")))

    # ─── Special Edge Filters ──────────────────────────────────────────────────
    ADX_PERIOD: int = 14
    ADX_MIN: float = field(default_factory=lambda: float(os.getenv("ADX_MIN", "22.0")))
    BB_PERIOD: int = 20           # Bollinger Bands for squeeze detection
    RSI_PERIOD: int = 14
    RSI_OB: float = 70.0          # RSI overbought (filter longs above)
    RSI_OS: float = 30.0          # RSI oversold (filter shorts below)
    VWAP_FILTER: bool = True      # Entry must be on correct side of VWAP
    SESSION_FILTER: bool = field(default_factory=lambda: os.getenv("SESSION_FILTER", "true").lower() == "true")
    # UTC hours where trading is ALLOWED (avoids dead 0-4h window)
    ALLOWED_UTC_HOURS: List[int] = field(default_factory=lambda: list(range(6, 23)))

    # ─── Exit Management ───────────────────────────────────────────────────────
    TP_MULT: float = 2.0           # TP = entry ± ATR14 * 2.0
    SL_MULT: float = 1.2           # SL = entry ∓ ATR14 * 1.2
    TRAIL_AFTER_PCT: float = 0.5   # Move SL to breakeven after 50% TP hit
    USE_PARTIAL_TP: bool = True    # Take 50% at TP1, let rest run
    MAX_HOLD_BARS: int = 20        # Force close if still open after N bars

    # ─── Scanning ──────────────────────────────────────────────────────────────
    SCAN_INTERVAL_SECONDS: int = field(default_factory=lambda: int(os.getenv("SCAN_INTERVAL_SECONDS", "120")))
    CANDLES_NEEDED: int = 250      # History depth for indicators

    # ─── Logging ───────────────────────────────────────────────────────────────
    LOG_LEVEL: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    LOG_FILE: str = "logs/bot.log"
    TRADE_JOURNAL: str = "logs/trades.json"

    def validate(self):
        missing = []
        for key in ("BINGX_API_KEY", "BINGX_API_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            if not getattr(self, key):
                missing.append(key)
        if missing:
            raise ValueError(f"Missing required env vars: {', '.join(missing)}")
        return True
