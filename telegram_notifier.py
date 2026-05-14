import aiohttp
from datetime import datetime
import config

API=f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}"

async def send(session,text,pm="HTML"):
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        print(f"[TG] {text[:100]}"); return
    try:
        async with session.post(f"{API}/sendMessage",json={
            "chat_id":config.TELEGRAM_CHAT_ID,"text":text,
            "parse_mode":pm,"disable_web_page_preview":True}): pass
    except Exception as e: print(f"[TG-ERR] {e}")

async def bot_start(session):
    filtros=[]
    if config.USE_EMA_FILTER: filtros.append(f"EMA{config.EMA_PERIOD}")
    if config.USE_RSI_FILTER: filtros.append(f"RSI>{config.RSI_SHORT_MIN}/<{config.RSI_LONG_MAX}")
    if config.USE_VOL_FILTER: filtros.append(f"Vol>{config.VOL_MULT}x")
    fstr=", ".join(filtros) if filtros else "ninguno (máximas señales)"
    await send(session,
        "🤖 <b>ZigZag Channel Fade Bot</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Timeframe:    <code>{config.TIMEFRAME}</code>\n"
        f"📐 Trigger ATR:  <code>{config.ATR_TRIGGER_MULT}×ATR</code>\n"
        f"📏 Canal mín:    <code>{config.MIN_CANAL_ATR}×ATR</code>\n"
        f"🔒 SL:           <code>{config.SL_ATR_MULT}×ATR</code>\n"
        f"⏱  Cooldown:     <code>{config.COOLDOWN_BARS} velas</code>\n"
        f"🧪 Filtros:      <code>{fstr}</code>\n"
        f"⚡ Leverage:     <code>{config.LEVERAGE}x</code>\n"
        f"💰 Riesgo/trade: <code>{config.RISK_PCT}%</code>\n"
        f"🎯 Top pares:    <code>{config.TOP_PAIRS}</code>\n"
        f"⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
    )

async def scanner_result(session,pairs,balance):
    txt="\n".join(f"  • <code>{p}</code>" for p in pairs[:20])
    await send(session,
        f"🔭 <b>SCAN — {len(pairs)} PARES ACTIVOS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 Balance: <code>{balance:.2f} USDT</code>\n{txt}"
    )

async def signal_detected(session,symbol,side,green,red,close,trigger,canal,vol_ratio,rsi):
    emoji="🔴 SHORT" if side=="SELL" else "🟢 LONG"
    if side=="SELL":
        desc=f"close {close:.6g} ≥ verde+ATR {trigger:.6g}"
    else:
        desc=f"close {close:.6g} ≤ rojo-ATR {trigger:.6g}"
    await send(session,
        f"{emoji} <b>SEÑAL</b> — <code>{symbol}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 {desc}\n"
        f"🟩 Línea verde: <code>{green:.6g}</code>\n"
        f"🟥 Línea roja:  <code>{red:.6g}</code>\n"
        f"📏 Canal:       <code>{canal:.4g}</code>\n"
        f"📉 RSI:         <code>{rsi:.1f}</code>\n"
        f"📊 Vol ratio:   <code>{vol_ratio:.2f}x</code>"
    )

async def trade_entry(session,symbol,side,entry,sl,tp,qty,balance,rr,atr):
    e="🟢 LONG" if side=="BUY" else "🔴 SHORT"
    slp=abs(entry-sl)/entry*100; tpp=abs(tp-entry)/entry*100
    await send(session,
        f"{e} <b>ENTRADA</b> — <code>{symbol}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💲 Entry:   <code>{entry:.6g}</code>\n"
        f"🛑 SL:      <code>{sl:.6g}</code> (-{slp:.2f}%)\n"
        f"🎯 TP:      <code>{tp:.6g}</code> (+{tpp:.2f}%)\n"
        f"📦 Qty:     <code>{qty:.4f}</code>\n"
        f"⚖️ RR:      <code>1:{rr:.1f}</code>\n"
        f"🌊 ATR:     <code>{atr:.4g}</code>\n"
        f"💵 Balance: <code>{balance:.2f} USDT</code>\n"
        f"⏰ {datetime.utcnow().strftime('%H:%M:%S UTC')}"
    )

async def trail_moved(session,symbol,side,entry,cur,pct):
    e="🟢" if side=="BUY" else "🔴"
    await send(session,
        f"🔒 <b>TRAIL → BREAKEVEN</b> — <code>{symbol}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{e} {'LONG' if side=='BUY' else 'SHORT'}\n"
        f"💲 Entry:   <code>{entry:.6g}</code>\n"
        f"📍 Actual:  <code>{cur:.6g}</code>\n"
        f"✅ TP avance: <code>{pct:.0f}%</code>\n"
        f"🛑 SL → <code>{entry:.6g}</code> (breakeven)"
    )

async def trade_exit(session,symbol,side,entry,exit_p,pnl,pct,reason):
    em="✅" if pnl>=0 else "❌"
    de="🟢 LONG" if side=="BUY" else "🔴 SHORT"
    await send(session,
        f"{em} <b>CIERRE</b> — <code>{symbol}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{de}\n"
        f"💲 Entrada: <code>{entry:.6g}</code>\n"
        f"💲 Salida:  <code>{exit_p:.6g}</code>\n"
        f"💰 PnL:     <code>{pnl:+.4f} USDT ({pct:+.2f}%)</code>\n"
        f"📋 Motivo:  <code>{reason}</code>\n"
        f"⏰ {datetime.utcnow().strftime('%H:%M:%S UTC')}"
    )

async def daily_summary(session,trades,wins,pnl,balance):
    wr=wins/trades*100 if trades else 0
    await send(session,
        f"📊 <b>RESUMEN DIARIO</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 Trades:  <code>{trades}</code>\n"
        f"✅ Wins:    <code>{wins} ({wr:.1f}%)</code>\n"
        f"❌ Losses:  <code>{trades-wins}</code>\n"
        f"💰 PnL:     <code>{pnl:+.4f} USDT</code>\n"
        f"💵 Balance: <code>{balance:.2f} USDT</code>\n"
        f"⏰ {datetime.utcnow().strftime('%Y-%m-%d UTC')}"
    )

async def daily_loss_limit(session,pnl,limit,balance):
    await send(session,
        f"🚨 <b>LÍMITE PÉRDIDA DIARIA</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📉 PnL:     <code>{pnl:+.4f} USDT</code>\n"
        f"🛑 Límite:  <code>-{limit}%</code>\n"
        f"💵 Balance: <code>{balance:.2f} USDT</code>\n"
        "⏸️ PAUSADO hasta mañana"
    )

async def error_alert(session,msg):
    await send(session,f"⚠️ <b>ERROR</b>\n<code>{msg[:400]}</code>\n"
               f"⏰ {datetime.utcnow().strftime('%H:%M:%S UTC')}")
