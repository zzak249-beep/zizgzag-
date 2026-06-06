"""
QF×JP Crypto Bot — BingX Full Scanner + Telegram
v4.3 — escanea TODOS los pares BingX con concurrencia máxima
       + ranking por score + filtros de calidad mejorados
       + gestión de riesgo avanzada
       + FIX: _sign() usa urllib.parse.urlencode (sin sorted) — BingX requiere orden original
"""
import asyncio, logging, math, os, time, hmac, hashlib, urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional
import aiohttp
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
log = logging.getLogger("qfjp_bot")

# ══════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════
BINGX_API_KEY    = os.environ["BINGX_API_KEY"]
BINGX_API_SECRET = os.environ["BINGX_API_SECRET"]
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET",    "qfjp_secret_2025")
TRADE_SIZE_USDT  = float(os.environ.get("TRADE_SIZE_USDT", "10"))
MAX_OPEN_TRADES  = int(os.environ.get("MAX_OPEN_TRADES",   "3"))
SL_PCT           = float(os.environ.get("SL_PCT",          "1.5"))
TP_PCT           = float(os.environ.get("TP_PCT",          "3.0"))
MIN_SIGNAL_LEVEL = os.environ.get("MIN_SIGNAL_LEVEL",  "HUNT_LONG")
KLINE_INTERVAL   = os.environ.get("KLINE_INTERVAL",    "3m")
HTF_INTERVAL     = os.environ.get("HTF_INTERVAL",      "15m")
SCAN_INTERVAL    = int(os.environ.get("SCAN_INTERVAL",  "180"))

# Volumen mínimo — bajo para capturar más pares
MIN_VOLUME_USDT  = float(os.environ.get("MIN_VOLUME_USDT", "200000"))
# Máximo de pares a escanear (0 = todos)
MAX_SYMBOLS      = int(os.environ.get("MAX_SYMBOLS", "0"))
# Concurrencia — cuántos pares en paralelo
CONCURRENCY      = int(os.environ.get("CONCURRENCY", "20"))

# Parámetros señal
NORM_SCORE_TH    = float(os.environ.get("NORM_SCORE_TH",  "0.08"))
EXEC_BPT         = float(os.environ.get("EXEC_BPT",       "1.50"))
DECAY_TH         = float(os.environ.get("DECAY_TH",       "0.35"))
ASYM_RATIO       = float(os.environ.get("ASYM_RATIO",     "0.00"))
HL_MIN_COUNT     = int(os.environ.get("HL_MIN_COUNT",     "0"))
PIVOT_LEFT       = int(os.environ.get("PIVOT_LEFT",       "3"))
PIVOT_RIGHT      = int(os.environ.get("PIVOT_RIGHT",      "2"))
TL_REQUIRED      = os.environ.get("TL_REQUIRED",  "false").lower() == "true"
DP_REQUIRED      = os.environ.get("DP_REQUIRED",  "false").lower() == "true"
DEBUG_SIGNALS    = os.environ.get("DEBUG_SIGNALS", "true").lower() == "true"

# Filtro de calidad mínima para ejecutar orden (no solo detectar)
MIN_SCORE_TO_TRADE = float(os.environ.get("MIN_SCORE_TO_TRADE", "0.15"))
MIN_DECAY_TO_TRADE = float(os.environ.get("MIN_DECAY_TO_TRADE", "0.40"))

# Símbolos manuales (override)
_raw = os.environ.get("SYMBOLS","").strip()
SYMBOLS_OVERRIDE = [
    s.strip() for s in _raw.split(",")
    if s.strip() and "-" in s.strip()
    and s.strip().upper().endswith("-USDT")
    and "=" not in s.strip()
] if _raw else []

BINGX_BASE = "https://open-api.bingx.com"

SIGNAL_RANK = {
    "HUNT_LONG":1, "HUNT_SHORT":1,
    "LONG_STD":1,  "SHORT_STD":1,
    "LONG_FUEL":2, "SHORT_FUEL":2,
    "LONG_SUP":3,  "SHORT_SUP":3,
    "LONG_SUP_V3":4,"SHORT_SUP_V3":4,
}
MIN_RANK = SIGNAL_RANK.get(MIN_SIGNAL_LEVEL, 1)

open_trades: dict[str,dict] = {}
SYMBOLS: list[str] = []
last_scan_results: dict[str,dict] = {}   # último resultado de cada par
scan_stats = {"cycles":0,"signals_total":0,"trades_opened":0,"errors":0}

# ══════════════════════════════════════════════════
#  BINGX — FIX: _sign sin sorted (BingX requiere orden original)
# ══════════════════════════════════════════════════
def _sign(params: dict) -> str:
    """
    HMAC-SHA256 sobre los parámetros en el orden en que se pasan.
    BingX NO acepta params ordenados alfabéticamente — usar urlencode sin sorted.
    """
    query_string = urllib.parse.urlencode(params)
    return hmac.new(
        BINGX_API_SECRET.encode(),
        query_string.encode(),
        hashlib.sha256
    ).hexdigest()

def _h()->dict:
    return {"X-BX-APIKEY":BINGX_API_KEY,"Content-Type":"application/json"}

