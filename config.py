"""
QF×JP v3.5 PREDATOR — Multi-Symbol Scanner Config
All values from environment variables.
"""
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # ── BingX ─────────────────────────────────────────────────────────────
    BINGX_API_KEY:    str = Field(..., description="BingX API Key")
    BINGX_SECRET_KEY: str = Field(..., description="BingX Secret Key")
    BINGX_BASE_URL:   str = "https://open-api.bingx.com"

    # ── Telegram ──────────────────────────────────────────────────────────
    TELEGRAM_TOKEN:   str = Field(...)
    TELEGRAM_CHAT_ID: str = Field(...)

    # ── Scanner settings ──────────────────────────────────────────────────
    TIMEFRAME:          str = "3m"    # candle interval: 1m 3m 5m 15m 1h
    SCAN_INTERVAL:      int = 60      # seconds between full scans
    MAX_SYMBOLS:        int = 80      # max perpetual pairs to scan (top by volume)
    CONCURRENT_SCANS:   int = 10      # parallel symbol evaluations
    MIN_VOLUME_USDT:    float = 500000  # min 24h volume to include symbol

    # ── Capital & Risk ────────────────────────────────────────────────────
    CAPITAL:          float = 1000.0
    RISK_PCT:         float = 1.0     # % capital risk per trade
    LEVERAGE:         int   = 10
    TP1_MULT:         float = 1.5     # ATR multiplier TP1
    TP2_MULT:         float = 3.0     # ATR multiplier TP2
    SL_MULT:          float = 1.0     # ATR multiplier SL
    MAX_OPEN_TRADES:  int   = 5       # max simultaneous positions
    MAX_DAILY_TRADES: int   = 20

    # ── Signal filters ────────────────────────────────────────────────────
    # Main trigger: trendline breakout (required)
    REQUIRE_TL_BREAK:   bool = True    # must have TL ruptura
    # Score thresholds
    SC_THR_STD:   int = 55
    SC_THR_FUEL:  int = 68
    SC_THR_SUP:   int = 80
    SC_THR_PRE:   int = 48
    MIN_TIER:     str = "STD"   # STD | FUEL | SUP
    # HTF alignment minimum (0-4 timeframes)
    HTF_MIN:      int = 2

    # ── Strategy parameters ───────────────────────────────────────────────
    ATR_LEN:   int   = 10
    MOM_LEN:   int   = 20
    REV_LEN:   int   = 8
    VOL_LEN:   int   = 14
    ADX_LEN:   int   = 14
    ADX_TREND: int   = 25
    ADX_LAT:   int   = 20
    CVD_LEN:   int   = 20
    CVD_ROLL:  int   = 50
    RSI_LEN:   int   = 14
    MFI_LEN:   int   = 14
    MFI_OB:    int   = 80
    MFI_OS:    int   = 20
    VDI_LEN:   int   = 3
    VDI_THR:   float = 1.5
    SQ_LEN:    int   = 20
    SQ_BBM:    float = 2.0
    SQ_KCM:    float = 1.5
    # Trendline pivot params
    TL_PIVOT_L: int   = 5    # pivot left bars
    TL_PIVOT_R: int   = 3    # pivot right bars
    TL_LOOKBACK:int   = 30   # max bars to search for 2nd pivot
    TL_BUFFER:  float = 0.15 # ATR buffer for breakout

    # ── HTF timeframes ────────────────────────────────────────────────────
    HTF_15M: str = "15m"
    HTF_1H:  str = "1h"
    HTF_4H:  str = "4h"

    # ── Session filter (UTC) ──────────────────────────────────────────────
    ONLY_ACTIVE_SESSION: bool = True  # skip signals in OFF session

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
