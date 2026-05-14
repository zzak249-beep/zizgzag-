import asyncio
import logging
import math
from typing import Dict
import aiohttp

import config
import telegram_notifier as tg
from bingx_client import BingXClient
from strategy import ChannelFadeSignal, parse_klines

log = logging.getLogger("trader")


class Position:
    def __init__(self, symbol, side, entry, sl, tp, qty, green, red, atr):
        self.symbol=symbol; self.side=side; self.entry=entry
        self.sl=sl; self.tp=tp; self.qty=qty
        self.green=green; self.red=red; self.atr=atr
        self.closed=False; self.trail_done=False


class Trader:
    def __init__(self, client: BingXClient, session: aiohttp.ClientSession):
        self.client    = client
        self.session   = session
        self.strategy  = ChannelFadeSignal()
        self.positions: Dict[str,Position] = {}
        self.cooldown:  Dict[str,int]      = {}   # symbol → bars restantes de cooldown
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.daily_wins   = 0
        self.paused       = False
        self._live: set   = set()

    async def refresh_live_positions(self):
        try:
            live = await self.client.get_positions()
            self._live = {
                p.get("symbol","") for p in live
                if self._amt(p) != 0
            }
        except Exception as e:
            log.error(f"refresh_live_positions: {e}")

    @staticmethod
    def _amt(p):
        for k in ("positionAmt","posAmt","availableAmt"):
            try:
                v=float(p.get(k,0))
                if v!=0: return v
            except: pass
        return 0.0

    # ──────────────────────────────────────────────────────────────────
    async def process_pair(self, symbol: str, balance: float):
        if self.paused: return

        # Límite pérdida diaria
        if balance>0 and (self.daily_pnl/balance)*100 <= -config.MAX_DAILY_LOSS:
            self.paused=True
            await tg.daily_loss_limit(self.session,self.daily_pnl,config.MAX_DAILY_LOSS,balance)
            return

        # Monitorear posición activa
        pos=self.positions.get(symbol)
        if pos and not pos.closed:
            await self._monitor(symbol)
            return

        # Cooldown: saltarse este par N ciclos tras señal
        if self.cooldown.get(symbol,0)>0:
            self.cooldown[symbol]-=1
            return

        # Límite posiciones simultáneas
        open_n=sum(1 for p in self.positions.values() if not p.closed)
        if open_n>=config.MAX_POSITIONS: return

        # Fetch klines
        raw=await self.client.get_klines(symbol,config.TIMEFRAME,config.KLINE_LIMIT)
        if not raw or len(raw)<35: return
        opens,highs,lows,closes,volumes=parse_klines(raw)
        if len(closes)<35: return

        # Señal
        sig=self.strategy.compute(opens,highs,lows,closes,volumes)
        if sig is None: return

        # Activar cooldown para evitar re-entrada inmediata en el mismo nivel
        self.cooldown[symbol]=config.COOLDOWN_BARS

        await tg.signal_detected(self.session,symbol,sig["side"],sig["green"],
                                 sig["red"],sig["entry"],sig["trigger"],
                                 sig["canal_width"],sig["vol_ratio"],sig["rsi"])
        await self._enter(symbol,sig,balance)

    # ──────────────────────────────────────────────────────────────────
    async def _enter(self, symbol, sig, balance):
        try:
            entry=sig["entry"]; sl=sig["sl"]; tp=sig["tp"]
            side=sig["side"];   atr=sig["atr"]
            sl_dist=abs(entry-sl)
            if sl_dist==0 or entry==0: return

            qty=math.floor((balance*(config.RISK_PCT/100)*config.LEVERAGE/entry)*1000)/1000
            if qty<=0:
                log.warning(f"[{symbol}] qty=0, balance={balance:.2f}"); return

            rr=abs(tp-entry)/sl_dist

            await self.client.set_leverage(symbol,config.LEVERAGE)
            await asyncio.sleep(0.1)

            resp=await self.client.place_market_order(symbol,side,qty,sl,tp)
            code=resp.get("code",-1)
            if code!=0:
                err=resp.get("msg",str(resp))
                log.error(f"[{symbol}] Order rejected code={code}: {err}")
                await tg.error_alert(self.session,f"[{symbol}] {err}")
                return

            self.positions[symbol]=Position(symbol,side,entry,sl,tp,qty,
                                            sig["green"],sig["red"],atr)
            self._live.add(symbol)
            self.daily_trades+=1

            await tg.trade_entry(self.session,symbol,side,entry,sl,tp,qty,balance,rr,atr)
            log.info(f"✅ [{symbol}] {side} entry={entry:.6g} SL={sl:.6g} TP={tp:.6g} qty={qty} RR=1:{rr:.1f}")

        except Exception as e:
            log.exception(f"[{symbol}] _enter: {e}")
            await tg.error_alert(self.session,f"[{symbol}] Entry error: {e}")

    # ──────────────────────────────────────────────────────────────────
    async def _monitor(self, symbol):
        try:
            pos=self.positions.get(symbol)
            if not pos or pos.closed: return

            # Trailing: mover SL a breakeven cuando PnL >= TRAIL_PCT% del TP
            if not pos.trail_done and config.TRAIL_PCT>0:
                raw=await self.client.get_klines(symbol,config.TIMEFRAME,3)
                _,_,_,Ct,_=parse_klines(raw)
                if len(Ct)>=2:
                    cur=float(Ct[-2])
                    tp_dist=abs(pos.tp-pos.entry)
                    if tp_dist>0:
                        done=(abs(cur-pos.entry)/tp_dist)*100
                        if done>=config.TRAIL_PCT:
                            pos.sl=pos.entry; pos.trail_done=True
                            await tg.trail_moved(self.session,symbol,pos.side,
                                                 pos.entry,cur,done)
                            log.info(f"[{symbol}] Trail→BE: SL={pos.entry:.6g} avance={done:.0f}%")

            # ¿Posición cerrada por SL/TP?
            if symbol not in self._live:
                pos.closed=True
                raw=await self.client.get_klines(symbol,config.TIMEFRAME,3)
                _,_,_,Ct,_=parse_klines(raw)
                exit_p=float(Ct[-2]) if len(Ct)>=2 else pos.entry

                pnl_pts=(exit_p-pos.entry) if pos.side=="BUY" else (pos.entry-exit_p)
                pnl=pnl_pts*pos.qty*config.LEVERAGE
                pnl_pct=(pnl_pts/pos.entry)*100*config.LEVERAGE

                dist_tp=abs(exit_p-pos.tp); dist_sl=abs(exit_p-pos.sl)
                if pos.trail_done and abs(exit_p-pos.entry)<pos.atr*0.3:
                    reason="BREAKEVEN 🔄"
                elif dist_tp<dist_sl:
                    reason="TAKE PROFIT ✅"; self.daily_wins+=1
                else:
                    reason="STOP LOSS ❌"

                self.daily_pnl+=pnl
                await tg.trade_exit(self.session,symbol,pos.side,
                                    pos.entry,exit_p,pnl,pnl_pct,reason)
                log.info(f"[{symbol}] Cerrada PnL={pnl:+.4f} {reason}")

        except Exception as e:
            log.error(f"[{symbol}] _monitor: {e}")

    def reset_daily(self):
        self.daily_pnl=0.; self.daily_trades=0
        self.daily_wins=0; self.paused=False
        log.info("🔄 Reset diario")