async def bx_get(path:str,params:dict,session:aiohttp.ClientSession)->dict:
    p=dict(params)
    p["timestamp"]=int(time.time()*1000)
    p["signature"]=_sign(p)
    async with session.get(BINGX_BASE+path,params=p,headers=_h(),
                           timeout=aiohttp.ClientTimeout(total=12)) as r:
        d=await r.json()
    if d.get("code",0)!=0: raise Exception(f"GET {path}: {d.get('msg',d)}")
    return d

async def bx_post(path:str,body:dict)->dict:
    b=dict(body)
    b["timestamp"]=int(time.time()*1000)
    b["signature"]=_sign(b)
    async with aiohttp.ClientSession() as s:
        async with s.post(BINGX_BASE+path,json=b,headers=_h(),
                          timeout=aiohttp.ClientTimeout(total=12)) as r:
            d=await r.json()
    if d.get("code",0)!=0: raise Exception(f"POST {path}: {d.get('msg',d)}")
    return d

async def get_klines_s(sym:str,interval:str,limit:int,
                        session:aiohttp.ClientSession)->list[dict]:
    """Obtiene velas reutilizando sesión HTTP (más eficiente)."""
    p={"symbol":sym,"interval":interval,"limit":str(limit),
       "timestamp":int(time.time()*1000)}
    p["signature"]=_sign(p)
    async with session.get(BINGX_BASE+"/openApi/swap/v3/quote/klines",
                           params=p,headers=_h(),
                           timeout=aiohttp.ClientTimeout(total=12)) as r:
        d=await r.json()
    if d.get("code",0)!=0: raise Exception(f"klines {sym}: {d.get('msg',d)}")
    return [{"open":float(c[1]),"high":float(c[2]),"low":float(c[3]),
             "close":float(c[4]),"volume":float(c[5])}
            for c in d.get("data",[])]

async def get_all_symbols()->list[str]:
    """
    Obtiene TODOS los pares USDT perpetuos de BingX activos.
    Filtra por volumen mínimo y ordena por volumen desc.
    """
    try:
        async with aiohttp.ClientSession() as s:
            # Contratos disponibles
            async with s.get(BINGX_BASE+"/openApi/swap/v2/quote/contracts",
                headers={"Content-Type":"application/json"},
                timeout=aiohttp.ClientTimeout(total=20)) as r:
                d=await r.json()
            pairs=[c["symbol"] for c in d.get("data",[])
                   if c.get("symbol","").endswith("-USDT")
                   and c.get("status",1)==1]

            # Ticker 24h para filtrar por volumen
            async with s.get(BINGX_BASE+"/openApi/swap/v2/quote/ticker",
                headers={"Content-Type":"application/json"},
                timeout=aiohttp.ClientTimeout(total=20)) as r:
                td=await r.json()

        tickers={t["symbol"]:float(t.get("quoteVolume",0))
                 for t in td.get("data",[])
                 if t.get("symbol","").endswith("-USDT")}

        filtered=[(s,tickers.get(s,0)) for s in pairs
                  if tickers.get(s,0)>=MIN_VOLUME_USDT]
        filtered.sort(key=lambda x:x[1],reverse=True)

        result=[s for s,_ in filtered]
        if MAX_SYMBOLS>0:
            result=result[:MAX_SYMBOLS]

        log.info(f"Pares disponibles: {len(pairs)} total | "
                 f"{len(filtered)} con vol≥{MIN_VOLUME_USDT/1e3:.0f}K | "
                 f"Escaneando: {len(result)}")
        return result
    except Exception as e:
        log.error(f"Error obteniendo pares: {e}")
        return ["BTC-USDT","ETH-USDT","SOL-USDT","BNB-USDT","XRP-USDT",
                "DOGE-USDT","ADA-USDT","AVAX-USDT","LINK-USDT","TAO-USDT"]

async def get_price(sym:str)->float:
    async with aiohttp.ClientSession() as s:
        d=await bx_get("/openApi/swap/v2/quote/price",{"symbol":sym},s)
    return float(d["data"]["price"])

async def get_balance()->float:
    async with aiohttp.ClientSession() as s:
        d=await bx_get("/openApi/swap/v2/user/balance",{},s)
    # Manejar múltiples estructuras de respuesta de BingX
    raw=d.get("data",{})
    # Estructura 1: {"data":{"balance":[{"asset":"USDT","availableMargin":"..."}]}}
    if isinstance(raw,dict) and "balance" in raw:
        for item in raw["balance"]:
            if item.get("asset")=="USDT":
                return float(item.get("availableMargin",0))
    # Estructura 2: {"data":[{"asset":"USDT",...}]}
    if isinstance(raw,list):
        for item in raw:
            if item.get("asset")=="USDT":
                return float(item.get("availableMargin",0))
    # Estructura 3: {"data":{"USDT":{"availableMargin":"..."}}}
    if isinstance(raw,dict) and "USDT" in raw:
        return float(raw["USDT"].get("availableMargin",0))
    log.warning(f"get_balance: estructura desconocida: {str(raw)[:200]}")
    return 0.0

async def get_open_pos(sym:str)->Optional[dict]:
    try:
        async with aiohttp.ClientSession() as s:
            d=await bx_get("/openApi/swap/v2/user/positions",{"symbol":sym},s)
        for p in d.get("data",[]):
            if float(p.get("positionAmt",0))!=0: return p
        return None
    except: return None

