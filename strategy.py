"""
Strategy Engine — Sniper Bot V50.6
Converts Pine Script logic to Python + adds special edge filters:
  • Multi-timeframe confluence (2h + 4h)
  • ADX trend-strength filter
  • RSI extremes guard
  • VWAP side filter
  • Bollinger Band squeeze pre-filter
  • Kelly Criterion position sizing
  • Session filter
"""
import logging
import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional, Tuple
from config.settings import Settings

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    symbol: str
    direction: str          # "LONG" | "SHORT" | "NONE"
    entry_price: float
    tp_price: float
    sl_price: float
    size_pct: float         # % of capital to allocate
    atr14: float
    rvol: float
    adx: float
    rsi: float
    slope: float
    reason: str             # human-readable trigger summary


# ─── Indicator Helpers ────────────────────────────────────────────────────────

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    hi, lo, cl = df["high"], df["low"], df["close"]
    tr = pd.concat([hi - lo,
                    (hi - cl.shift()).abs(),
                    (lo - cl.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def _adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder-smoothed ADX."""
    hi, lo, cl = df["high"], df["low"], df["close"]
    up   = hi.diff()
    down = -lo.diff()
    plus_dm  = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)

    tr = pd.concat([hi - lo,
                    (hi - cl.shift()).abs(),
                    (lo - cl.shift()).abs()], axis=1).max(axis=1)

    atr_w = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di  = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1/period, adjust=False).mean() / atr_w
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1/period, adjust=False).mean() / atr_w

    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-9))
    adx = dx.ewm(alpha=1/period, adjust=False).mean()
    return adx


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    rs    = gain / (loss + 1e-9)
    return 100 - 100 / (1 + rs)


def _rvol(volume: pd.Series, period: int = 50) -> pd.Series:
    avg = volume.rolling(period).mean()
    return volume / (avg + 1e-9)


def _vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP approximation over available data."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    cum_vol = df["volume"].cumsum()
    cum_tpv = (tp * df["volume"]).cumsum()
    return cum_tpv / (cum_vol + 1e-9)


def _bb_width(series: pd.Series, period: int = 20, std_dev: float = 2.0) -> pd.Series:
    """Bollinger Band width — low = squeeze, potential breakout incoming."""
    mid  = series.rolling(period).mean()
    std  = series.rolling(period).std()
    width = (2 * std_dev * std) / (mid + 1e-9)
    return width


def _kelly_fraction(win_rate: float, rr_ratio: float) -> float:
    """Kelly Criterion: f = W - (1-W)/R  clamped to [0, 0.25]"""
    if rr_ratio <= 0 or win_rate <= 0:
        return 0.0
    f = win_rate - (1 - win_rate) / rr_ratio
    return float(np.clip(f, 0.0, 0.25))   # half-Kelly cap at 25%


# ─── Main Strategy Class ──────────────────────────────────────────────────────

class SniperStrategy:
    def __init__(self, settings: Settings):
        self.s = settings
        # Rolling performance tracker for Kelly Criterion
        self._trade_log: list = []   # list of {"win": bool, "rr": float}

    # ------------------------------------------------------------------
    def analyze(self,
                df_primary: pd.DataFrame,        # 2h OHLCV
                df_confirm: pd.DataFrame,        # 4h OHLCV
                symbol: str,
                current_hour_utc: int,
                available_capital: float) -> Signal:
        """
        Full signal evaluation pipeline.
        Returns a Signal with direction='NONE' when no trade should be taken.
        """

        s  = self.s
        df = df_primary.copy()

        if len(df) < s.CANDLES_NEEDED:
            return self._no_signal(symbol, "insufficient data")

        # ── Session filter ─────────────────────────────────────────────
        if s.SESSION_FILTER and current_hour_utc not in s.ALLOWED_UTC_HOURS:
            return self._no_signal(symbol, f"dead session UTC {current_hour_utc}")

        # ── Core indicators (primary TF) ───────────────────────────────
        close  = df["close"]
        high   = df["high"]
        low    = df["low"]
        volume = df["volume"]
        open_  = df["open"]

        atr_fast = _atr(df, s.ATR_FAST)
        atr_slow = _atr(df, s.ATR_SLOW)
        atr14    = _atr(df, 14)
        macro    = _ema(close, s.EMA_MACRO)
        ema7     = _ema(close, s.EMA_FAST)
        adx_ser  = _adx(df, s.ADX_PERIOD)
        rsi_ser  = _rsi(close, s.RSI_PERIOD)
        rvol_ser = _rvol(volume, 50)
        vwap_ser = _vwap(df)
        bb_w     = _bb_width(close, s.BB_PERIOD)

        # ── Last-bar values ────────────────────────────────────────────
        c   = close.iloc[-1]
        h   = high.iloc[-1]
        l   = low.iloc[-1]
        o   = open_.iloc[-1]
        ph  = high.iloc[-2]     # previous high (for crossover check)
        pl  = low.iloc[-2]

        atr_f = atr_fast.iloc[-1]
        atr_sl = atr_slow.iloc[-1]
        a14   = atr14.iloc[-1]
        mc    = macro.iloc[-1]
        e7    = ema7.iloc[-1]
        e7_1  = ema7.iloc[-2]
        adx   = adx_ser.iloc[-1]
        rsi   = rsi_ser.iloc[-1]
        rv    = rvol_ser.iloc[-1]
        vwap  = vwap_ser.iloc[-1]
        bbw   = bb_w.iloc[-1]
        bbw_avg = bb_w.rolling(50).mean().iloc[-1]

        # ── Magic Slope (EMA7 derivative / ATR7) ──────────────────────
        atr7 = _atr(df, 7).iloc[-1]
        slope = ((e7 - e7_1) / (atr7 if atr7 > 0 else 1)) * 100

        # ── Breakout levels ────────────────────────────────────────────
        long_brk  = o + atr_sl * s.ATR_ENTRY_MULT
        short_brk = o - atr_sl * s.ATR_ENTRY_MULT

        # ── Signal conditions ──────────────────────────────────────────
        vol_expanding = atr_f > atr_sl
        bull_macro    = c > mc
        bear_macro    = c < mc

        # MTF confluence (4h)
        mtf_bull, mtf_bear = self._mtf_bias(df_confirm)

        # Bollinger squeeze pre-filter: prefer trades just after a squeeze
        # (bbw below its 50-bar average = compressed, about to move)
        bb_squeeze_recently = bbw < bbw_avg * 0.85

        # ADX filter
        adx_ok = adx >= s.ADX_MIN

        # LONG conditions
        long_cross = ph <= long_brk and h > long_brk   # crossover
        long_cond = (
            vol_expanding
            and long_cross
            and slope > s.SLOPE_THRESHOLD
            and rv > s.RVOL_THRESHOLD
            and bull_macro
            and mtf_bull
            and adx_ok
            and rsi < s.RSI_OB           # not overbought
            and (not s.VWAP_FILTER or c > vwap)
        )

        # SHORT conditions
        short_cross = pl >= short_brk and l < short_brk
        short_cond = (
            vol_expanding
            and short_cross
            and slope < -s.SLOPE_THRESHOLD
            and rv > s.RVOL_THRESHOLD
            and bear_macro
            and mtf_bear
            and adx_ok
            and rsi > s.RSI_OS           # not oversold
            and (not s.VWAP_FILTER or c < vwap)
        )

        if not long_cond and not short_cond:
            return self._no_signal(
                symbol,
                f"no signal | vol_exp={vol_expanding} adx={adx:.1f} "
                f"rv={rv:.2f} slope={slope:.1f} mtf_bull={mtf_bull} mtf_bear={mtf_bear}"
            )

        direction = "LONG" if long_cond else "SHORT"

        # ── Price targets ──────────────────────────────────────────────
        entry = c
        if direction == "LONG":
            tp = entry + a14 * s.TP_MULT
            sl = entry - a14 * s.SL_MULT
        else:
            tp = entry - a14 * s.TP_MULT
            sl = entry + a14 * s.SL_MULT

        # ── Position sizing ────────────────────────────────────────────
        size_pct = self._position_size(available_capital, entry, sl, s)

        reason = (
            f"{direction} | ATR_x={vol_expanding} bbSqz={bb_squeeze_recently} "
            f"rv={rv:.2f}x slope={slope:.1f} adx={adx:.1f} rsi={rsi:.1f} "
            f"mtf={'bull' if mtf_bull else 'bear'}"
        )

        logger.info(f"[{symbol}] SIGNAL: {reason}")

        return Signal(
            symbol=symbol,
            direction=direction,
            entry_price=round(entry, 8),
            tp_price=round(tp, 8),
            sl_price=round(sl, 8),
            size_pct=size_pct,
            atr14=round(a14, 8),
            rvol=round(rv, 3),
            adx=round(adx, 2),
            rsi=round(rsi, 2),
            slope=round(slope, 2),
            reason=reason,
        )

    # ------------------------------------------------------------------
    def _mtf_bias(self, df4h: pd.DataFrame) -> Tuple[bool, bool]:
        """4h EMA200 + ADX: returns (bull_ok, bear_ok)."""
        if df4h is None or len(df4h) < 210:
            return True, True          # no confirmation data → allow both
        close = df4h["close"]
        ema200 = _ema(close, 200)
        adx4h  = _adx(df4h, 14)
        c = close.iloc[-1]
        mc = ema200.iloc[-1]
        adx_val = adx4h.iloc[-1]
        trending = adx_val >= self.s.ADX_MIN
        return (c > mc and trending), (c < mc and trending)

    # ------------------------------------------------------------------
    def _position_size(self,
                       capital: float,
                       entry: float,
                       sl: float,
                       s: Settings) -> float:
        """
        Risk-based sizing with optional Kelly adjustment.
        Returns % of capital to use (e.g. 5.0 = 5%).
        """
        base_risk = s.RISK_PER_TRADE_PCT / 100.0    # e.g. 0.02
        rr = s.TP_MULT / s.SL_MULT                  # e.g. 2.0/1.2 ≈ 1.67

        if s.USE_KELLY and len(self._trade_log) >= 10:
            wins = sum(1 for t in self._trade_log[-30:] if t["win"])
            wr   = wins / min(len(self._trade_log), 30)
            k    = _kelly_fraction(wr, rr)
            # Blend Kelly (50%) with fixed risk (50%) — conservative approach
            effective_risk = 0.5 * k + 0.5 * base_risk
        else:
            effective_risk = base_risk

        # Dollar risk
        dollar_risk = capital * effective_risk
        # Price risk per unit (distance to SL)
        price_risk = abs(entry - sl)
        if price_risk == 0:
            return s.RISK_PER_TRADE_PCT

        units = dollar_risk / price_risk
        position_value = units * entry
        size_pct = (position_value / capital) * 100
        return round(min(size_pct, 30.0), 2)    # Hard cap 30% capital

    # ------------------------------------------------------------------
    def record_trade_result(self, win: bool, rr_achieved: float):
        """Called by the bot after trade closes to update Kelly stats."""
        self._trade_log.append({"win": win, "rr": rr_achieved})
        if len(self._trade_log) > 100:
            self._trade_log.pop(0)

    # ------------------------------------------------------------------
    @property
    def win_rate(self) -> float:
        if not self._trade_log:
            return 0.0
        return sum(1 for t in self._trade_log if t["win"]) / len(self._trade_log)

    # ------------------------------------------------------------------
    @staticmethod
    def _no_signal(symbol: str, reason: str) -> Signal:
        return Signal(
            symbol=symbol, direction="NONE",
            entry_price=0, tp_price=0, sl_price=0,
            size_pct=0, atr14=0, rvol=0, adx=0,
            rsi=0, slope=0, reason=reason,
        )
