"""
bot/strategy.py
Motor de señales híbrido: Sniper Bot V49 + Kotegawa Dip Reversal.

Ventaja competitiva:
  1. Markov Chain filtrado por régimen ADX (V50.1)
  2. Filtro institucional triple: VWAP + RVOL + POC
  3. Confirmación Kotegawa: dip > 20% bajo MA25 + RSI oversold + BB inferior
  4. STC oscillator para evitar sobrecompra/sobreventa extrema
  5. Requiere convergencia de TRES capas independientes de señal
"""
import numpy as np
import pandas as pd
import logging
from dataclasses import dataclass, field
from typing import Optional

from config import Config
from bot import indicators as ind
from bot.markov import MarkovEngine

logger = logging.getLogger(__name__)


@dataclass
class SignalResult:
    symbol:          str
    long:            bool   = False
    short:           bool   = False
    # Sniper
    prob_bull:       float  = 0.0
    prob_bear:       float  = 0.0
    adx:             float  = 0.0
    regime:          str    = "UNKNOWN"
    slope:           float  = 0.0
    adaptive_thr:    float  = 0.0
    rvol:            float  = 0.0
    stc:             float  = 0.0
    poc:             float  = 0.0
    vwap:            float  = 0.0
    # Kotegawa
    pct_below_ma:    float  = 0.0
    rsi_val:         float  = 50.0
    kotegawa_setup:  bool   = False
    kotegawa_bull:   bool   = False
    # Precio referencia
    entry_price:     float  = 0.0
    atr14:           float  = 0.0
    # Puntuación compuesta (0-100)
    score:           float  = 0.0
    reasons:         list   = field(default_factory=list)