async def place_order(sym:str,side:str,qty:float,sl:float,tp:float)->dict:
    return await bx_post("/openApi/swap/v2/trade/order",{
        "symbol":sym,"side":side,
        "positionSide":"LONG" if side=="BUY" else "SHORT",
        "type":"MARKET","quantity":str(qty),
        "stopLossPrice":str(round(sl,4)),
        "takeProfitPrice":str(round(tp,4)),})

async def close_pos(sym:str,side:str,qty:float)->dict:
    return await bx_post("/openApi/swap/v2/trade/order",{
        "symbol":sym,"side":"SELL" if side=="BUY" else "BUY",
        "positionSide":"LONG" if side=="BUY" else "SHORT",
        "type":"MARKET","quantity":str(qty),"reduceOnly":"true",})

# ══════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════
_tg_queue: asyncio.Queue = None  # type: ignore

async def tg(msg:str)->None:
    if _tg_queue:
        await _tg_queue.put(msg)

async def tg_worker()->None:
    """Envía mensajes Telegram en cola para no saturar la API."""
    global _tg_queue
    _tg_queue = asyncio.Queue()
    url=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    while True:
        msg=await _tg_queue.get()
        try:
            async with aiohttp.ClientSession() as s:
                await s.post(url,json={"chat_id":TELEGRAM_CHAT_ID,
                    "text":msg,"parse_mode":"HTML"},
                    timeout=aiohttp.ClientTimeout(total=10))
        except Exception as e:
            log.error(f"Telegram: {e}")
        await asyncio.sleep(0.4)  # max ~2.5 msg/s (límite Telegram)

def fmt_open(sig,sym,price,sl,tp,qty,score,decay):
    e="🟢" if "LONG" in sig else "🔴"
    st="★"*SIGNAL_RANK.get(sig,1)
    rr=round(abs(tp-price)/abs(price-sl),1) if price!=sl else 0
    return(f"{e} <b>{sig}</b> {st}\n"
           f"📊 <b>{sym}</b> @ <code>{price}</code>\n"
           f"📐 <code>{qty}</code> (~<code>{round(qty*price,1)}</code> USDT)\n"
           f"🛑 SL:<code>{sl}</code>  🎯 TP:<code>{tp}</code>  R/R:<code>1:{rr}</code>\n"
           f"🔬 Score:<code>{score:.3f}</code>  Decay:<code>{decay:.0f}%</code>\n"
           f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}")

def fmt_close(sym,pnl,reason,sig=""):
    e="✅" if pnl>0 else "❌"
    return f"{e} <b>CERRADO</b> {sym} PnL:<code>{pnl:+.2f}%</code> | {reason}"

def fmt_cycle_summary(cycle,n_sym,n_sig,elapsed,top_signals):
    if not top_signals: return None
    lines=[f"📡 <b>Ciclo #{cycle}</b> — {n_sym} pares en {elapsed:.0f}s\n"
           f"Señales: <b>{n_sig}</b>"]
    for sym,sig,score,decay in top_signals[:5]:
        e="🟢" if "LONG" in sig else "🔴"
        lines.append(f"  {e} <b>{sym}</b> {sig} s={score:.2f} d={decay:.0f}%")
    return "\n".join(lines)

# ══════════════════════════════════════════════════
#  INDICADORES
# ══════════════════════════════════════════════════
def sma(d,p):
    r=[float("nan")]*len(d)
    for i in range(p-1,len(d)): r[i]=sum(d[i-p+1:i+1])/p
    return r

def ema(d,p):
    r=[float("nan")]*len(d); k=2/(p+1)
    for i in range(len(d)):
        prev=r[i-1] if i>0 else float("nan")
        r[i]=(d[i]*k+prev*(1-k)) if prev==prev else (d[i] if d[i]==d[i] else 0.0)
    return r

def stdev(d,p):
    r=[float("nan")]*len(d)
    for i in range(p-1,len(d)):
        w=d[i-p+1:i+1]
        if any(x!=x for x in w): continue
        m=sum(w)/p; r[i]=(sum((x-m)**2 for x in w)/p)**0.5
    return r

def atr_f(h,l,c,p):
    tr=[float("nan")]+[max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1]))
                       for i in range(1,len(c))]
    return sma(tr,p)

def obv_f(c,v):
    r=[0.0]
    for i in range(1,len(c)):
        r.append(r[-1]+(v[i] if c[i]>c[i-1] else -v[i] if c[i]<c[i-1] else 0))
    return r

def tanh_f(x):
    v=max(min(2*x,20),-20); e=math.exp(v); return (e-1)/(e+1)

def corr(x,y,p,i):
    if i<p-1: return 0.0
    xs=x[i-p+1:i+1]; ys=y[i-p+1:i+1]
    mx=sum(xs)/len(xs); my=sum(ys)/len(ys)
    num=sum((a-mx)*(b-my) for a,b in zip(xs,ys))
    den=(sum((a-mx)**2 for a in xs)*sum((b-my)**2 for b in ys))**0.5
    return num/den if den else 0.0

def zscore_at(d,p,i):
    if i<p-1: return 0.0
    w=d[i-p+1:i+1]; m=sum(w)/len(w)
    s=(sum((x-m)**2 for x in w)/len(w))**0.5
    return (d[i]-m)/s if s else 0.0

def winsor(z,cap=2.5): return max(min(z,cap),-cap)

