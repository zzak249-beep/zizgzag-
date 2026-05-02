# -*- coding: utf-8 -*-
"""config.py -- Phantom Edge Bot ELITE v2.0"""
import os
from dataclasses import dataclass


@dataclass
class Config:
    bingx_api_key:    str   = ""
    bingx_secret_key: str   = ""
    telegram_token:   str   = ""
    telegram_chat_id: str   = ""

    trade_usdt:  float = 5.0
    leverage:    int   = 10

    # ZigZag
    zz_deviation:   float = 0.5    # 5m ZigZag min % swing
    zz15_deviation: float = 0.8    # 15m ZigZag min % swing (coarser)

    # Indicators
    atr_period:  int   = 14
    atr_mult:    float = 1.5
    rr:          float = 2.5
    st_period:   int   = 10
    st_mult:     float = 3.0
    rsi_period:  int   = 14

    # Filters
    min_atr_pct:  float = 0.10
    min_vol_mult: float = 0.8
    min_score:    int   = 6        # out of 12

    # Compat (unused but keep for strategy signature)
    pivot_len:   int   = 3
    adx_period:  int   = 14
    adx_min:     float = 0.0

    # Timeframes
    timeframe:      str = "5m"
    timeframe_slow: str = "15m"
    timeframe_1h:   str = "1h"

    # Position management
    max_positions:   int   = 5
    breakeven_r:     float = 1.0
    partial_pct:     float = 0.25   # first partial at 1R
    max_trade_hours: float = 6.0
    min_r_time_exit: float = 0.5

    # Risk
    max_daily_trades:   int   = 40
    max_daily_loss_pct: float = 5.0
    min_balance_usdt:   float = 15.0

    # Scanning
    symbols_raw:    str = "ALL"
    scan_interval:  int = 60
    max_concurrent: int = 25

    http_timeout: int = 12
    health_port:  int = 8080

    @property
    def symbols(self) -> list[str]:
        if self.symbols_raw.strip().upper() == "ALL":
            return []
        return [s.strip() for s in self.symbols_raw.split(",") if s.strip()]

    def __post_init__(self) -> None:
        e = lambda k, d: os.getenv(k, d)
        self.bingx_api_key      = e("BINGX_API_KEY",      self.bingx_api_key)
        self.bingx_secret_key   = e("BINGX_SECRET_KEY",   self.bingx_secret_key)
        self.telegram_token     = e("TELEGRAM_TOKEN",     self.telegram_token)
        self.telegram_chat_id   = e("TELEGRAM_CHAT_ID",   self.telegram_chat_id)
        self.trade_usdt         = max(5.0, float(e("TRADE_USDT",       str(self.trade_usdt))))
        self.leverage           = int(e("LEVERAGE",            str(self.leverage)))
        self.zz_deviation       = float(e("ZZ_DEVIATION",     str(self.zz_deviation)))
        self.zz15_deviation     = float(e("ZZ15_DEVIATION",   str(self.zz15_deviation)))
        self.atr_period         = int(e("ATR_PERIOD",          str(self.atr_period)))
        self.atr_mult           = float(e("ATR_MULT",          str(self.atr_mult)))
        self.rr                 = float(e("RR",                str(self.rr)))
        self.st_period          = int(e("ST_PERIOD",           str(self.st_period)))
        self.st_mult            = float(e("ST_MULT",           str(self.st_mult)))
        self.rsi_period         = int(e("RSI_PERIOD",          str(self.rsi_period)))
        self.min_atr_pct        = float(e("MIN_ATR_PCT",       str(self.min_atr_pct)))
        self.min_vol_mult       = float(e("MIN_VOL_MULT",      str(self.min_vol_mult)))
        self.min_score          = int(e("MIN_SCORE",           str(self.min_score)))
        self.timeframe          = e("TIMEFRAME",               self.timeframe)
        self.timeframe_slow     = e("TIMEFRAME_SLOW",          self.timeframe_slow)
        self.timeframe_1h       = e("TIMEFRAME_1H",            self.timeframe_1h)
        self.max_positions      = int(e("MAX_POSITIONS",       str(self.max_positions)))
        self.scan_interval      = int(e("SCAN_INTERVAL",       str(self.scan_interval)))
        self.max_concurrent     = int(e("MAX_CONCURRENT",      str(self.max_concurrent)))
        self.symbols_raw        = e("SYMBOLS",                 self.symbols_raw)
        self.health_port        = int(e("PORT",                str(self.health_port)))
        self.max_daily_loss_pct = float(e("MAX_DAILY_LOSS",    str(self.max_daily_loss_pct)))
        self.max_daily_trades   = int(e("MAX_DAILY_TRADES",    str(self.max_daily_trades)))
        self.max_trade_hours    = float(e("MAX_TRADE_HOURS",   str(self.max_trade_hours)))

        if not self.bingx_api_key or not self.bingx_secret_key:
            import sys
            print("FATAL: BINGX_API_KEY / BINGX_SECRET_KEY no configurados", flush=True)
            sys.exit(1)


cfg = Config()
