"""
ZigZag Channel Fade — Motor de señales
Basado en investigación: 3m genera 7-10 rupturas/día, con trigger 0.5×ATR → 1-2 señales netas
Canal tiene ~3-4×ATR de ancho → RR real ~1.5:1 con SL de 2×ATR
"""
import logging
import numpy as np
from typing import Optional, Tuple, List
import config

log = logging.getLogger("strategy")


def parse_klines(raw: list) -> Tuple[np.ndarray,...]:
    if not raw:
        return (np.array([]),)*5
    O,H,L,C,V=[],[],[],[],[]
    for k in raw:
        try:
            if isinstance(k, dict):
                o=float(k.get("open",  k.get("o",0)))
                h=float(k.get("high",  k.get("h",0)))
                l=float(k.get("low",   k.get("l",0)))
                c=float(k.get("close", k.get("c",0)))
                v=float(k.get("volume",k.get("v",0)))
            elif isinstance(k,(list,tuple)) and len(k)>=6:
                o,h,l,c,v=float(k[1]),float(k[2]),float(k[3]),float(k[4]),float(k[5])
            else: continue
            if h<=0 or c<=0 or h<l: continue
            O.append(o);H.append(h);L.append(l);C.append(c);V.append(v)
        except: continue
    return np.array(O),np.array(H),np.array(L),np.array(C),np.array(V)


def calc_atr(H,L,C,p=14):
    if len(C)<p+1: return 0.0
    tr=np.maximum(H[1:]-L[1:],np.maximum(np.abs(H[1:]-C[:-1]),np.abs(L[1:]-C[:-1])))
    v=np.mean(tr[:p])
    for i in range(p,len(tr)): v=(v*(p-1)+tr[i])/p
    return float(v)

def calc_ema(C,p):
    if len(C)<2: return np.full(len(C),float(C[0]))
    e=np.empty(len(C)); e[0]=float(C[0]); k=2.0/(p+1)
    for i in range(1,len(C)): e[i]=C[i]*k+e[i-1]*(1-k)
    return e

def calc_rsi(C,p=14):
    if len(C)<p+1: return 50.0
    d=np.diff(C.astype(float))
    g=np.where(d>0,d,0.); l=np.where(d<0,-d,0.)
    ag=np.mean(g[:p]); al=np.mean(l[:p])
    for i in range(p,len(d)):
        ag=(ag*(p-1)+g[i])/p; al=(al*(p-1)+l[i])/p
    return 100. if al==0 else float(100-100/(1+ag/al))

def find_pivots(H,L,pl):
    ph,plv=[],[]
    n=len(H)
    for i in range(pl,n-pl):
        if H[i]>=np.max(H[i-pl:i+pl+1]): ph.append((float(H[i]),i))
        if L[i]<=np.min(L[i-pl:i+pl+1]): plv.append((float(L[i]),i))
    return ph,plv