def highest(d,p,i):
    w=[x for x in d[max(0,i-p+1):i+1] if x==x]; return max(w) if w else float("nan")

def lowest(d,p,i):
    w=[x for x in d[max(0,i-p+1):i+1] if x==x]; return min(w) if w else float("nan")

def pivot_high(h,left,right,i):
    pi=i-right
    if pi<left or pi<0: return None
    cv=h[pi]
    for j in range(pi-left,pi+right+1):
        if j!=pi and 0<=j<len(h) and h[j]>=cv: return None
    return cv

def pivot_low(l,left,right,i):
    pi=i-right
    if pi<left or pi<0: return None
    cv=l[pi]
    for j in range(pi-left,pi+right+1):
        if j!=pi and 0<=j<len(l) and l[j]<=cv: return None
    return cv

# ══════════════════════════════════════════════════
#  MOTOR DE SEÑALES
# ══════════════════════════════════════════════════
def compute_signal(candles:list[dict],htf:list[dict]) -> tuple[Optional[str],dict]:
    dbg:dict={}
    if len(candles)<80 or len(htf)<25:
        dbg["error"]=f"velas insuf: {len(candles)}/{len(htf)}"
        return None,dbg

    o=[c["open"] for c in candles]; h=[c["high"] for c in candles]
    l=[c["low"]  for c in candles]; cl=[c["close"] for c in candles]
    v=[c["volume"] for c in candles]; n=len(cl); i=n-1

    # ATR
    at=atr_f(h,l,cl,10); atl=atr_f(h,l,cl,20)
    atr =at[i]  if at[i] ==at[i]  else 0.0001
    atrl=atl[i] if atl[i]==atl[i] else 0.0001
    blk=atr>atrl*2.5
    dbg["in_blackout"]=blk

    # Spread
    hilo=[math.log(h[j]/l[j]) if l[j]>0 else 0 for j in range(n)]
    sp=sma(hilo,5); bp=sp[i]*100 if sp[i]==sp[i] else 999
    exec_ok=bp<EXEC_BPT
    dbg["bp_drain"]=round(bp,4); dbg["exec_ok"]=exec_ok

    # OBV
    ob=obv_f(cl,v); om=ema(ob,14); os=stdev(ob,14)
    fvp=[(ob[j]-om[j])/os[j] if os[j]>0 else 0 for j in range(n)]

    # Crowding adaptativo
    from_rc=[((cl[j]-cl[j-20])/cl[j-20]) if j>=20 and cl[j-20]!=0 else 0 for j in range(n)]
    s2=sma(from_rc,40); sd2=stdev(from_rc,40)
    szl=[winsor((from_rc[j]-s2[j])/sd2[j]) if sd2[j]>0 else 0 for j in range(n)]
    cnt=0
    for j in range(max(0,i-15),i+1):
        cnt=(cnt+1) if abs(corr(szl,fvp,60,j))>=0.75 else 0
    cp=cnt>=15
    w1=max(0.40-0.15,0.10) if cp else 0.40
    w3=min(0.30+0.15,0.60) if cp else 0.30

    # L2 Factores
    sc=sma(cl,20); sd=stdev(cl,20); ba=sma(cl,8); bs=stdev(cl,8)
    rws=[]
    for j in range(n):
        fm=((cl[j]-cl[j-20])/cl[j-20]/((sd[j]/sc[j]) if sc[j]>0 else 1)) if j>=20 and cl[j-20]!=0 and sd[j]==sd[j] else 0
        fr=-(cl[j]-ba[j])/bs[j] if bs[j]>0 and ba[j]==ba[j] else 0
        rws.append(w1*fm+0.30*fr+w3*fvp[j])
    cp2=ema(rws,3); ss2=stdev(cp2,40)
    ns=tanh_f(cp2[i]/ss2[i]) if ss2[i]>0 else 0
    dbg["norm_score"]=round(ns,4)

    # L3 Decay
    fw=[(cl[j]-cl[j-1])/cl[j-1] if j>0 and cl[j-1]!=0 else 0 for j in range(n)]
    nss=[cp2[j-1]/ss2[j-1] if j>0 and ss2[j-1]>0 else 0 for j in range(n)]
    icv=[abs(corr(nss,fw,40,j)) for j in range(n)]
    ice=ema(icv,3)
    icp=max((x for x in ice[max(0,i-40):i+1] if x==x),default=0.001)
    dr=ice[i]/icp if icp>0 else 0.5
    sig_alive=dr>=DECAY_TH
    dbg["decay_pct"]=round(dr*100,1); dbg["sig_alive"]=sig_alive

    # L4 Dark Pool
    vb=sma(v,20); vbi=vb[i] if vb[i]==vb[i] else 0
    dp_buy =(v[i]>vbi*2.5) and ((h[i]-l[i])<atr*0.6) and cl[i]>o[i]
    dp_sell=(v[i]>vbi*2.5) and ((h[i]-l[i])<atr*0.6) and cl[i]<o[i]
    dbg["dp_buy"]=dp_buy; dbg["dp_sell"]=dp_sell

    # L6 Asimetría
    ur=[(h[j]-l[j]) if cl[j]>o[j] else 0 for j in range(n)]
    dr2=[(h[j]-l[j]) if cl[j]<o[j] else 0 for j in range(n)]
    au=sma(ur,10); ad=sma(dr2,10)
    rb=au[i]/ad[i] if ad[i]>0 else 0
    rs=ad[i]/au[i] if au[i]>0 else 0
    asym_bull=rb>=ASYM_RATIO if ASYM_RATIO>0 else True
    asym_bear=rs>=ASYM_RATIO if ASYM_RATIO>0 else True
    dbg["asym_bull"]=asym_bull; dbg["asym_bear"]=asym_bear

    # HTF tendencia
    hc=[x["close"] for x in htf]; e9=ema(hc,9); e21=ema(hc,21); hi2=len(hc)-1
    htf_bull=e9[hi2]>e21[hi2]; htf_bear=e9[hi2]<e21[hi2]
    dbg["htf_bull"]=htf_bull; dbg["htf_bear"]=htf_bear

    # L7 Trendlines
    phl=[]; pll=[]
    for j in range(max(0,i-50),i+1):
        ph=pivot_high(h,PIVOT_LEFT,PIVOT_RIGHT,j)
        pl=pivot_low(l, PIVOT_LEFT,PIVOT_RIGHT,j)
        if ph is not None: phl.append((j-PIVOT_RIGHT,ph))
        if pl is not None: pll.append((j-PIVOT_RIGHT,pl))

    tlb_l=tlb_s=False
    if len(phl)>=2:
        (b2,v2),(b1,v1)=phl[-2],phl[-1]
        if v2>v1 and (i-b2)<=30:
            slp=(v1-v2)/max(b1-b2,1); tn=v1+slp*(i-b1)
            tp_=v1+slp*(i-1-b1)
            tlb_l=cl[i]>tn+atr*0.15 and cl[i-1]<=tp_+atr*0.15
    if len(pll)>=2:
        (b2,v2),(b1,v1)=pll[-2],pll[-1]
        if v2<v1 and (i-b2)<=30:
            slp=(v1-v2)/max(b1-b2,1); tn=v1+slp*(i-b1)
            tp_=v1+slp*(i-1-b1)
            tlb_s=cl[i]<tn-atr*0.15 and cl[i-1]>=tp_-atr*0.15
    dbg["tl_break_long"]=tlb_l; dbg["tl_break_short"]=tlb_s

    # L8 Swing
    rpl=[pv for (pb,pv) in pll if (i-pb)<=40]
    rph=[pv for (pb,pv) in phl if (i-pb)<=40]
    hl2=sum(1 for j in range(1,len(rpl)) if rpl[j]>rpl[j-1])
    lh2=sum(1 for j in range(1,len(rph)) if rph[j]<rph[j-1])
    sell_ex=hl2>=HL_MIN_COUNT; buy_ex=lh2>=HL_MIN_COUNT
    dbg["sell_exhausted"]=sell_ex; dbg["buy_exhausted"]=buy_ex
    dbg["hl_count"]=hl2; dbg["lh_count"]=lh2

    # Liquidaciones
    ll=highest(h,50,i)*(1-0.1); ls=lowest(l,50,i)*(1+0.1)
    nl=abs(cl[i]-ll)<atr*0.5; ns_=abs(cl[i]-ls)<atr*0.5
    dbg["near_liq_long"]=nl; dbg["near_liq_short"]=ns_

    # CVD
    bp2=[(cl[j]-l[j])/(h[j]-l[j]) if (h[j]-l[j])>0 else 0.5 for j in range(n)]
    bd=[v[j]*(2*bp2[j]-1) for j in range(n)]
    ca=sma(bd,20); pra=[((cl[j]-cl[j-20])/cl[j-20]*100) if j>=20 and cl[j-20]!=0 else 0 for j in range(n)]
    cra=[((ca[j]-ca[j-20])/abs(ca[j-20])*100) if j>=20 and ca[j-20]!=0 and ca[j-20]==ca[j-20] else 0 for j in range(n)]
    cz=winsor(zscore_at(ca,40,i))
    dbg["cvd_z"]=round(cz,3)

    # Stop Hunt
    br=h[i]-l[i]
    lw=min(o[i],cl[i])-l[i]; hw=h[i]-max(o[i],cl[i])
    sh_dn=lw>br*0.60 and v[i]>vbi*1.5 and cl[i]>o[i] and br>atr*0.8
    sh_up=hw>br*0.60 and v[i]>vbi*1.5 and cl[i]<o[i] and br>atr*0.8
    dbg["stop_hunt_dn"]=sh_dn; dbg["stop_hunt_up"]=sh_up

    # Spoof / Blackout / Sesión
    spoof=(i>1 and v[i-1]>vbi*2.0 and abs(cl[i]-cl[i-2])<atr*0.25) or \
          (i>0 and (h[i-1]-l[i-1])>atr*1.5 and abs(cl[i]-o[i-1])<atr*0.3 and v[i-1]>vbi*1.8)
    hr=datetime.now(timezone.utc).hour
    in_t=(7<=hr<15) or (13<=hr<21) or hr>=21 or hr<1
    fok=not spoof and not blk and in_t
    dbg["filters_ok"]=fok; dbg["hour_utc"]=hr

    # ══ SEÑALES con jerarquía completa
    bl=ns>NORM_SCORE_TH  and sig_alive and exec_ok and htf_bull and asym_bull and sell_ex
    bs_=ns<-NORM_SCORE_TH and sig_alive and exec_ok and htf_bear and asym_bear and buy_ex
    dbg["base_long"]=bl; dbg["base_short"]=bs_

    ls_ =bl  and fok
    ss_ =bs_ and fok
    lf  =ls_ and tlb_l
    sf  =ss_ and tlb_s
    lsu =lf  and dp_buy
    ssu =sf  and dp_sell
    lv3 =lsu and cz>0 and not ns_
    sv3 =ssu and cz<0 and not nl
    shl =sh_dn and htf_bull and sig_alive and not blk and in_t and not spoof
    shs =sh_up and htf_bear and sig_alive and not blk and in_t and not spoof

    # Señal STD sin TL/DP requeridos
    if not TL_REQUIRED and not DP_REQUIRED:
        if ls_:  return "LONG_STD",  dbg
        if ss_:  return "SHORT_STD", dbg

    if lv3: return "LONG_SUP_V3",  dbg
    if sv3: return "SHORT_SUP_V3", dbg
    if lsu: return "LONG_SUP",     dbg
    if ssu: return "SHORT_SUP",    dbg
    if lf:  return "LONG_FUEL",    dbg
    if sf:  return "SHORT_FUEL",   dbg
    if shl: return "HUNT_LONG",    dbg
    if shs: return "HUNT_SHORT",   dbg
    return None, dbg