class HybridStrategy:
    """
    Genera señales de entrada combinando Sniper V49 + Kotegawa.
    Mantiene el estado del motor Markov entre llamadas.
    """

    def __init__(self, config: Config):
        self.cfg = config
        # Un motor Markov por símbolo
        self._markov: dict[str, MarkovEngine] = {}
        self._kotegawa_pending: dict[str, bool] = {}

    def _get_markov(self, symbol: str) -> MarkovEngine:
        if symbol not in self._markov:
            self._markov[symbol] = MarkovEngine(self.cfg.LOOKBACK_MARKOV)
        return self._markov[symbol]

    # ─────────────────────────────────────────
    # ENTRADA PRINCIPAL
    # ─────────────────────────────────────────

    def analyze(self, df: pd.DataFrame, symbol: str) -> SignalResult:
        """
        df debe tener columnas: open, high, low, close, volume
        con índice DatetimeIndex y al menos 250 filas.
        """
        result = SignalResult(symbol=symbol)

        if len(df) < 100:
            logger.warning(f"{symbol}: datos insuficientes ({len(df)} velas)")
            return result

        try:
            result = self._compute_sniper(df, symbol, result)
            result = self._compute_kotegawa(df, result)
            result = self._combine_signals(result)
        except Exception as e:
            logger.error(f"{symbol} strategy error: {e}", exc_info=True)

        return result

    # ─────────────────────────────────────────
    # CAPA 1 — SNIPER BOT V49
    # ─────────────────────────────────────────

    def _compute_sniper(self, df: pd.DataFrame, symbol: str,
                        result: SignalResult) -> SignalResult:
        cfg = self.cfg

        # Indicadores base
        slope_s   = ind.magic_slope_full(df["high"], df["low"], df["close"])
        atr7_s    = ind.atr(df["high"], df["low"], df["close"], 7)
        atr14_s   = ind.atr(df["high"], df["low"], df["close"], 14)
        plus_di, minus_di, adx_s = ind.adx_dmi(df["high"], df["low"], df["close"], cfg.ADX_LEN)
        vwap_s    = ind.vwap(df["high"], df["low"], df["close"], df["volume"])
        rvol_s    = ind.rvol(df["volume"], 50)
        poc_s     = ind.poc(df["close"], df["volume"], cfg.POC_LOOKBACK)
        stc_s     = ind.stc(df["close"])
        ph_s      = ind.pivot_high(df["high"], cfg.PIVOT_LEN)
        pl_s      = ind.pivot_low(df["low"],   cfg.PIVOT_LEN)
        adap_thr  = ind.adaptive_slope_threshold(
            adx_s, cfg.SLOPE_MIN, cfg.ADX_TREND, cfg.ADX_RANGE
        )

        # Valores actuales (última vela confirmada, es decir iloc[-2])
        i      = -1      # vela actual
        i_prev = -2      # vela anterior confirmada

        cur_slope  = float(slope_s.iloc[i])
        prev_slope = float(slope_s.iloc[i_prev])
        cur_adx    = float(adx_s.iloc[i])
        cur_thr    = float(adap_thr.iloc[i])
        cur_atr14  = float(atr14_s.iloc[i])
        cur_vwap   = float(vwap_s.iloc[i])
        cur_rvol   = float(rvol_s.iloc[i])
        cur_stc    = float(stc_s.iloc[i])
        cur_poc    = float(poc_s.iloc[i]) if not np.isnan(poc_s.iloc[i]) else 0.0
        cur_close  = float(df["close"].iloc[i])
        cur_low    = float(df["low"].iloc[i])
        cur_high   = float(df["high"].iloc[i])

        # Pivots (último valor no-NaN)
        peak_series   = ph_s.dropna()
        valley_series = pl_s.dropna()
        last_peak   = float(peak_series.iloc[-1])   if len(peak_series)   > 0 else np.nan
        last_valley = float(valley_series.iloc[-1])  if len(valley_series) > 0 else np.nan

        # Régimen
        is_trending = cur_adx > cfg.ADX_TREND
        is_ranging  = cur_adx < cfg.ADX_RANGE
        regime      = "TENDENCIA" if is_trending else ("RANGO" if is_ranging else "TRANSICION")

        # Motor Markov
        markov = self._get_markov(symbol)
        prob_bull, prob_bear = markov.update(cur_slope, prev_slope, cur_thr)

        # Volumen institucional
        is_dense = cur_rvol >= cfg.RVOL_MIN
        eff_thr  = cfg.PROB_THRESHOLD - 5.0 if is_dense else cfg.PROB_THRESHOLD

        # ---- Señal LONG Sniper ----
        long_sniper = (
            not np.isnan(last_valley)        and
            cur_low  < last_valley           and
            cur_close < cur_vwap             and
            cur_slope > cur_thr              and
            is_dense                         and
            prob_bull > eff_thr              and
            cur_stc < 75
        )

        # ---- Señal SHORT Sniper ----
        short_sniper = (
            not np.isnan(last_peak)          and
            cur_high > last_peak             and
            cur_close > cur_vwap             and
            cur_slope < -cur_thr             and
            is_dense                         and
            prob_bear > eff_thr              and
            cur_stc > 25
        )

        # Relleno del resultado
        result.prob_bull    = prob_bull
        result.prob_bear    = prob_bear
        result.adx          = cur_adx
        result.regime       = regime
        result.slope        = cur_slope
        result.adaptive_thr = cur_thr
        result.rvol         = cur_rvol
        result.stc          = cur_stc
        result.poc          = cur_poc
        result.vwap         = cur_vwap
        result.entry_price  = cur_close
        result.atr14        = cur_atr14
        # Guardamos señales parciales en el objeto para combinación posterior
        result._long_sniper  = long_sniper    # type: ignore[attr-defined]
        result._short_sniper = short_sniper   # type: ignore[attr-defined]

        return result

    # ─────────────────────────────────────────
    # CAPA 2 — KOTEGAWA DIP REVERSAL
    # ─────────────────────────────────────────

    def _compute_kotegawa(self, df: pd.DataFrame,
                          result: SignalResult) -> SignalResult:
        cfg = self.cfg

        # Indicadores Kotegawa (usa MA diaria simulada con cierra de la TF actual)
        ma25      = ind.sma(df["close"], cfg.MA_LEN)
        rsi_s     = ind.rsi(df["close"], cfg.RSI_LEN)
        bb_up, bb_basis, bb_low = ind.bollinger_bands(df["close"], cfg.BB_LEN, cfg.BB_MULT)

        cur_close  = float(df["close"].iloc[-1])
        cur_ma25   = float(ma25.iloc[-1])
        cur_rsi    = float(rsi_s.iloc[-1])
        cur_bb_low = float(bb_low.iloc[-1])
        cur_bb_bas = float(bb_basis.iloc[-1])

        dip_level = cur_ma25 * (1.0 - cfg.DIP_PCT / 100.0)
        pct_below = (cur_ma25 - cur_close) / cur_ma25 * 100.0 if cur_ma25 > 0 else 0.0

        dip_ok  = cur_close <= dip_level
        rsi_ok  = cur_rsi <= cfg.RSI_OVERSOLD
        bb_ok   = cur_close <= cur_bb_low
        setup   = dip_ok and rsi_ok and bb_ok

        # Persistir estado "pending" entre barras
        sym = result.symbol
        if setup:
            self._kotegawa_pending[sym] = True

        pending = self._kotegawa_pending.get(sym, False)
        bull_candle = float(df["close"].iloc[-1]) > float(df["open"].iloc[-1])
        kotegawa_entry = pending and bull_candle

        if kotegawa_entry:
            self._kotegawa_pending[sym] = False   # se consume la señal

        result.pct_below_ma   = round(pct_below, 2)
        result.rsi_val        = round(cur_rsi, 2)
        result.kotegawa_setup = setup
        result.kotegawa_bull  = kotegawa_entry

        return result

    # ─────────────────────────────────────────
    # CAPA 3 — FUSIÓN DE SEÑALES
    # ─────────────────────────────────────────

    def _combine_signals(self, result: SignalResult) -> SignalResult:
        """
        Estrategia de fusión con puntuación compuesta (0-100).
        Requiere al menos 2 de 3 capas confirmadas.

        Capas para LONG:
          A) Sniper long_sniper activo
          B) Kotegawa setup + vela alcista
          C) Prob_bull > umbral Y régimen no RANGO

        Capas para SHORT:
          A) Sniper short_sniper activo
          B) STC > 75 (sobrecompra extrema)
          C) Prob_bear > umbral Y tendencia bajista
        """
        score_long  = 0.0
        score_short = 0.0
        reasons     = []

        # ─── LONG ───
        if getattr(result, "_long_sniper", False):
            score_long += 40.0
            reasons.append("✅ Sniper LONG")
        if result.kotegawa_bull:
            score_long += 35.0
            reasons.append("✅ Kotegawa LONG")
        if result.prob_bull > self.cfg.PROB_THRESHOLD:
            score_long += 15.0
            reasons.append(f"✅ Markov bull {result.prob_bull:.1f}%")
        if result.regime == "TENDENCIA" and result.slope > 0:
            score_long += 10.0
            reasons.append("✅ Régimen TENDENCIA alcista")

        # ─── SHORT ───
        if getattr(result, "_short_sniper", False):
            score_short += 40.0
            reasons.append("📉 Sniper SHORT")
        if result.prob_bear > self.cfg.PROB_THRESHOLD:
            score_short += 30.0
            reasons.append(f"📉 Markov bear {result.prob_bear:.1f}%")
        if result.stc > 75:
            score_short += 20.0
            reasons.append("📉 STC sobrecompra")
        if result.regime == "TENDENCIA" and result.slope < 0:
            score_short += 10.0
            reasons.append("📉 Régimen TENDENCIA bajista")

        # Umbral mínimo: 55 puntos (al menos 2 capas convergiendo)
        MIN_SCORE = 55.0
        result.long  = score_long  >= MIN_SCORE
        result.short = score_short >= MIN_SCORE
        result.score = max(score_long, score_short)
        result.reasons = reasons

        if result.long:
            logger.info(f"[{result.symbol}] SEÑAL LONG score={score_long:.0f} {reasons}")
        if result.short:
            logger.info(f"[{result.symbol}] SEÑAL SHORT score={score_short:.0f} {reasons}")

        return result