class ChannelFadeSignal:
    """
    Reglas base (siempre activas):
      SHORT: close >= verde + ATR×trigger  →  TP=rojo,  SL=entry+2×ATR
      LONG:  close <= rojo  - ATR×trigger  →  TP=verde, SL=entry-2×ATR
    
    Filtros opcionales (via env var):
      USE_EMA_FILTER  → SHORT solo si close<EMA, LONG solo si close>EMA
      USE_RSI_FILTER  → SHORT si RSI>60, LONG si RSI<40
      USE_VOL_FILTER  → solo si vol > 1.3×media20
    """

    def compute(self, opens, highs, lows, closes, volumes) -> Optional[dict]:
        n = len(closes)
        MIN = config.PIVOT_LEN*2 + config.ATR_LEN + 3
        if n < MIN:
            log.debug(f"✗ pocas velas {n}<{MIN}")
            return None

        # Excluir última vela (BingX siempre incluye la vela en curso)
        H=highs[:-1]; L=lows[:-1]; C=closes[:-1]; O=opens[:-1]; V=volumes[:-1]
        if len(C) < MIN-1: return None

        # ── ATR ──────────────────────────────────────────────────────
        atr = calc_atr(H,L,C,config.ATR_LEN)
        if atr <= 0:
            log.debug("✗ ATR=0")
            return None

        # ── Canal ZigZag ─────────────────────────────────────────────
        ph,plv = find_pivots(H,L,config.PIVOT_LEN)
        if not ph or not plv:
            log.debug(f"✗ sin pivots: ph={len(ph)} plv={len(plv)}")
            return None

        green = ph[-1][0]   # último máximo local confirmado
        red   = plv[-1][0]  # último mínimo local confirmado

        if green <= red:
            log.debug(f"✗ canal inválido green={green:.4g}<=red={red:.4g}")
            return None

        canal = green - red
        if canal < atr * config.MIN_CANAL_ATR:
            log.debug(f"✗ canal estrecho {canal:.4g}<{atr*config.MIN_CANAL_ATR:.4g}")
            return None

        close = float(C[-1])
        short_trig = green + atr * config.ATR_TRIGGER_MULT
        long_trig  = red   - atr * config.ATR_TRIGGER_MULT

        # ── Filtros opcionales ────────────────────────────────────────
        rsi = 50.0
        if config.USE_RSI_FILTER:
            rsi = calc_rsi(C, config.RSI_PERIOD)

        ema_val = 0.0
        if config.USE_EMA_FILTER:
            ema_val = float(calc_ema(C, config.EMA_PERIOD)[-1])

        vol_ratio = 1.0
        if config.USE_VOL_FILTER:
            vm = np.mean(V[-20:]) if len(V)>=20 else np.mean(V)
            vol_ratio = float(V[-1]/vm) if vm>0 else 1.0
            if vol_ratio < config.VOL_MULT:
                log.debug(f"✗ vol bajo {vol_ratio:.2f}x<{config.VOL_MULT}x")
                return None

        # ── SHORT ─────────────────────────────────────────────────────
        if close >= short_trig:
            ema_ok  = (not config.USE_EMA_FILTER) or (close < ema_val)
            rsi_ok  = (not config.USE_RSI_FILTER) or (rsi > config.RSI_SHORT_MIN)
            if ema_ok and rsi_ok:
                sl = close + atr * config.SL_ATR_MULT
                tp = red
                if tp < close < sl and (close - tp) > 0:
                    rr = abs(tp-close)/abs(sl-close)
                    log.info(f"🔴 SHORT green={green:.6g} trig={short_trig:.6g} "
                             f"close={close:.6g} canal={canal:.4g} ATR={atr:.4g} RR=1:{rr:.1f}"
                             + (f" RSI={rsi:.1f}" if config.USE_RSI_FILTER else "")
                             + (f" EMA={ema_val:.4g}" if config.USE_EMA_FILTER else ""))
                    return dict(side="SELL", entry=close, sl=sl, tp=tp, atr=atr,
                                green=green, red=red, canal_width=canal,
                                trigger=short_trig, rsi=rsi, vol_ratio=vol_ratio)
            else:
                log.debug(f"✗ SHORT filtrado ema_ok={ema_ok} rsi_ok={rsi_ok} RSI={rsi:.1f}")

        # ── LONG ──────────────────────────────────────────────────────
        if close <= long_trig:
            ema_ok  = (not config.USE_EMA_FILTER) or (close > ema_val)
            rsi_ok  = (not config.USE_RSI_FILTER) or (rsi < config.RSI_LONG_MAX)
            if ema_ok and rsi_ok:
                sl = close - atr * config.SL_ATR_MULT
                tp = green
                if sl < close < tp and (tp - close) > 0:
                    rr = abs(tp-close)/abs(close-sl)
                    log.info(f"🟢 LONG red={red:.6g} trig={long_trig:.6g} "
                             f"close={close:.6g} canal={canal:.4g} ATR={atr:.4g} RR=1:{rr:.1f}"
                             + (f" RSI={rsi:.1f}" if config.USE_RSI_FILTER else "")
                             + (f" EMA={ema_val:.4g}" if config.USE_EMA_FILTER else ""))
                    return dict(side="BUY", entry=close, sl=sl, tp=tp, atr=atr,
                                green=green, red=red, canal_width=canal,
                                trigger=long_trig, rsi=rsi, vol_ratio=vol_ratio)
            else:
                log.debug(f"✗ LONG filtrado ema_ok={ema_ok} rsi_ok={rsi_ok} RSI={rsi:.1f}")

        log.debug(f"· {close:.6g} | verde={green:.6g}(+ATR→{short_trig:.6g}) "
                  f"rojo={red:.6g}(-ATR→{long_trig:.6g}) dentro_canal={red<close<green}")
        return None


class ExplosionScorer:
    def score(self, ticker, daily_klines):
        try:
            pc = abs(float(ticker.get("priceChangePercent",0)))
            qv = float(ticker.get("quoteVolume",0))
            vs = 1.0
            if len(daily_klines)>=2:
                def _v(k): return float(k.get("volume",0)) if isinstance(k,dict) else (float(k[5]) if isinstance(k,(list,tuple)) and len(k)>5 else 0.)
                avg=np.mean([_v(k) for k in daily_klines[:-1]])
                vs=_v(daily_klines[-1])/avg if avg>0 else 1.
            return pc*2 + vs*3 + min(qv/1e7,5)
        except: return 0.0