# ══════════════════════════════════════════════════
#  TRADING
# ══════════════════════════════════════════════════
async def handle_signal(signal:str,symbol:str,dbg:dict)->None:
    rank=SIGNAL_RANK.get(signal,0)
    if rank<MIN_RANK:
        return
    if len(open_trades)>=MAX_OPEN_TRADES:
        log.info(f"Max trades ({MAX_OPEN_TRADES}) — ignorando {signal} {symbol}")
        return

    is_long="LONG" in signal; is_short="SHORT" in signal
    if not (is_long or is_short): return

    if symbol in open_trades:
        ex=open_trades[symbol]
        if (is_long and ex["side"]=="BUY") or (is_short and ex["side"]=="SELL"):
            return

    # Filtro de calidad adicional para ejecutar orden real
    score=abs(dbg.get("norm_score",0))
    decay=dbg.get("decay_pct",0)/100
    if score < MIN_SCORE_TO_TRADE or decay < MIN_DECAY_TO_TRADE:
        log.info(f"Calidad insuf para trade {signal} {symbol}: "
                 f"score={score:.3f}<{MIN_SCORE_TO_TRADE} "
                 f"decay={decay:.2f}<{MIN_DECAY_TO_TRADE}")
        await tg(f"🔍 <b>SEÑAL DETECTADA</b> {signal} {symbol}\n"
                 f"Score:<code>{dbg.get('norm_score',0):.3f}</code> "
                 f"Decay:<code>{dbg.get('decay_pct',0):.0f}%</code>\n"
                 f"⚠️ Calidad insuficiente para abrir orden")
        return

    try:
        price=await get_price(symbol)
        balance=await get_balance()
        usdt=min(TRADE_SIZE_USDT, balance*0.20)
        if usdt<5:
            await tg(f"⚠️ Balance bajo: <code>{balance:.2f} USDT</code>"); return

        qty=max(round(usdt/price,4),0.001)
        side="BUY" if is_long else "SELL"
        sl=round(price*(1-SL_PCT/100) if is_long else price*(1+SL_PCT/100),4)
        tp=round(price*(1+TP_PCT/100) if is_long else price*(1-TP_PCT/100),4)

        order=await place_order(symbol,side,qty,sl,tp)
        oid=order.get("data",{}).get("orderId","—")
        open_trades[symbol]={
            "side":side,"entry":price,"qty":qty,"sl":sl,"tp":tp,
            "signal":signal,"order_id":oid,"time":time.time(),
            "score":dbg.get("norm_score",0),"decay":dbg.get("decay_pct",0),
        }
        scan_stats["trades_opened"]+=1
        log.info(f"✅ TRADE: {side} {symbol} @ {price} SL={sl} TP={tp}")
        await tg(fmt_open(signal,symbol,price,sl,tp,qty,
                          dbg.get("norm_score",0),dbg.get("decay_pct",0)))
    except Exception as e:
        scan_stats["errors"]+=1
        log.error(f"Trade error {signal} {symbol}: {e}")
        await tg(f"❌ <b>ERROR</b> {signal} {symbol}\n<code>{str(e)[:200]}</code>")

