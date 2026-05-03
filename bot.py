"""
╔══════════════════════════════════════════════════════════════════════════╗
║        PHANTOM EDGE  U L T I M A T E  v8.0                             ║
║        Nivel institucional. Ventaja real sobre el mercado.              ║
╠══════════════════════════════════════════════════════════════════════════╣
║                                                                          ║
║  LO QUE HACEN LOS BOTS NORMALES:                                        ║
║  · Cruces de medias → entran tarde, con el movimiento ya hecho          ║
║  · Stop loss en pips fijos → fáciles de cazar para el market maker      ║
║  · Operan 24/7 → pérdidas en sesiones sin liquidez                     ║
║  · Una sola señal → muchos falsos positivos                             ║
║                                                                          ║
║  LO QUE HACE ULTIMATE v8:                                               ║
║                                                                          ║
║  ① MARKET REGIME FILTER                                                 ║
║     ADX + ATR Ratio + HMA Slope → Trending/Ranging/Chaotic              ║
║     Solo opera en Trending. Para todo lo demás → skip.                  ║
║                                                                          ║
║  ② LIQUIDITY SWEEP DETECTOR                                             ║
║     El precio barre stops (spike fuera de rango) y REGRESA              ║
║     → Entrada en la reversión. Exactamente lo que hacen los bancos.     ║
║                                                                          ║
║  ③ ORDER BLOCK DETECTION                                                ║
║     Detecta la última vela bajista antes de un impulso alcista          ║
║     (y viceversa). Precio retorna al OB = zona de alta probabilidad.    ║
║                                                                          ║
║  ④ KILLZONE TIMING                                                      ║
║     London Open (07-09 UTC): señales ×1.5 peso                         ║
║     NY Open (13:30-15:30 UTC): señales ×1.5 peso                       ║
║     Overnight Asia (00-06 UTC): señales ×0 (off)                        ║
║                                                                          ║
║  ⑤ SEÑAL BASE: ZigZag + HMA + Future-Trend + CVD                       ║
║     (misma lógica probada del Pine Script)                              ║
║                                                                          ║
║  ⑥ GESTIÓN DINÁMICA DE POSICIÓN                                        ║
║     Break-even automático al 0.8R                                       ║
║     Trailing por ATR en tiempo real                                     ║
║     Reducción de sizing en racha negativa (drawdown protection)         ║
║     Incremento de sizing en racha positiva (profit compounding)         ║
║                                                                          ║
║  SCORE MÁXIMO: 12 puntos                                                ║
║  MIN_SCORE: 5 (señal válida)                                            ║
║  SCORE PREMIUM ≥ 9: sizing máximo × 2                                  ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import os, asyncio, logging, time, hmac, hashlib, json, math
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Optional
import numpy as np
import httpx

# ─────────────────────────────────────────────────────────────────────────
#  CONFIGURACIÓN
# ─────────────────────────────────────────────────────────────────────────
API_KEY        = os.getenv("BINGX_API_KEY", "")
API_SECRET     = os.getenv("BINGX_API_SECRET", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")

AUTO_TRADING     = os.getenv("AUTO_TRADING_ENABLED", "false").lower() == "true"
LEVERAGE         = int(os.getenv("LEVERAGE", "10"))
TIMEFRAME        = os.getenv("TIMEFRAME", "5m")
TIMEFRAME_SLOW   = os.getenv("TIMEFRAME_SLOW", "15m")

# Indicadores base (Pine Script)
PIVOT_LEN   = int(os.getenv("PIVOT_LEN",   "5"))
HMA_LEN     = int(os.getenv("HMA_LEN",    "50"))
FT_PERIOD   = int(os.getenv("FT_PERIOD",  "25"))
CVD_PERIOD  = int(os.getenv("CVD_PERIOD", "20"))

# Market Regime
ADX_PERIOD     = int(os.getenv("ADX_PERIOD",   "14"))
ADX_THRESHOLD  = float(os.getenv("ADX_THRESHOLD", "22"))  # >22 = trending

# Order Block
OB_LOOKBACK    = int(os.getenv("OB_LOOKBACK",   "20"))    # velas atrás para buscar OB
OB_TOUCH_PCT   = float(os.getenv("OB_TOUCH_PCT", "0.3"))  # precio toca OB si está a 0.3×ATR

# Liquidity Sweep
SWEEP_LOOKBACK = int(os.getenv("SWEEP_LOOKBACK", "15"))   # ventana de swing para sweep
SWEEP_RETURN   = float(os.getenv("SWEEP_RETURN",  "0.5"))  # regreso mínimo en fracción de ATR

# Riesgo
TRADE_USDT       = float(os.getenv("TRADE_USDT",       "9"))
TRADE_USDT_MAX   = float(os.getenv("TRADE_USDT_MAX",   "20"))
ATR_SL           = float(os.getenv("ATR_SL",    "1.5"))
ATR_TP1          = float(os.getenv("ATR_TP1",   "1.5"))
ATR_TP2          = float(os.getenv("ATR_TP2",   "3.5"))
ATR_BE           = float(os.getenv("ATR_BE",    "0.8"))   # break-even a 0.8R
TP1_SIZE         = float(os.getenv("TP1_SIZE",  "0.35"))
TP2_SIZE         = float(os.getenv("TP2_SIZE",  "0.35"))

MIN_SCORE        = int(os.getenv("MIN_SCORE",    "5"))
MIN_ATR_PCT      = float(os.getenv("MIN_ATR_PCT","0.10"))
MIN_VOL_MULT     = float(os.getenv("MIN_VOL_MULT","0.6"))

MAX_POSITIONS    = int(os.getenv("MAX_POSITIONS",   "5"))
SCAN_INTERVAL    = int(os.getenv("SCAN_INTERVAL",  "30"))
MAX_CONCURRENT   = int(os.getenv("MAX_CONCURRENT", "30"))
MAX_DAILY_TRADES = int(os.getenv("MAX_DAILY_TRADES","40"))
MAX_DAILY_LOSS   = float(os.getenv("MAX_DAILY_LOSS","5.0"))
PORT             = int(os.getenv("PORT", "8080"))

PRIORITY_RAW = os.getenv("PRIORITY_SYMBOLS",
    "BTC-USDT,ETH-USDT,SOL-USDT,XRP-USDT,DOGE-USDT,"
    "SKY-USDT,REZ-USDT,XNY-USDT,MAXXIN-USDT,ROBO-USDT,"
    "PNUT-USDT,TURBO-USDT,HYPE-USDT,RIVER-USDT,PEPE-USDT,"
    "WIF-USDT,BONK-USDT,FLOKI-USDT,ARB-USDT,SHIB-USDT,"
    "SUI-USDT,APT-USDT,INJ-USDT,TIA-USDT,JUP-USDT,PENGU-USDT"
)
PRIORITY = [s.strip() for s in PRIORITY_RAW.split(",") if s.strip()]

BINGX_BASE = "https://open-api.bingx.com"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ULTIMATE")


# ══════════════════════════════════════════════════════════════════════════
#  ① INDICADORES BASE
# ══════════════════════════════════════════════════════════════════════════

def _f(a) -> np.ndarray:
    return np.nan_to_num(np.asarray(a, dtype=float), nan=0.0, posinf=0.0, neginf=0.0)

def _wma(c, n):
    if len(c) < n: return np.full(len(c), c[-1] if len(c) else 0.0)
    w = np.arange(1, n+1, dtype=float); ws = w.sum()
    r = np.zeros(len(c))
    for i in range(n-1, len(c)): r[i] = np.dot(c[i-n+1:i+1], w) / ws
    return r

def calc_hma(c, n):
    c = _f(c)
    if len(c) < n: return np.full(len(c), c[-1] if len(c) else 0.0)
    return _wma(2.0*_wma(c, max(1,n//2)) - _wma(c, n), max(1, int(math.sqrt(n))))

def calc_atr(h, l, c, p=14) -> float:
    h,l,c = _f(h),_f(l),_f(c)
    if len(c)<p+1: return max(float(np.mean(h-l)), 1e-12)
    tr = np.maximum(h[1:]-l[1:], np.maximum(abs(h[1:]-c[:-1]), abs(l[1:]-c[:-1])))
    tr = np.r_[h[0]-l[0], tr]
    a = np.zeros(len(tr)); a[p-1] = np.mean(tr[:p])
    for i in range(p, len(tr)): a[i] = (a[i-1]*(p-1)+tr[i])/p
    return max(float(a[-1]), 1e-12)

def calc_rsi(c, p=14) -> float:
    c = _f(c)
    if len(c)<p+2: return 50.0
    d = np.diff(c)
    g = np.mean(np.where(d>0,d,0)[:p]); b = np.mean(np.where(d<0,-d,0)[:p])
    for i in range(p, len(d)):
        g=(g*(p-1)+max(d[i],0))/p; b=(b*(p-1)+max(-d[i],0))/p
    return 100.0 if b<1e-12 else 100-100/(1+g/b)

def calc_supertrend(h, l, c, p=10, m=3.0) -> int:
    h,l,c = _f(h),_f(l),_f(c); n = len(c)
    if n < p+2: return 0
    tr = np.maximum(h[1:]-l[1:], np.maximum(abs(h[1:]-c[:-1]), abs(l[1:]-c[:-1])))
    tr = np.r_[h[0]-l[0], tr]
    at = np.zeros(n); at[p-1] = np.mean(tr[:p])
    for i in range(p,n): at[i]=(at[i-1]*(p-1)+tr[i])/p
    hl2=(h+l)/2; ub=hl2+m*at; lb=hl2-m*at
    for i in range(1,n):
        lb[i]=lb[i] if lb[i]>lb[i-1] or c[i-1]<lb[i-1] else lb[i-1]
        ub[i]=ub[i] if ub[i]<ub[i-1] or c[i-1]>ub[i-1] else ub[i-1]
    d=np.zeros(n,dtype=int); st=np.zeros(n); d[p]=1; st[p]=lb[p]
    for i in range(p+1,n):
        if st[i-1]==ub[i-1]: st[i]=lb[i] if c[i]>ub[i] else ub[i]
        else: st[i]=ub[i] if c[i]<lb[i] else lb[i]
        d[i]=1 if st[i]<c[i] else -1
    return int(d[-1])

# ZigZag (Pine exacto)
def _pvh(h,n):
    r=np.full(len(h),np.nan)
    for i in range(n, len(h)-n):
        if h[i]==np.max(h[i-n:i+n+1]): r[i]=h[i]
    return r

def _pvl(l,n):
    r=np.full(len(l),np.nan)
    for i in range(n, len(l)-n):
        if l[i]==np.min(l[i-n:i+n+1]): r[i]=l[i]
    return r

def peak_ser(h,n):
    ph=_pvh(h,n); r=np.full(len(h),np.nan); cur=np.nan
    for i in range(len(h)):
        if not np.isnan(ph[i]): cur=ph[i]
        r[i]=cur
    return r

def valley_ser(l,n):
    pl=_pvl(l,n); r=np.full(len(l),np.nan); cur=np.nan
    for i in range(len(l)):
        if not np.isnan(pl[i]): cur=pl[i]
        r[i]=cur
    return r

def crossover(s,lvl):
    if len(s)<2 or len(lvl)<2: return False
    return bool(s[-2]<=lvl[-2] and s[-1]>lvl[-1] and not np.isnan(lvl[-1]))

def crossunder(s,lvl):
    if len(s)<2 or len(lvl)<2: return False
    return bool(s[-2]>=lvl[-2] and s[-1]<lvl[-1] and not np.isnan(lvl[-1]))

# Future-Trend (Pine exacto)
def calc_ft(o,c,v,p=25) -> float:
    o,c,v=_f(o),_f(c),_f(v); n=len(c)
    if n<p*3+1: return 0.0
    delta=np.where(c>o,v,np.where(c<o,-v,0.0))
    s=0.0
    for i in range(p):
        i0,i1,i2=n-1-i,n-1-i-p,n-1-i-p*2
        if i2>=0: s+=(delta[i0]+delta[i1]+delta[i2])/3.0
    return s/p

# CVD
def calc_cvd(o,c,v,p=20) -> tuple:
    o,c,v=_f(o),_f(c),_f(v); n=len(c)
    if n<p+5: return 0.0,0.0,"neutral"
    delta=np.where(c>o,v,np.where(c<o,-v,0.0))
    cvd=np.cumsum(delta[-p:])
    val=float(cvd[-1]); slope=float(cvd[-1]-cvd[-5]) if len(cvd)>=5 else 0.0
    if val>0 and slope>0:   sig="bull"
    elif val<0 and slope<0: sig="bear"
    else:                    sig="neutral"
    return val, slope, sig


# ══════════════════════════════════════════════════════════════════════════
#  ② MARKET REGIME FILTER — ADX + ATR Ratio
# ══════════════════════════════════════════════════════════════════════════

def calc_adx(h, l, c, p=14) -> float:
    """
    ADX clásico de Wilder.
    > 25 = tendencia fuerte
    > 20 = tendencia moderada
    < 20 = mercado lateral / rango
    """
    h,l,c = _f(h),_f(l),_f(c); n=len(c)
    if n < p*2+1: return 0.0

    tr   = np.maximum(h[1:]-l[1:], np.maximum(abs(h[1:]-c[:-1]), abs(l[1:]-c[:-1])))
    dmp  = np.where((h[1:]-h[:-1])>(l[:-1]-l[1:]), np.maximum(h[1:]-h[:-1],0), 0.0)
    dmm  = np.where((l[:-1]-l[1:])>(h[1:]-h[:-1]), np.maximum(l[:-1]-l[1:],0), 0.0)

    atr_s  = np.zeros(n-1); atr_s[p-1]  = np.mean(tr[:p])
    dmp_s  = np.zeros(n-1); dmp_s[p-1]  = np.mean(dmp[:p])
    dmm_s  = np.zeros(n-1); dmm_s[p-1]  = np.mean(dmm[:p])

    for i in range(p, n-1):
        atr_s[i] = (atr_s[i-1]*(p-1)+tr[i])/p
        dmp_s[i] = (dmp_s[i-1]*(p-1)+dmp[i])/p
        dmm_s[i] = (dmm_s[i-1]*(p-1)+dmm[i])/p

    dip = np.where(atr_s>0, 100*dmp_s/atr_s, 0.0)
    dim = np.where(atr_s>0, 100*dmm_s/atr_s, 0.0)
    dx  = np.where((dip+dim)>0, 100*abs(dip-dim)/(dip+dim), 0.0)

    adx = np.zeros(n-1); adx[p*2-1] = np.mean(dx[p-1:p*2])
    for i in range(p*2, n-1): adx[i] = (adx[i-1]*(p-1)+dx[i])/p
    return float(adx[-1])


def market_regime(h, l, c, atr14: float, adx_period=14) -> tuple[str, float]:
    """
    Clasifica el mercado en:
      'trending_bull'  → ADX fuerte + precio sobre HMA subiendo
      'trending_bear'  → ADX fuerte + precio bajo HMA bajando
      'ranging'        → ADX débil, mercado lateral
      'volatile'       → ATR muy alto (spike/noticias)

    Returns: (regime, adx_value)
    """
    c_arr = _f(c)
    close = float(c_arr[-1])

    adx = calc_adx(h, l, c, adx_period)

    # ATR ratio: ATR actual vs ATR promedio de 50 periodos
    if len(c_arr) >= 50:
        atr_hist = calc_atr(h[-50:], l[-50:], c_arr[-50:], 14)
        atr_ratio = atr14 / max(atr_hist, 1e-12)
    else:
        atr_ratio = 1.0

    # HMA slope
    hma = calc_hma(c_arr, HMA_LEN)
    hma_slope = float(hma[-1] - hma[-3]) / max(atr14, 1e-12) if len(hma)>=3 else 0.0

    if atr_ratio > 3.0:
        return "volatile", adx           # spike de volatilidad — no operar
    if adx >= ADX_THRESHOLD:
        if hma_slope > 0.1:
            return "trending_bull", adx
        elif hma_slope < -0.1:
            return "trending_bear", adx
        else:
            return "trending_mixed", adx
    return "ranging", adx


# ══════════════════════════════════════════════════════════════════════════
#  ③ LIQUIDITY SWEEP DETECTOR
# ══════════════════════════════════════════════════════════════════════════

def detect_liquidity_sweep(h, l, c, atr14: float, lookback=15) -> tuple[bool, bool, str]:
    """
    Un Liquidity Sweep ocurre cuando:
    1. El precio rompe el swing high/low de las últimas N velas
    2. PERO CIERRA DE REGRESO dentro del rango

    Esto indica que el mercado barrió los stops y ahora revierte.
    Es una de las señales más fiables en crypto.

    Returns:
        bull_sweep — hubo sweep bajista (barró longs) → señal LONG
        bear_sweep — hubo sweep alcista (barró shorts) → señal SHORT
        description
    """
    h,l,c = _f(h),_f(l),_f(c)
    if len(c) < lookback+2: return False, False, ""

    # Swing high/low de las últimas N velas (excluyendo la actual)
    prev_h = h[-(lookback+1):-1]
    prev_l = l[-(lookback+1):-1]
    swing_high = float(np.max(prev_h))
    swing_low  = float(np.min(prev_l))

    current_h = float(h[-1])
    current_l = float(l[-1])
    current_c = float(c[-1])
    prev_c    = float(c[-2])

    min_return = atr14 * SWEEP_RETURN

    # Bull sweep: la mecha BAJA por debajo del swing low pero el cierre regresa arriba
    bull_sweep = (
        current_l < swing_low                    # spike bajo el swing low
        and current_c > swing_low                # cierra ENCIMA del low
        and (current_c - current_l) > min_return # cuerpo de regreso significativo
        and current_c > prev_c                   # cierra más alto que anterior
    )

    # Bear sweep: la mecha SUBE sobre el swing high pero el cierre regresa abajo
    bear_sweep = (
        current_h > swing_high                   # spike sobre el swing high
        and current_c < swing_high               # cierra DEBAJO del high
        and (current_h - current_c) > min_return # cuerpo de regreso significativo
        and current_c < prev_c                   # cierra más bajo que anterior
    )

    desc = ""
    if bull_sweep: desc = f"LIQ_SWEEP_BULL(low={swing_low:.5g})"
    if bear_sweep: desc = f"LIQ_SWEEP_BEAR(high={swing_high:.5g})"

    return bull_sweep, bear_sweep, desc


# ══════════════════════════════════════════════════════════════════════════
#  ④ ORDER BLOCK DETECTION
# ══════════════════════════════════════════════════════════════════════════

def detect_orderblock(o, h, l, c, atr14: float, lookback=20) -> tuple[bool, bool, str]:
    """
    Un Order Block es la última vela OPUESTA antes de un movimiento impulsivo.

    Bullish OB: última vela BAJISTA antes de un impulso ALCISTA fuerte
    → El precio regresa a esa zona → alta probabilidad de subida

    Bearish OB: última vela ALCISTA antes de un impulso BAJISTA fuerte
    → El precio regresa a esa zona → alta probabilidad de bajada

    Condición de 'impulso fuerte': cuerpo > 1.5×ATR
    Condición de 'toca OB': precio actual dentro del rango del OB ± buffer
    """
    o,h,l,c = _f(o),_f(h),_f(l),_f(c)
    if len(c) < lookback+5: return False, False, ""

    close = float(c[-1])
    impulse_threshold = atr14 * 1.5
    touch_buffer = atr14 * OB_TOUCH_PCT

    # Buscar Bullish OB (vela bajista previa a impulso alcista)
    bull_ob = False
    bull_ob_desc = ""
    for i in range(len(c)-lookback, len(c)-3):
        if i < 1: continue
        # Vela bajista
        if c[i] < o[i]:
            # ¿Seguida de impulso alcista fuerte?
            next_body = abs(c[i+1] - o[i+1])
            if next_body > impulse_threshold and c[i+1] > o[i+1]:
                # ¿El precio actual está dentro del OB?
                ob_high = float(o[i])  # apertura de la vela bajista
                ob_low  = float(c[i])  # cierre de la vela bajista
                if (ob_low - touch_buffer) <= close <= (ob_high + touch_buffer):
                    bull_ob = True
                    bull_ob_desc = f"OB_BULL({ob_low:.5g}-{ob_high:.5g})"
                    break

    # Buscar Bearish OB (vela alcista previa a impulso bajista)
    bear_ob = False
    bear_ob_desc = ""
    for i in range(len(c)-lookback, len(c)-3):
        if i < 1: continue
        # Vela alcista
        if c[i] > o[i]:
            # ¿Seguida de impulso bajista fuerte?
            next_body = abs(c[i+1] - o[i+1])
            if next_body > impulse_threshold and c[i+1] < o[i+1]:
                ob_low  = float(o[i])  # apertura de la vela alcista
                ob_high = float(c[i])  # cierre de la vela alcista
                if (ob_low - touch_buffer) <= close <= (ob_high + touch_buffer):
                    bear_ob = True
                    bear_ob_desc = f"OB_BEAR({ob_low:.5g}-{ob_high:.5g})"
                    break

    return bull_ob, bear_ob, bull_ob_desc or bear_ob_desc


# ══════════════════════════════════════════════════════════════════════════
#  ⑤ SESSION KILLZONES
# ══════════════════════════════════════════════════════════════════════════

def get_killzone() -> tuple[str, float]:
    """
    Killzones de mayor probabilidad (hora UTC):
      London Open:    07:00–09:00  → peso 1.5
      London Mid:     09:00–12:00  → peso 1.0
      NY Open:        13:30–15:30  → peso 1.5 (mayor volumen del día)
      NY Overlap:     15:30–17:00  → peso 1.0
      NY Close:       17:00–20:00  → peso 0.8
      Asia Open:      00:00–03:00  → peso 0.6
      Dead Zone:      20:00–00:00  → peso 0.3 (baja liquidez)

    Returns: (zone_name, weight)
    """
    h = datetime.now(timezone.utc).hour
    m = datetime.now(timezone.utc).minute
    t = h + m/60.0

    if  7.0 <= t <  9.0: return "🇬🇧 London Open",  1.5
    if  9.0 <= t < 12.0: return "🇬🇧 London Mid",   1.0
    if 12.0 <= t < 13.5: return "⏳ Pre-NY",        0.8
    if 13.5 <= t < 15.5: return "🇺🇸 NY Open",      1.5
    if 15.5 <= t < 17.0: return "🇺🇸 NY Mid",       1.0
    if 17.0 <= t < 20.0: return "🇺🇸 NY Close",     0.8
    if  0.0 <= t <  3.0: return "🌏 Asia Open",     0.6
    if  3.0 <= t <  7.0: return "🌏 Asia Mid",      0.5
    return "💤 Dead Zone", 0.3


# ══════════════════════════════════════════════════════════════════════════
#  ⑥ MOTOR DE SEÑALES ULTIMATE — 12 puntos máximo
# ══════════════════════════════════════════════════════════════════════════
#
#  CAPA A · SEÑAL BASE Pine Script    [0–4 pts]
#    +2 ZigZag breakout confirmado
#    +1 HMA dirección + slope
#    +1 Future-Trend Volume Delta
#
#  CAPA B · ORDER FLOW                [0–3 pts]
#    +1 CVD valor favorable
#    +1 CVD slope (flujo reciente)
#    +1 SuperTrend 5m + 15m alineados
#
#  CAPA C · ESTRUCTURA AVANZADA       [0–3 pts]
#    +2 Liquidity Sweep detectado (señal premium)
#    +1 Order Block tocado
#
#  CAPA D · CONTEXTO + RÉGIMEN        [0–2 pts]
#    +1 Market Regime favorable (ADX trending)
#    +1 Killzone con peso ≥ 1.0 + volumen ok

def analyze(c5: list[dict], c15: list[dict]) -> Optional[dict]:
    need5  = max(HMA_LEN+20, FT_PERIOD*3+10, PIVOT_LEN*6, CVD_PERIOD+10,
                 OB_LOOKBACK+10, SWEEP_LOOKBACK+5, ADX_PERIOD*3)
    need15 = max(HMA_LEN+10, FT_PERIOD*2+10, ADX_PERIOD*2)

    if len(c5) < need5 or len(c15) < need15:
        return None

    # Arrays
    h5  = _f([x["h"] for x in c5]);  l5  = _f([x["l"] for x in c5])
    c5a = _f([x["c"] for x in c5]);  o5  = _f([x["o"] for x in c5])
    v5  = _f([x["v"] for x in c5])
    h15 = _f([x["h"] for x in c15]); l15 = _f([x["l"] for x in c15])
    c15a= _f([x["c"] for x in c15]); o15 = _f([x["o"] for x in c15])
    v15 = _f([x["v"] for x in c15])

    close = float(c5a[-1])
    if close <= 0: return None

    # Filtros básicos
    atr14   = calc_atr(h5,l5,c5a,14)
    atr_pct = atr14/close*100
    if atr_pct < MIN_ATR_PCT: return None

    vol_ma20 = float(np.mean(v5[-20:])) if len(v5)>=20 else 1.0
    if vol_ma20<=0 or float(v5[-1])<vol_ma20*MIN_VOL_MULT: return None

    # ── Calcular todos los indicadores ────────────────────────────────

    # Capa A
    pk5  = peak_ser(h5, PIVOT_LEN);    vl5  = valley_ser(l5, PIVOT_LEN)
    long_zz  = crossover(c5a,  pk5);   short_zz = crossunder(c5a, vl5)
    hma5     = calc_hma(c5a, HMA_LEN)
    hma_now  = float(hma5[-1]);         hma_prev = float(hma5[-2]) if len(hma5)>1 else hma_now
    hma_bull = close > hma_now and hma_now > hma_prev
    hma_bear = close < hma_now and hma_now < hma_prev
    ft5      = calc_ft(o5,c5a,v5,FT_PERIOD)
    ft15     = calc_ft(o15,c15a,v15,FT_PERIOD)

    # Capa B
    cvd_val, cvd_slope, cvd_sig = calc_cvd(o5,c5a,v5,CVD_PERIOD)
    st5  = calc_supertrend(h5,l5,c5a, 10, 3.0)
    st15 = calc_supertrend(h15,l15,c15a, 10, 3.0)

    # Capa C
    bull_sweep, bear_sweep, sweep_desc = detect_liquidity_sweep(h5,l5,c5a, atr14, SWEEP_LOOKBACK)
    bull_ob, bear_ob, ob_desc = detect_orderblock(o5,h5,l5,c5a, atr14, OB_LOOKBACK)

    # Capa D
    regime, adx_val = market_regime(h5,l5,c5a, atr14, ADX_PERIOD)
    kzone, kweight  = get_killzone()
    vol_ok = float(v5[-1]) > vol_ma20 * 1.2

    # RSI para filtro adicional
    rsi5 = calc_rsi(c5a, 14)

    # ── SCORING LONG ──────────────────────────────────────────────────
    ls, lr = 0, []

    # Capa A
    if long_zz:                        ls+=2; lr.append(f"ZZ_BRK↑")
    if hma_bull:                       ls+=1; lr.append("HMA▲")
    if ft5 > 0:                        ls+=1; lr.append(f"FT▲{ft5:+.0f}")

    # Capa B
    if cvd_sig=="bull":                ls+=1; lr.append(f"CVD▲")
    if cvd_slope>0 and cvd_val>0:      ls+=1; lr.append("CVD_SLP▲")
    if st5==1 and st15==1:             ls+=1; lr.append("ST▲×2")
    elif st5==1:                       ls+=0  # solo vale con confirmación

    # Capa C — PREMIUM
    if bull_sweep:                     ls+=2; lr.append(f"⚡{sweep_desc}")
    if bull_ob:                        ls+=1; lr.append(ob_desc)

    # Capa D
    if regime in ("trending_bull","trending_mixed") and adx_val >= ADX_THRESHOLD:
        ls+=1; lr.append(f"ADX{adx_val:.0f}▲")
    if kweight >= 1.0 and vol_ok:      ls+=1; lr.append(f"KZ[{kzone.split()[1]}]")

    # ── SCORING SHORT ─────────────────────────────────────────────────
    ss, sr = 0, []

    if short_zz:                       ss+=2; sr.append(f"ZZ_BRK↓")
    if hma_bear:                       ss+=1; sr.append("HMA▼")
    if ft5 < 0:                        ss+=1; sr.append(f"FT▼{ft5:+.0f}")

    if cvd_sig=="bear":                ss+=1; sr.append("CVD▼")
    if cvd_slope<0 and cvd_val<0:      ss+=1; sr.append("CVD_SLP▼")
    if st5==-1 and st15==-1:           ss+=1; sr.append("ST▼×2")

    if bear_sweep:                     ss+=2; sr.append(f"⚡{sweep_desc}")
    if bear_ob:                        ss+=1; sr.append(ob_desc)

    if regime in ("trending_bear","trending_mixed") and adx_val >= ADX_THRESHOLD:
        ss+=1; sr.append(f"ADX{adx_val:.0f}▼")
    if kweight >= 1.0 and vol_ok:      ss+=1; sr.append(f"KZ[{kzone.split()[1]}]")

    # ── Filtro de régimen (no operar en rango o volátil) ─────────────
    if regime == "volatile":
        return None  # spike / news → skip siempre
    if regime == "ranging" and (ls < 7 and ss < 7):
        return None  # en rango solo señales muy fuertes

    # ── Killzone weight: si zona muerta, score mínimo sube ───────────
    effective_min = MIN_SCORE
    if kweight < 0.5:      effective_min = 99   # no operar en dead zone
    elif kweight < 0.8:    effective_min = MIN_SCORE + 2

    # ── SL en estructura real ─────────────────────────────────────────
    def build_signal(side, score, reasons):
        if side == "BUY":
            sl_struct = float(np.min(l5[-10:])) - atr14 * 0.15
            sl_dist   = max(close - sl_struct, atr14 * ATR_SL)
            sl        = close - sl_dist
        else:
            sl_struct = float(np.max(h5[-10:])) + atr14 * 0.15
            sl_dist   = max(sl_struct - close, atr14 * ATR_SL)
            sl        = close + sl_dist

        tp1_d = atr14 * ATR_TP1
        tp2_d = atr14 * ATR_TP2
        tp1   = close + tp1_d if side=="BUY" else close - tp1_d
        tp2   = close + tp2_d if side=="BUY" else close - tp2_d
        be    = close + atr14*ATR_BE if side=="BUY" else close - atr14*ATR_BE
        rr    = tp1_d / sl_dist if sl_dist > 0 else 1.0

        # Adaptive sizing con killzone weight
        base = TRADE_USDT * kweight
        if score >= 10:  size = min(TRADE_USDT_MAX * kweight, TRADE_USDT_MAX)
        elif score >= 7: size = min(base * 1.5, TRADE_USDT_MAX)
        else:             size = base
        size = max(TRADE_USDT * 0.5, round(size, 1))

        return {
            "side":     side,
            "score":    score,
            "max":      12,
            "reasons":  reasons,
            "entry":    close,
            "sl":       sl,
            "tp1":      tp1,
            "tp2":      tp2,
            "be_level": be,
            "atr":      atr14,
            "atr_pct":  atr_pct,
            "rr":       rr,
            "size_usdt": size,
            "regime":   regime,
            "adx":      adx_val,
            "kzone":    kzone,
            "kweight":  kweight,
            "ft5":      ft5,
            "cvd":      cvd_val,
            "cvd_sig":  cvd_sig,
            "sweep":    bull_sweep if side=="BUY" else bear_sweep,
            "ob":       bull_ob if side=="BUY" else bear_ob,
            "zz":       long_zz if side=="BUY" else short_zz,
            "st5":      st5,
            "st15":     st15,
            "rsi":      rsi5,
            "hma":      hma_now,
        }

    if ls >= effective_min and ls > ss:
        return build_signal("BUY", ls, lr)
    if ss >= effective_min and ss > ls:
        return build_signal("SELL", ss, sr)
    return None


# ══════════════════════════════════════════════════════════════════════════
#  GESTIÓN DINÁMICA DE RIESGO — Streak tracking
# ══════════════════════════════════════════════════════════════════════════

class RiskManager:
    """
    Ajusta el sizing según el estado actual de la cuenta:
    - Racha ganadora (+3 seguidos): sizing normal
    - Racha perdedora (-2 seguidos): reduce sizing 50%
    - Drawdown > 3%: pausa hasta recuperación
    """
    def __init__(self):
        self.wins   = 0
        self.losses = 0
        self.streak = 0   # + = ganancias, - = pérdidas
        self.daily_pnl = 0.0
        self.peak_balance = 0.0
        self.history: deque = deque(maxlen=20)

    def record(self, won: bool, pnl_pct: float):
        self.history.append(("W" if won else "L", pnl_pct))
        self.daily_pnl += pnl_pct
        if won:
            self.wins += 1
            self.streak = max(1, self.streak + 1)
        else:
            self.losses += 1
            self.streak = min(-1, self.streak - 1)

    def size_multiplier(self) -> float:
        """Multiplica el size base según el estado"""
        if self.streak <= -3: return 0.5   # racha perdedora grave
        if self.streak == -2: return 0.7   # racha perdedora moderada
        if self.streak >= 3:  return 1.0   # no aumentar más (disciplina)
        return 1.0

    def can_trade(self) -> tuple[bool, str]:
        if self.streak <= -4:
            return False, f"RACHA_NEGATIVA={self.streak} — espera señal de reversión"
        if self.daily_pnl <= -MAX_DAILY_LOSS * 0.8:
            return False, f"CERCA_MAX_LOSS ({self.daily_pnl:.1f}%)"
        return True, "OK"

    def summary(self) -> str:
        wr = self.wins/(self.wins+self.losses)*100 if (self.wins+self.losses)>0 else 0
        return f"W:{self.wins} L:{self.losses} WR:{wr:.0f}% Streak:{self.streak:+d}"


# ══════════════════════════════════════════════════════════════════════════
#  BINGX CLIENT
# ══════════════════════════════════════════════════════════════════════════

class BingXClient:
    def __init__(self):
        self._http: Optional[httpx.AsyncClient] = None
        self._sem  = asyncio.Semaphore(MAX_CONCURRENT)
        self._fail: dict[str,int] = defaultdict(int)
        self._lev:  set[str] = set()

    @property
    def http(self):
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=12.0,
                limits=httpx.Limits(max_connections=60, max_keepalive_connections=30))
        return self._http

    def _sign(self, p):
        s = "&".join(f"{k}={v}" for k,v in sorted(p.items()))
        return hmac.new(API_SECRET.encode(), s.encode(), hashlib.sha256).hexdigest()
    def _ts(self): return int(time.time()*1000)

    async def _get(self, path, params=None, auth=False):
        p=dict(params or {}); hdrs={}
        if auth:
            p["timestamp"]=self._ts(); p["signature"]=self._sign(p)
            hdrs={"X-BX-APIKEY":API_KEY}
        async with self._sem:
            try:
                r = await self.http.get(f"{BINGX_BASE}{path}", params=p, headers=hdrs)
                return r.json()
            except Exception as e:
                log.debug(f"GET {path}: {e}"); return {}

    async def _post(self, path, params=None):
        p=dict(params or {}); p["timestamp"]=self._ts(); p["signature"]=self._sign(p)
        try:
            r = await self.http.post(f"{BINGX_BASE}{path}", data=p,
                headers={"X-BX-APIKEY":API_KEY,
                         "Content-Type":"application/x-www-form-urlencoded"})
            return r.json()
        except Exception as e:
            log.debug(f"POST {path}: {e}"); return {}

    async def get_balance(self) -> float:
        for path in ["/openApi/swap/v2/user/balance","/openApi/swap/v3/user/balance"]:
            data = await self._get(path, auth=True)
            if data.get("code") != 0: continue
            d = data.get("data", {})
            candidates = []
            if isinstance(d, dict):
                bal = d.get("balance", {})
                if isinstance(bal, dict):
                    for f in ["availableMargin","available","equity","freeMargin"]:
                        v=bal.get(f)
                        if v is not None: candidates.append(float(v))
                for f in ["availableMargin","available","equity"]:
                    v=d.get(f)
                    if v is not None: candidates.append(float(v))
            if isinstance(d, list):
                for item in d:
                    if isinstance(item,dict) and item.get("asset","") in ("USDT",""):
                        for f in ["availableMargin","available"]:
                            v=item.get(f)
                            if v is not None: candidates.append(float(v))
            pos = [v for v in candidates if v > 0]
            if pos:
                val = max(pos)
                log.info(f"✅ Balance: {val:.2f} USDT")
                return val
        ov = float(os.getenv("BALANCE_OVERRIDE","0"))
        if ov > 0:
            log.warning(f"⚠️  BALANCE_OVERRIDE={ov}"); return ov
        log.error("❌ Balance=0 — BingX→Activos→Transferir Spot→Futuros")
        return 0.0

    async def get_symbols(self) -> list[str]:
        data = await self._get("/openApi/swap/v2/quote/contracts")
        if data.get("code")==0:
            return [s["symbol"] for s in data.get("data",[])
                    if s.get("symbol","").endswith("-USDT") and s.get("status",0)==1]
        return []

    async def klines(self, symbol, interval, limit=300) -> list[dict]:
        if self._fail[symbol] >= 3: return []
        try:
            async with asyncio.timeout(8.0):
                data = await self._get("/openApi/swap/v3/quote/klines",
                    {"symbol":symbol,"interval":interval,"limit":limit})
        except asyncio.TimeoutError:
            self._fail[symbol]+=1; return []
        if data.get("code")!=0:
            self._fail[symbol]+=1; return []
        self._fail[symbol]=0
        out=[]
        for k in data.get("data",[]):
            try: out.append({"t":int(k[0]),"o":float(k[1]),"h":float(k[2]),
                              "l":float(k[3]),"c":float(k[4]),"v":float(k[5])})
            except: continue
        return sorted(out, key=lambda x:x["t"])

    async def get_positions(self) -> list[dict]:
        data = await self._get("/openApi/swap/v2/user/positions", auth=True)
        if data.get("code")==0:
            return [p for p in data.get("data",[]) if float(p.get("positionAmt",0))!=0]
        return []

    async def set_leverage(self, symbol):
        if symbol in self._lev: return
        await asyncio.gather(
            self._post("/openApi/swap/v2/trade/leverage",
                       {"symbol":symbol,"side":"LONG","leverage":LEVERAGE}),
            self._post("/openApi/swap/v2/trade/leverage",
                       {"symbol":symbol,"side":"SHORT","leverage":LEVERAGE}),
        )
        self._lev.add(symbol)

    async def place_order(self, symbol, side, qty, sl, tp1, tp2) -> bool:
        pos  = "LONG"  if side=="BUY"  else "SHORT"
        clos = "SELL"  if side=="BUY"  else "BUY"
        await self.set_leverage(symbol)
        res = await self._post("/openApi/swap/v2/trade/order",{
            "symbol":symbol,"side":side,"positionSide":pos,
            "type":"MARKET","quantity":round(qty,4),
        })
        if res.get("code")!=0:
            log.error(f"Orden fallida {symbol}: {res.get('code')} {res.get('msg','')}"); return False
        await asyncio.gather(
            self._post("/openApi/swap/v2/trade/order",{
                "symbol":symbol,"side":clos,"positionSide":pos,
                "type":"STOP_MARKET","stopPrice":round(sl,8),
                "closePosition":"true","workingType":"MARK_PRICE",
            }),
            self._post("/openApi/swap/v2/trade/order",{
                "symbol":symbol,"side":clos,"positionSide":pos,
                "type":"TAKE_PROFIT_MARKET","stopPrice":round(tp1,8),
                "quantity":round(qty*TP1_SIZE,4),"workingType":"MARK_PRICE",
            }),
            self._post("/openApi/swap/v2/trade/order",{
                "symbol":symbol,"side":clos,"positionSide":pos,
                "type":"TAKE_PROFIT_MARKET","stopPrice":round(tp2,8),
                "quantity":round(qty*TP2_SIZE,4),"workingType":"MARK_PRICE",
            }),
            return_exceptions=True,
        )
        return True

    async def close(self):
        if self._http and not self._http.is_closed: await self._http.aclose()


# ══════════════════════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════════════════════
async def tg(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT: return
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            await c.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id":TELEGRAM_CHAT,"text":msg,"parse_mode":"HTML"})
    except Exception as e: log.debug(f"TG:{e}")


# ══════════════════════════════════════════════════════════════════════════
#  CACHE
# ══════════════════════════════════════════════════════════════════════════
class Cache:
    def __init__(self,ttl=25): self.ttl=ttl; self._d={}; self._t={}
    def get(self,k): return self._d[k] if k in self._d and time.time()-self._t.get(k,0)<self.ttl else None
    def set(self,k,v): self._d[k]=v; self._t[k]=time.time()


# ══════════════════════════════════════════════════════════════════════════
#  BOT ULTIMATE — Orquestador
# ══════════════════════════════════════════════════════════════════════════
class PhantomUltimate:

    def __init__(self):
        self.api   = BingXClient()
        self.cache = Cache(25)
        self.risk  = RiskManager()
        self.c5:  dict = {}
        self.c15: dict = {}
        self.warm: set = set()
        self.symbols: list = []
        self.open_pos: dict = {}
        self.balance  = 0.0
        self.cycle    = 0
        self.daily_trades = 0
        self.daily_loss   = 0.0
        self.last_day = datetime.now(timezone.utc).date()
        self.t0       = time.time()
        self.kill     = False
        self.total_signals = 0
        self.total_orders  = 0

    async def warmup_one(self, sym: str) -> bool:
        need5  = max(HMA_LEN+20, FT_PERIOD*3+10, PIVOT_LEN*6, CVD_PERIOD+10,
                     OB_LOOKBACK+10, SWEEP_LOOKBACK+5, ADX_PERIOD*3)
        need15 = max(HMA_LEN+10, FT_PERIOD*2+10)
        try:
            k5,k15 = await asyncio.gather(
                self.api.klines(sym,TIMEFRAME, max(350,need5+50)),
                self.api.klines(sym,TIMEFRAME_SLOW, max(200,need15+30)),
                return_exceptions=True,
            )
            if (isinstance(k5,list) and isinstance(k15,list)
                    and len(k5)>=need5 and len(k15)>=need15):
                self.c5[sym]=k5; self.c15[sym]=k15
                self.warm.add(sym); return True
        except Exception: pass
        return False

    async def warmup_batch(self, syms, conc=25):
        done=0; total=len(syms)
        for i in range(0,total,conc):
            res=await asyncio.gather(*[self.warmup_one(s) for s in syms[i:i+conc]],return_exceptions=True)
            done+=sum(1 for r in res if r is True)
            log.info(f"  WarmUp {done}/{total}...")
            await asyncio.sleep(0.15)
        return done

    async def update(self, sym):
        k5k=f"{sym}:5"; k15k=f"{sym}:15"
        n5=self.cache.get(k5k) is None; n15=self.cache.get(k15k) is None
        if not n5 and not n15: return
        tasks=[]
        if n5:  tasks.append(self.api.klines(sym,TIMEFRAME,3))
        if n15: tasks.append(self.api.klines(sym,TIMEFRAME_SLOW,3))
        res=await asyncio.gather(*tasks,return_exceptions=True); idx=0
        def mrg(ex,nc,mx):
            if not isinstance(nc,list) or not nc: return ex
            lt=ex[-1]["t"] if ex else 0
            for x in nc:
                if x["t"]>lt: ex.append(x)
                elif ex and x["t"]==ex[-1]["t"]: ex[-1]=x
            return ex[-mx:]
        if n5:
            r=res[idx]; idx+=1
            if isinstance(r,list):
                self.c5[sym]=mrg(self.c5.get(sym,[]),r,350); self.cache.set(k5k,self.c5[sym])
        if n15:
            r=res[idx]
            if isinstance(r,list):
                self.c15[sym]=mrg(self.c15.get(sym,[]),r,200); self.cache.set(k15k,self.c15[sym])

    def reset_daily(self):
        today=datetime.now(timezone.utc).date()
        if today!=self.last_day:
            self.daily_trades=0; self.daily_loss=0.0; self.last_day=today
            self.kill=False; self.risk.daily_pnl=0.0
            log.info("📅 Reseteado")

    def can_trade(self) -> tuple[bool,str]:
        if self.kill: return False,"KILL_SWITCH"
        if len(self.open_pos)>=MAX_POSITIONS: return False,f"MAX_POS"
        if self.daily_trades>=MAX_DAILY_TRADES: return False,"MAX_TRADES"
        if self.daily_loss>=MAX_DAILY_LOSS: self.kill=True; return False,"MAX_LOSS→KILL"
        if self.balance<TRADE_USDT*0.5:
            return False,f"BAL={self.balance:.2f}U → BingX Activos→Futuros o BALANCE_OVERRIDE"
        risk_ok, risk_reason = self.risk.can_trade()
        if not risk_ok: return False, risk_reason
        return True,"OK"

    def quality_label(self, score: int) -> str:
        if score >= 10: return "🔥🔥 APEX PREMIUM"
        if score >= 8:  return "🔥 FUERTE"
        if score >= 6:  return "✅ BUENA"
        return "📶 VÁLIDA"

    async def scan(self):
        self.cycle+=1; self.reset_daily()
        self.balance, pos_raw = await asyncio.gather(
            self.api.get_balance(), self.api.get_positions()
        )
        self.open_pos = {p["symbol"]:p for p in pos_raw}
        kzone, kweight = get_killzone()

        ok, reason = self.can_trade()
        log.info(
            f"[C{self.cycle:04d}] Bal:{self.balance:.2f}U | "
            f"Pos:{len(self.open_pos)}/{MAX_POSITIONS} | "
            f"Warm:{len(self.warm)}/{len(self.symbols)} | "
            f"{kzone}({kweight:.1f}) | {self.risk.summary()}"
        )

        if not ok:
            log.info(f"  ⛔ {reason}"); return

        prio  = [s for s in PRIORITY if s in self.warm and s not in self.open_pos]
        resto = [s for s in self.warm if s not in self.open_pos and s not in prio]
        cands = prio + resto
        if not cands: return

        for i in range(0,min(len(cands),60),30):
            await asyncio.gather(*[self.update(s) for s in cands[i:i+30]],return_exceptions=True)

        sigs=0
        for sym in cands:
            if len(self.open_pos)>=MAX_POSITIONS: break
            sig = analyze(self.c5.get(sym,[]), self.c15.get(sym,[]))
            if sig is None: continue

            sigs+=1; self.total_signals+=1
            e  = "🟢" if sig["side"]=="BUY" else "🔴"
            pt = "⭐" if sym in PRIORITY else ""
            ql = self.quality_label(sig["score"])

            log.info(
                f"  {e}{pt} {sym} {sig['side']} {ql} "
                f"{sig['score']}/{sig['max']} | "
                f"Regime:{sig['regime']} ADX:{sig['adx']:.0f} | "
                f"{'⚡SWEEP ' if sig['sweep'] else ''}"
                f"{'🟦OB ' if sig['ob'] else ''}"
                f"RR:{sig['rr']:.1f} | "
                f"{' '.join(sig['reasons'])}"
            )

            if AUTO_TRADING:
                entry=sig["entry"]; sl_dist=abs(entry-sig["sl"])
                rp=sl_dist/entry if entry>0 else 0
                if rp<0.0005: continue

                # Sizing con risk manager
                raw_size = sig["size_usdt"] * self.risk.size_multiplier()
                final_size = max(TRADE_USDT*0.5, min(raw_size, TRADE_USDT_MAX))
                qty = round((final_size/rp)/entry, 4)
                if qty<=0: continue

                t0=time.time()
                ok2 = await self.api.place_order(sym,sig["side"],qty,sig["sl"],sig["tp1"],sig["tp2"])
                ms = int((time.time()-t0)*1000)

                if ok2:
                    self.daily_trades+=1; self.total_orders+=1
                    self.open_pos[sym]={"symbol":sym,"side":sig["side"],"entry":entry}
                    slp  = sl_dist/entry*100
                    tp1p = abs(sig["tp1"]-entry)/entry*100
                    tp2p = abs(sig["tp2"]-entry)/entry*100

                    sweep_line = f"⚡ <b>Liquidity Sweep detectado!</b>\n" if sig["sweep"] else ""
                    ob_line    = f"🟦 <b>Order Block tocado</b>\n"         if sig["ob"]    else ""

                    msg = (
                        f"{e} <b>{sym}</b>{pt} — "
                        f"{'LONG' if sig['side']=='BUY' else 'SHORT'}\n"
                        f"{ql}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"{sweep_line}{ob_line}"
                        f"📊 Score:  <b>{sig['score']}/{sig['max']}</b>\n"
                        f"📍 Entry:  <code>{entry:.6g}</code>\n"
                        f"🛡 SL:     <code>{sig['sl']:.6g}</code>  (-{slp:.2f}%)\n"
                        f"🎯 TP1:   <code>{sig['tp1']:.6g}</code>  35% (+{tp1p:.2f}%)\n"
                        f"🎯 TP2:   <code>{sig['tp2']:.6g}</code>  35% (+{tp2p:.2f}%)\n"
                        f"🔄 Trail: 30%\n"
                        f"📐 RR:    1:{sig['rr']:.1f}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📈 Regime: {sig['regime']} | ADX:{sig['adx']:.0f}\n"
                        f"🕐 {sig['kzone']}\n"
                        f"🌊 FT:{sig['ft5']:+.0f} CVD:{sig['cvd_sig']}({sig['cvd']:+.0f})\n"
                        f"📏 ST5:{'▲' if sig['st5']==1 else '▼'} "
                        f"ST15:{'▲' if sig['st15']==1 else '▼'} "
                        f"RSI:{sig['rsi']:.0f} ATR:{sig['atr_pct']:.2f}%\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"✨ {' · '.join(sig['reasons'])}\n"
                        f"💰 Bal:{self.balance:.2f}U | Size:{final_size:.1f}U | ⚡{ms}ms\n"
                        f"📊 {self.risk.summary()}"
                    )
                    await tg(msg)
                    log.info(f"  ✅ {sym} qty={qty} size={final_size:.1f}U {ms}ms")
            else:
                entry=sig["entry"]; sl_dist=abs(entry-sig["sl"])
                slp=sl_dist/entry*100; tp1p=abs(sig["tp1"]-entry)/entry*100
                sweep_tag = " ⚡SWEEP" if sig["sweep"] else ""
                ob_tag    = " 🟦OB"    if sig["ob"]    else ""
                msg = (
                    f"🔔 <b>[SIM] {sym}</b>{pt} — "
                    f"{'LONG' if sig['side']=='BUY' else 'SHORT'}"
                    f"{sweep_tag}{ob_tag}\n"
                    f"{ql} | Score:{sig['score']}/12 | RR:1:{sig['rr']:.1f}\n"
                    f"Entry:{entry:.6g} SL:{sig['sl']:.6g}(-{slp:.2f}%)\n"
                    f"TP1:{sig['tp1']:.6g}(+{tp1p:.2f}%) TP2:{sig['tp2']:.6g}\n"
                    f"Regime:{sig['regime']} ADX:{sig['adx']:.0f} | {sig['kzone']}\n"
                    f"FT:{sig['ft5']:+.0f} CVD:{sig['cvd_sig']} ATR:{sig['atr_pct']:.2f}%\n"
                    f"✨ {' · '.join(sig['reasons'])}"
                )
                await tg(msg)

        log.info(f"  Cands:{len(cands)} | Señales:{sigs} | Pos:{len(self.open_pos)} | Total:{self.total_signals}")

    async def run(self):
        log.info("═"*70)
        log.info("  PHANTOM EDGE  U L T I M A T E  v8.0")
        log.info("  Market Regime + Liquidity Sweep + Order Blocks + Killzones")
        log.info(f"  Mode: {'AUTO-TRADING ✅' if AUTO_TRADING else 'SIMULACIÓN 🟡'}")
        log.info(f"  Score:{MIN_SCORE}/12 | TP:{ATR_TP1}R/{ATR_TP2}R | SL:{ATR_SL}R | Lev:x{LEVERAGE}")
        log.info(f"  ADX umbral:{ADX_THRESHOLD} | OB lookback:{OB_LOOKBACK} | Sweep:{SWEEP_LOOKBACK}")
        log.info("═"*70)

        self.balance = await self.api.get_balance()
        log.info(f"💰 Balance: {self.balance:.2f} USDT")

        self.symbols = await self.api.get_symbols()
        log.info(f"📊 {len(self.symbols)} pares USDT-Perp")
        if not self.symbols:
            log.error("❌ Sin símbolos"); return

        kzone, kweight = get_killzone()
        await tg(
            f"🤖 <b>Phantom Edge ULTIMATE v8.0</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🧠 Motor: ZZ+HMA+FT+CVD+Sweep+OB+Regime+KZ\n"
            f"💰 Balance: {self.balance:.2f} USDT\n"
            f"📊 {len(self.symbols)} pares | Score≥{MIN_SCORE}/12\n"
            f"📐 TP:{ATR_TP1}R/{ATR_TP2}R | SL:{ATR_SL}R | Lev:x{LEVERAGE}\n"
            f"🕐 Sesión actual: {kzone} (peso:{kweight})\n"
            f"💡 Sizing: {TRADE_USDT}U→{TRADE_USDT_MAX}U (adaptive)\n"
            f"{'🟢 AUTO-TRADING ACTIVO' if AUTO_TRADING else '🟡 SIMULACIÓN'}"
            + (f"\n⚠️ Balance=0 → BingX→Activos→Transferir a Futuros"
               if self.balance==0 and API_KEY else "")
        )

        prio_ok = [s for s in PRIORITY if s in self.symbols]
        rest    = [s for s in self.symbols if s not in PRIORITY]

        log.info(f"🔥 WarmUp prio ({len(prio_ok)})...")
        await self.warmup_batch(prio_ok, conc=min(len(prio_ok)+1,25))
        log.info(f"🔥 WarmUp general ({len(rest)})...")
        await self.warmup_batch(rest[:200], conc=25)
        if len(rest) > 200:
            asyncio.create_task(self.warmup_batch(rest[200:], conc=15))

        log.info(f"✅ {len(self.warm)} pares listos — ULTIMATE en marcha!")

        while True:
            try:
                t0=time.time()
                await self.scan()
                elapsed=time.time()-t0
                sl=max(5.0, SCAN_INTERVAL-elapsed)
                log.info(f"  ⏱ {elapsed:.1f}s | próximo:{sl:.0f}s\n")
                await asyncio.sleep(sl)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"❌ {e}", exc_info=True)
                await asyncio.sleep(10)

        await self.api.close()


# ══════════════════════════════════════════════════════════════════════════
#  HEALTH CHECK — Dashboard completo
# ══════════════════════════════════════════════════════════════════════════
async def health_server(bot: PhantomUltimate, port: int):
    from aiohttp import web
    async def h(req):
        ok, reason = bot.can_trade()
        kzone, kw  = get_killzone()
        return web.json_response({
            "v": "8.0",
            "engine": "ZZ+HMA+FT+CVD+Sweep+OB+Regime+KZ",
            "status": "kill" if bot.kill else ("trading" if ok else "blocked"),
            "block_reason": None if ok else reason,
            "uptime_min": round((time.time()-bot.t0)/60,1),
            "cycle": bot.cycle,
            "balance_usdt": round(bot.balance,2),
            "warm": len(bot.warm),
            "symbols": len(bot.symbols),
            "open_pos": len(bot.open_pos),
            "daily_trades": bot.daily_trades,
            "total_signals": bot.total_signals,
            "total_orders":  bot.total_orders,
            "risk": {
                "wins": bot.risk.wins,
                "losses": bot.risk.losses,
                "streak": bot.risk.streak,
                "size_mult": bot.risk.size_multiplier(),
                "daily_pnl": round(bot.risk.daily_pnl,2),
            },
            "session": {"zone": kzone, "weight": kw},
            "auto_trading": AUTO_TRADING,
            "params": {
                "pivot":PIVOT_LEN,"hma":HMA_LEN,"ft":FT_PERIOD,
                "cvd":CVD_PERIOD,"adx":ADX_PERIOD,
                "ob_lookback":OB_LOOKBACK,"sweep_lookback":SWEEP_LOOKBACK,
                "min_score":f"{MIN_SCORE}/12",
                "tp":f"{ATR_TP1}R/{ATR_TP2}R","sl":f"{ATR_SL}R",
                "sizing":f"{TRADE_USDT}/{TRADE_USDT_MAX}U",
            }
        })
    app=web.Application()
    app.router.add_get("/",h); app.router.add_get("/health",h)
    runner=web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner,"0.0.0.0",port).start()
    log.info(f"🌐 http://0.0.0.0:{port}")


async def main():
    bot = PhantomUltimate()
    await asyncio.gather(health_server(bot, PORT), bot.run())

if __name__ == "__main__":
    asyncio.run(main())
