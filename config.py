"""Configuración por variables de entorno (Railway). Todos los parsers limpian comillas."""
import os

CODE_VERSION = "fedwer-signals-1.0.0"

TF_MIN = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120, "4h": 240}


def _raw(key, default=None):
    v = os.getenv(key)
    if v is None:
        return default
    v = v.strip().strip('"').strip("'").strip()
    return default if v == "" else v


def _f(key, default):
    v = _raw(key)
    return float(v) if v is not None else float(default)


def _i(key, default):
    v = _raw(key)
    return int(float(v)) if v is not None else int(default)


def _b(key, default):
    v = _raw(key)
    if v is None:
        return bool(default)
    return v.lower() in ("1", "true", "yes", "si", "sí", "on")


def _list(key):
    v = _raw(key, "")
    return [s.strip().upper() for s in v.split(",") if s.strip()]


class Config:
    def __init__(self):
        # ── Infra ──
        self.TG_TOKEN = _raw("TELEGRAM_BOT_TOKEN", "")
        self.TG_CHAT = _raw("TELEGRAM_CHAT_ID", "")
        self.STATE_DIR = _raw("STATE_DIR", "./data")
        self.BOT_NAME = _raw("BOT_NAME", "FED+WER")
        self.TIMEFRAME = _raw("TIMEFRAME", "15m")
        if self.TIMEFRAME not in TF_MIN:
            raise ValueError(f"TIMEFRAME no soportado: {self.TIMEFRAME}")
        self.TF_MS = TF_MIN[self.TIMEFRAME] * 60_000
        self.SCAN_DELAY_SEC = _i("SCAN_DELAY_SEC", 8)
        self.CONCURRENCY = _i("CONCURRENCY", 6)

        # ── Universo ──
        self.SYMBOLS = _list("SYMBOLS")                  # vacío = top por volumen
        self.TOP_N = _i("TOP_N", 120)
        self.MIN_QUOTE_VOL_24H = _f("MIN_QUOTE_VOL_24H", 5_000_000)
        self.BLACKLIST = set(_list("BLACKLIST"))
        self.BTC_SYMBOL = _raw("BTC_SYMBOL", "BTC-USDT")

        # ── Extremo diario ──
        self.MIN_DAILY_MOVE = _f("MIN_DAILY_MOVE", 9.5)
        self.RS_THRESHOLD = _f("RS_THRESHOLD", 3.6)

        # ── Failed extension ──
        self.EXT_LOOKBACK = _i("EXT_LOOKBACK", 16)
        self.FAIL_BARS = _i("FAIL_BARS", 3)
        self.MIN_RETRACE_PCT = _f("MIN_RETRACE_PCT", 56.0)
        self.MIN_EXT_ATR = _f("MIN_EXT_ATR", 0.50)
        self.VOL_MULT = _f("VOL_MULT", 1.40)
        # MEJORA: la vela de fallo debe CERRAR de vuelta dentro del rango roto
        self.REQUIRE_CLOSE_INSIDE = _b("REQUIRE_CLOSE_INSIDE", True)

        # ── WER ──
        self.WER_THRESHOLD = _f("WER_THRESHOLD", 1.55)
        self.WER_LOOKBACK = _i("WER_LOOKBACK", 4)
        self.WER_ACCEL_MIN = _f("WER_ACCEL_MIN", 0.12)
        self.WER_WINDOW = _i("WER_WINDOW", 0)

        # ── Gestión ──
        self.COOLDOWN_BARS = _i("COOLDOWN_BARS", 5)
        self.ATR_LEN = _i("ATR_LEN", 14)
        self.SL_ATR = _f("SL_ATR", 1.30)
        self.TP_ATR = _f("TP_ATR", 2.15)
        self.USE_TRAILING = _b("USE_TRAILING", True)
        self.TRAIL_ACT_ATR = _f("TRAIL_ACT_ATR", 0.65)
        self.TRAIL_OFF_ATR = _f("TRAIL_OFF_ATR", 0.65)
        self.USE_BE = _b("USE_BE", False)
        self.BE_TRIGGER_ATR = _f("BE_TRIGGER_ATR", 1.0)
        self.BE_LOCK_ATR = _f("BE_LOCK_ATR", 0.05)
        self.MAX_BARS_IN_TRADE = _i("MAX_BARS_IN_TRADE", 0)

        self.USE_MULTI_TP = _b("USE_MULTI_TP", False)
        self.TP1_R = _f("TP1_R", 1.0)
        self.Q1 = _i("Q1", 40)
        self.TP2_R = _f("TP2_R", 1.65)
        self.Q2 = _i("Q2", 30)
        self.TP3_R = _f("TP3_R", 3.0)
        self.BE_AFTER_TP1 = _b("BE_AFTER_TP1", True)

        # ── Filtros de calidad ──
        self.ALLOW_SHORT = _b("ALLOW_SHORT", True)
        self.ALLOW_LONG = _b("ALLOW_LONG", True)
        self.MIN_BAR_VOL_USDT = _f("MIN_BAR_VOL_USDT", 0)
        self.MAX_ATR_PCT = _f("MAX_ATR_PCT", 0)
        self.USE_RSI_FILTER = _b("USE_RSI_FILTER", False)
        self.RSI_HI = _f("RSI_HI", 75)
        self.RSI_LO = _f("RSI_LO", 25)
        # MEJORA: sin stops ridículos que las comisiones se coman el R
        self.MIN_RISK_PCT = _f("MIN_RISK_PCT", 0.30)

        # ── Score / delta ──
        self.MIN_SCORE = _i("MIN_SCORE", 0)
        self.MIN_DELTA = _i("MIN_DELTA", 0)
        self.MIN_FAIL_RVOL = _f("MIN_FAIL_RVOL", 0)

        # ── Contexto HTF ──
        self.CTX_TF = _raw("CTX_TF", "4h")
        self.CTX_EMA = _i("CTX_EMA", 50)
        self.MIN_EXT_EMA_ATR = _f("MIN_EXT_EMA_ATR", 0)
        self.USE_PD = _b("USE_PD", False)
        self.PD_LEN = _i("PD_LEN", 50)
        self.PD_MIN_PCT = _f("PD_MIN_PCT", 70)
        self.USE_SESSION = _b("USE_SESSION", False)
        self.SESSION_UTC = _raw("SESSION_UTC", "0700-2200")

        # ── De "Extreme Fade + Premium" (opcionales) ──
        self.USE_PREMIUM = _b("USE_PREMIUM", False)       # prima vs VWAP diario anclado
        self.PREMIUM_PCT = _f("PREMIUM_PCT", 0.35)
        self.USE_ROC = _b("USE_ROC", False)               # momentum ya girado
        self.ROC_LEN = _i("ROC_LEN", 4)

        # ── Control de señales (global) ──
        self.MAX_OPEN_SIGNALS = _i("MAX_OPEN_SIGNALS", 0)
        self.MAX_SIGNALS_DAY = _i("MAX_SIGNALS_DAY", 0)
        self.PAUSE_AFTER_LOSSES = _i("PAUSE_AFTER_LOSSES", 0)
        self.PAUSE_HOURS = _f("PAUSE_HOURS", 12)
        self.SEND_BLOCKED = _b("SEND_BLOCKED", False)     # avisar de señales bloqueadas por filtros
        self.FEE_PCT = _f("FEE_PCT", 0.05)                # por lado, para R neto

    @property
    def need_htf(self):
        return self.MIN_EXT_EMA_ATR > 0 or self.USE_PD

    def summary(self):
        mode = (f"TP1/2/3 {self.TP1_R}/{self.TP2_R}/{self.TP3_R}R ({self.Q1}/{self.Q2}%)"
                if self.USE_MULTI_TP else f"TP {self.TP_ATR} ATR")
        extra = []
        if self.MIN_SCORE: extra.append(f"score≥{self.MIN_SCORE}")
        if self.USE_PREMIUM: extra.append(f"prima≥{self.PREMIUM_PCT}%")
        if self.USE_ROC: extra.append("ROC")
        if self.need_htf: extra.append("HTF")
        if self.USE_SESSION: extra.append(f"sesión {self.SESSION_UTC}")
        return (f"TF {self.TIMEFRAME} · día ±{self.MIN_DAILY_MOVE}% · RS {self.RS_THRESHOLD} · "
                f"SL {self.SL_ATR} ATR · {mode} · trailing {'ON' if self.USE_TRAILING else 'OFF'}"
                + (f" · filtros: {', '.join(extra)}" if extra else ""))