# ══════════════════════════════════════════════════
#  SCANNER — alta concurrencia con sesiones reutilizadas
# ══════════════════════════════════════════════════
async def scan_batch(symbols:list[str], sem:asyncio.Semaphore) -> list[tuple]:
    """
    Escanea un lote de símbolos en paralelo reutilizando
    una sola sesión HTTP por worker.
    """
    results=[]

    async def _scan_one(sym:str)->Optional[tuple]:
        async with sem:
            try:
                async with aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(limit=1)
                ) as session:
                    kl =await get_klines_s(sym,KLINE_INTERVAL,120,session)
                    htf=await get_klines_s(sym,HTF_INTERVAL,40,session)

                sig,dbg=compute_signal(kl,htf)
                last_scan_results[sym]={"signal":sig,"dbg":dbg,"ts":time.time()}

                if sig:
                    score=abs(dbg.get("norm_score",0))
                    decay=dbg.get("decay_pct",0)
                    log.info(f"🎯 {sig} {sym} score={score:.3f} decay={decay:.0f}%")
                    return (sym,sig,dbg)

                if DEBUG_SIGNALS:
                    ns=dbg.get("norm_score",0)
                    if abs(ns)>0.12 and dbg.get("sig_alive",False):
                        fails=[]
                        if not dbg.get("exec_ok"):    fails.append(f"exec({dbg.get('bp_drain','?'):.3f})")
                        if not dbg.get("htf_bull") and ns>0: fails.append("htf")
                        if not dbg.get("htf_bear") and ns<0: fails.append("htf")
                        if not dbg.get("asym_bull") and ns>0: fails.append("asym")
                        if not dbg.get("sell_exhausted") and ns>0: fails.append(f"swingHL={dbg.get('hl_count',0)}")
                        if not dbg.get("filters_ok"):  fails.append("filters")
                        if fails:
                            log.debug(f"  near-miss {sym} score={ns:.3f} decay={dbg.get('decay_pct',0):.0f}% falla:{fails}")
                return None
            except asyncio.TimeoutError:
                log.debug(f"Timeout {sym}")
                return None
            except Exception as e:
                log.debug(f"Error {sym}: {str(e)[:80]}")
                return None

    tasks=[_scan_one(s) for s in symbols]
    raw=await asyncio.gather(*tasks,return_exceptions=True)
    for r in raw:
        if isinstance(r,tuple): results.append(r)
    return results

