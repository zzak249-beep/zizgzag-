"""
config.py — Carga y valida todas las variables de entorno.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(f"Variable de entorno requerida no encontrada: {key}")
    return val


class Config:
    # --- Binance ---
    BINANCE_API_KEY: str  = _require("BINANCE_API_KEY")
    BINANCE_SECRET_KEY: str = _require("BINANCE_SECRET_KEY")
    TESTNET: bool         = os.getenv("TESTNET", "false").lower() == "true"

    # --- Telegram ---
    TELEGRAM_TOKEN: str   = _require("TELEGRAM_TOKEN")
    TELEGRAM_CHAT_ID: str = _require("TELEGRAM_CHAT_ID")

    # --- Trading ---
    SYMBOLS: list         = [s.strip() for s in os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(",")]
    TIMEFRAME: str        = os.getenv("TIMEFRAME", "15m")
    LEVERAGE: int         = int(os.getenv("LEVERAGE", "5"))
    RISK_PER_TRADE: float = float(os.getenv("RISK_PER_TRADE", "1.5"))

    # --- Motor Markov ---
    SLOPE_MIN: float       = float(os.getenv("SLOPE_MIN", "30.0"))
    LOOKBACK_MARKOV: int   = int(os.getenv("LOOKBACK_MARKOV", "200"))
    PROB_THRESHOLD: float  = float(os.getenv("PROB_THRESHOLD", "40.0"))

    # --- ADX Adaptativo ---
    ADX_LEN: int    = int(os.getenv("ADX_LEN", "14"))
    ADX_TREND: int  = int(os.getenv("ADX_TREND", "25"))
    ADX_RANGE: int  = int(os.getenv("ADX_RANGE", "20"))

    # --- Filtros institucionales ---
    RVOL_MIN: float    = float(os.getenv("RVOL_MIN", "1.5"))
    POC_LOOKBACK: int  = int(os.getenv("POC_LOOKBACK", "50"))
    PIVOT_LEN: int     = int(os.getenv("PIVOT_LEN", "4"))

    # --- Triple barrera ---
    ATR_MULT_TP: float   = float(os.getenv("ATR_MULT_TP", "2.0"))
    ATR_MULT_SL: float   = float(os.getenv("ATR_MULT_SL", "1.2"))
    MAX_BARS_HOLD: int   = int(os.getenv("MAX_BARS_HOLD", "20"))

    # --- Kotegawa ---
    DIP_PCT: float      = float(os.getenv("DIP_PCT", "20.0"))
    MA_LEN: int         = int(os.getenv("MA_LEN", "25"))
    RSI_LEN: int        = int(os.getenv("RSI_LEN", "14"))
    RSI_OVERSOLD: float = float(os.getenv("RSI_OVERSOLD", "24.0"))
    BB_LEN: int         = int(os.getenv("BB_LEN", "20"))
    BB_MULT: float      = float(os.getenv("BB_MULT", "2.0"))

    # --- Riesgo global ---
    MAX_DAILY_LOSS_PCT: float  = float(os.getenv("MAX_DAILY_LOSS_PCT", "3.0"))
    MAX_OPEN_POSITIONS: int    = int(os.getenv("MAX_OPEN_POSITIONS", "2"))
    LOOP_INTERVAL: int         = int(os.getenv("LOOP_INTERVAL", "60"))

    def __repr__(self) -> str:
        return (
            f"<Config symbols={self.SYMBOLS} tf={self.TIMEFRAME} "
            f"lev={self.LEVERAGE}x testnet={self.TESTNET}>"
        )