async def scanner_loop()->None:
    global SYMBOLS
    await asyncio.sleep(5)

    SYMBOLS=SYMBOLS_OVERRIDE if SYMBOLS_OVERRIDE else await get_all_symbols()

    await tg(
        f"🤖 <b>QF×JP Bot v4.3 — Scanner completo</b>\n"
        f"📊 <b>{len(SYMBOLS)} pares</b> | Vol≥<code>{MIN_VOLUME_USDT/1e3:.0f}K</code>\n"
        f"⚡ Concurrencia: <code>{CONCURRENCY}</code> | Ciclo: <code>{SCAN_INTERVAL}s</code>\n"
        f"🎯 Min señal: <code>{MIN_SIGNAL_LEVEL}</code> (rank≥{MIN_RANK})\n"
        f"📐 Capital/trade: <code>{TRADE_SIZE_USDT}$</code> "
        f"SL:<code>{SL_PCT}%</code> TP:<code>{TP_PCT}%</code>\n"
        f"🔬 Score≥<code>{NORM_SCORE_TH}</code> "
        f"Decay≥<code>{DECAY_TH*100:.0f}%</code>\n"
        f"Top 5: <code>{', '.join(SYMBOLS[:5])}</code>..."
    )

    sem=asyncio.Semaphore(CONCURRENCY)
    cycle=0; last_refresh=time.time()

    while True:
        if not SYMBOLS_OVERRIDE and (time.time()-last_refresh)>14400:
            new=await get_all_symbols()
            if new:
                added=len(set(new)-set(SYMBOLS))
                removed=len(set(SYMBOLS)-set(new))
                SYMBOLS=new; last_refresh=time.time()
                log.info(f"Lista refrescada: {len(SYMBOLS)} pares (+{added} -{removed})")
                if added or removed:
                    await tg(f"🔄 Lista actualizada: <b>{len(SYMBOLS)} pares</b> "
                             f"(+{added} -{removed})")

        cycle+=1; scan_stats["cycles"]=cycle
        t0=time.time()

        results=await scan_batch(SYMBOLS,sem)
        scan_stats["signals_total"]+=len(results)

        results.sort(key=lambda x:(
            -SIGNAL_RANK.get(x[1],0),
            -abs(x[2].get("norm_score",0))
        ))

        executed=0
        for sym,sig,dbg in results:
            if len(open_trades)>=MAX_OPEN_TRADES: break
            await handle_signal(sig,sym,dbg)
            executed+=1

        elapsed=time.time()-t0

        log.info(f"━ Ciclo #{cycle} | {len(SYMBOLS)} pares | "
                 f"{len(results)} señales | {executed} trades | {elapsed:.1f}s ━")

        if results:
            top=[(s,sig,dbg.get("norm_score",0),dbg.get("decay_pct",0))
                 for s,sig,dbg in results]
            msg=fmt_cycle_summary(cycle,len(SYMBOLS),len(results),elapsed,top)
            if msg: await tg(msg)
        elif cycle%20==0:
            top5=sorted(
                [(s,d["dbg"].get("norm_score",0),d["dbg"].get("decay_pct",0))
                 for s,d in last_scan_results.items()
                 if "dbg" in d and d["dbg"].get("sig_alive",False)],
                key=lambda x:-abs(x[1])
            )[:5]
            if top5:
                lines=["🔬 <b>Top-5 (sin señal aún):</b>"]
                for s,sc,dc in top5:
                    e="🟢" if sc>0 else "🔴"
                    lines.append(f"  {e} <b>{s}</b> score=<code>{sc:.3f}</code> decay=<code>{dc:.0f}%</code>")
                await tg("\n".join(lines))

        await asyncio.sleep(max(0,SCAN_INTERVAL-elapsed))

# ══════════════════════════════════════════════════
#  MONITOR DE POSICIONES
# ══════════════════════════════════════════════════
async def monitor()->None:
    while True:
        await asyncio.sleep(30)
        for sym,tr in list(open_trades.items()):
            try:
                live=await get_open_pos(sym)
                price=await get_price(sym)
                entry=tr["entry"]; side=tr["side"]
                pnl=((price-entry)/entry*100) if side=="BUY" else ((entry-price)/entry*100)

                if live is None:
                    await tg(fmt_close(sym,pnl,"SL/TP ejecutado (BingX)"))
                    open_trades.pop(sym,None); continue

                reason=None
                if side=="BUY":
                    if price<=tr["sl"]: reason="SL"
                    elif price>=tr["tp"]: reason="TP"
                else:
                    if price>=tr["sl"]: reason="SL"
                    elif price<=tr["tp"]: reason="TP"
                if not reason and (time.time()-tr["time"])>10800:
                    reason="Timeout 3h"
                if reason:
                    await close_pos(sym,side,tr["qty"])
                    await tg(fmt_close(sym,pnl,reason,tr.get("signal","")))
                    open_trades.pop(sym,None)
            except Exception as e:
                log.error(f"Monitor {sym}: {e}")

# ══════════════════════════════════════════════════
#  FASTAPI
# ══════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app):
    asyncio.create_task(tg_worker())
    asyncio.create_task(scanner_loop())
    asyncio.create_task(monitor())
    yield

app=FastAPI(title="QF×JP Bot v4.3",lifespan=lifespan)

@app.post("/webhook")
async def webhook(request:Request):
    if request.headers.get("X-Webhook-Secret","")!=WEBHOOK_SECRET:
        raise HTTPException(401,"Unauthorized")
    try: data=await request.json()
    except: raise HTTPException(400,"Invalid JSON")
    sig=data.get("signal","").strip()
    sym=data.get("symbol","").strip().upper()
    if "-" not in sym: sym=sym.replace("USDT","")+"-USDT"
    dbg=data.get("debug",{"norm_score":0.99,"decay_pct":100})
    if sig and sym:
        asyncio.create_task(handle_signal(sig,sym,dbg))
    return JSONResponse({"status":"ok"})

@app.get("/health")
async def health():
    return {
        "status":"running","v":"4.3",
        "symbols":len(SYMBOLS),"top10":SYMBOLS[:10],
        "open_trades":len(open_trades),
        "trades":list(open_trades.keys()),
        "stats":scan_stats,
        "params":{
            "min_signal":MIN_SIGNAL_LEVEL,"min_rank":MIN_RANK,
            "concurrency":CONCURRENCY,"scan_interval":SCAN_INTERVAL,
            "min_volume_usdt":MIN_VOLUME_USDT,"max_symbols":MAX_SYMBOLS,
            "norm_score_th":NORM_SCORE_TH,"exec_bpt":EXEC_BPT,
            "decay_th":DECAY_TH,"asym_ratio":ASYM_RATIO,
            "hl_min":HL_MIN_COUNT,"pivot_l":PIVOT_LEFT,"pivot_r":PIVOT_RIGHT,
            "min_score_trade":MIN_SCORE_TO_TRADE,
            "min_decay_trade":MIN_DECAY_TO_TRADE,
        }
    }

@app.get("/json")
async def health_json():
    """Alias de /health para compatibilidad."""
    return await health()

@app.get("/trades")
async def trades_view(): return {"open_trades":open_trades}

@app.get("/signals")
async def signals_view():
    """Top 20 pares ordenados por score absoluto del último scan."""
    top=sorted(
        [(s,d["signal"],d["dbg"].get("norm_score",0),
          d["dbg"].get("decay_pct",0),d["dbg"].get("exec_ok",False),
          d["dbg"].get("htf_bull",False),d["dbg"].get("htf_bear",False))
         for s,d in last_scan_results.items() if "dbg" in d],
        key=lambda x:-abs(x[2])
    )[:20]
    return {"top20":[
        {"sym":s,"signal":sig or "none","score":sc,
         "decay":dc,"exec":ex,"htf_bull":hb,"htf_bear":hbr}
        for s,sig,sc,dc,ex,hb,hbr in top
    ]}

@app.get("/scan/{symbol}")
async def scan_now(symbol:str):
    sym=symbol.upper()
    if "-" not in sym: sym=sym.replace("USDT","")+"-USDT"
    try:
        async with aiohttp.ClientSession() as session:
            kl =await get_klines_s(sym,KLINE_INTERVAL,120,session)
            htf=await get_klines_s(sym,HTF_INTERVAL,40,session)
        sig,dbg=compute_signal(kl,htf)
        return {"symbol":sym,"signal":sig or "none","candles":len(kl),"debug":dbg}
    except Exception as e:
        return {"symbol":sym,"error":str(e)}

@app.get("/stats")
async def stats():
    return {
        "cycles":scan_stats["cycles"],
        "signals_total":scan_stats["signals_total"],
        "trades_opened":scan_stats["trades_opened"],
        "errors":scan_stats["errors"],
        "symbols_scanned":len(SYMBOLS),
        "open_trades":len(open_trades),
        "uptime_cycles":scan_stats["cycles"],
    }

if __name__=="__main__":
    uvicorn.run("src.bot:app",host="0.0.0.0",
                port=int(os.environ.get("PORT",8000)),reload=False)
