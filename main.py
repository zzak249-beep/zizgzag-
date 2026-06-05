"""
main.py — QF×JP v3.4 Bot — Con Scanner + Backtest automático
=============================================================
- Escanea TODOS los símbolos de BingX al arrancar
- Backtest automático para seleccionar los más rentables
- Opera solo con los mejores símbolos del backtest
- Rescan + Rebacktest cada 24h
"""
import logging, time, sys, os
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

import config as cfg

logging.basicConfig(
    level=getattr(logging, cfg.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")

from client            import BingXClient, parse_klines
from indicators        import QFJPIndicators
from qfxjp_signal      import QFJPScorer
from position_manager  import PositionManager
from risk_manager      import RiskManager
from telegram_notifier import TelegramNotifier
from scanner           import BingXScanner
from backtest          import BacktestEngine, BacktestResult

try:
    from market_context import MarketContextEngine
    MECHANICS_OK = True
except ImportError:
    MECHANICS_OK = False

# ── HEALTH CHECK ──────────────────────────────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"QF x JP v3.4 OK"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass

def start_health_server():
    HTTPServer(("0.0.0.0", cfg.PORT), HealthHandler).serve_forever()


# ═══════════════════════════════════════════════════════════════════════════════
#  BOT
# ═══════════════════════════════════════════════════════════════════════════════

class QFJPBot:

    # Parámetros del scanner/backtest
    SCANNER_TOP_N      = int(os.getenv("SCANNER_TOP_N",   "50"))   # top N por volumen
    BT_CANDLES         = int(os.getenv("BT_CANDLES",      "800"))  # velas por backtest
    BT_MIN_TRADES      = int(os.getenv("BT_MIN_TRADES",   "5"))    # trades mínimos
    BT_MIN_PF          = float(os.getenv("BT_MIN_PF",     "1.1"))  # profit factor mínimo
    BT_MIN_WR          = float(os.getenv("BT_MIN_WR",     "0.42")) # win rate mínimo
    BT_MAX_SYMBOLS     = int(os.getenv("BT_MAX_SYMBOLS",  "15"))   # máx símbolos a operar
    RESCAN_HOURS       = int(os.getenv("RESCAN_HOURS",    "24"))   # cada X horas re-backtest
    MIN_VOLUME_USDT    = float(os.getenv("MIN_VOLUME",    "1000000")) # volumen mínimo 24h

    def __init__(self):
        logger.info("╔══════════════════════════════════════╗")
        logger.info("║  QF×JP v3.4 Bot + Scanner + Backtest ║")
        logger.info("╚══════════════════════════════════════╝")

        self.client   = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET_KEY, cfg.BINGX_BASE_URL)
        self.tg       = TelegramNotifier(cfg.TELEGRAM_TOKEN, cfg.TELEGRAM_CHAT_ID)
        self.risk_mgr = RiskManager(cfg, self.client)
        self.pos_mgr  = PositionManager(self.client, cfg)
        self.bx_scanner = BingXScanner(min_volume_usdt=self.MIN_VOLUME_USDT)
        self.bt_engine  = BacktestEngine(cfg, QFJPIndicators, QFJPScorer)
        self.mechanics  = MarketContextEngine(
            api_key=cfg.BINGX_API_KEY, secret_key=cfg.BINGX_SECRET_KEY
        ) if MECHANICS_OK else None

        self.active_symbols: list[str] = []
        self.bt_results:     list      = []
        self.indicators:     dict      = {}
        self.scorers:        dict      = {}
        self._cycle          = 0
        self._last_scan_ts   = 0

    # ─── SCANNER + BACKTEST ───────────────────────────────────────────────────

    def run_scanner_and_backtest(self):
        """
        1. Obtiene todos los símbolos de BingX
        2. Filtra top N por volumen
        3. Corre backtest en cada uno
        4. Selecciona los rentables para operar
        5. Envía reporte por Telegram
        """
        self.tg.send(
            f"🔍 *Scanner iniciado*\n"
            f"Analizando TODOS los perpetuos de BingX...\n"
            f"_(esto tarda ~{self.SCANNER_TOP_N * 2} segundos)_"
        )

        # 1. Obtener todos los símbolos
        all_symbols = self.bx_scanner.get_tradeable_symbols(force_refresh=True)
        if not all_symbols:
            self.tg.send("❌ Scanner: no se pudieron obtener símbolos de BingX")
            return

        # Limitar al top N por volumen
        top_symbols = all_symbols[:self.SCANNER_TOP_N]
        self.tg.send(
            f"📋 *{len(all_symbols)} símbolos encontrados*\n"
            f"Backtesting top `{len(top_symbols)}` por volumen...\n"
            f"Candles por símbolo: `{self.BT_CANDLES}` × 3min"
        )

        # 2. Backtest
        def on_progress(sym, i, total):
            if i % 10 == 0:
                self.tg.send(f"⏳ Backtest: `{i}/{total}` — `{sym}`")

        self.bt_results = self.bt_engine.run_all(
            top_symbols,
            candles=self.BT_CANDLES,
            tf="3m",
            delay=0.8,
            on_progress=on_progress,
        )

        # 3. Filtrar rentables
        selected = [
            r for r in self.bt_results
            if r.total_trades >= self.BT_MIN_TRADES
            and r.profit_factor >= self.BT_MIN_PF
            and r.win_rate >= self.BT_MIN_WR
        ][:self.BT_MAX_SYMBOLS]

        if not selected:
            # Si no hay rentables, usar top 10 por profit factor
            selected = sorted(
                [r for r in self.bt_results if r.total_trades >= 3],
                key=lambda r: -r.profit_factor
            )[:10]
            self.tg.send(
                "⚠️ Sin símbolos que pasen filtros — "
                f"usando top `{len(selected)}` por profit factor"
            )

        self.active_symbols = [r.symbol for r in selected]

        # Reiniciar indicadores para nuevos símbolos
        self.indicators = {s: QFJPIndicators(cfg) for s in self.active_symbols}
        self.scorers    = {s: QFJPScorer(cfg)     for s in self.active_symbols}

        # 4. Enviar reporte completo
        msgs = BacktestEngine.format_telegram_report(self.bt_results, top_n=20)
        for msg in msgs:
            self.tg.send(msg)
            time.sleep(0.5)

        # Resumen final
        self.tg.send(
            f"✅ *Backtest completado*\n"
            f"Símbolos a operar: `{len(self.active_symbols)}`\n"
            f"`{'  |  '.join(self.active_symbols[:10])}`\n"
            f"{'  |  '.join(self.active_symbols[10:]) if len(self.active_symbols)>10 else ''}"
        )

        self._last_scan_ts = time.time()
        logger.info(f"[Bot] Símbolos activos: {self.active_symbols}")

    # ─── CICLO DE TRADING ─────────────────────────────────────────────────────

    def run_cycle(self):
        self._cycle += 1

        # Re-scan cada RESCAN_HOURS horas
        hours_since = (time.time() - self._last_scan_ts) / 3600
        if not self.active_symbols or hours_since >= self.RESCAN_HOURS:
            logger.info(f"[Bot] Iniciando scanner/backtest (han pasado {hours_since:.1f}h)")
            self.run_scanner_and_backtest()
            if not self.active_symbols:
                return

        logger.info(f"═══ Ciclo #{self._cycle} | {len(self.active_symbols)} símbolos ═══")

        # Risk check
        can, reason = self.risk_mgr.can_trade()
        if not can:
            logger.warning(f"[Risk] {reason}")
            return

        balance = self.risk_mgr.refresh()
        if balance is None:
            logger.error("Sin balance")
            return

        # Verificar posiciones
        prices = {}
        for s in self.active_symbols:
            p = self.client.get_last_price(s)
            if p: prices[s] = p
        self.pos_mgr.check_positions(prices)

        # Scan + señales
        scan_results = []
        new_signals  = []

        for symbol in self.active_symbols:
            try:
                df   = parse_klines(self.client.get_klines_raw(symbol, cfg.CANDLE_TF, cfg.CANDLE_LIMIT))
                df15 = parse_klines(self.client.get_klines_raw(symbol, cfg.HTF_15M_TF, 50))
                df1h = parse_klines(self.client.get_klines_raw(symbol, cfg.HTF_1H_TF, 50))

                if df is None or len(df) < 50: continue

                ind = self.indicators[symbol].compute(df, df15, df1h, None, None)
                sig = self.scorers[symbol].score(symbol, ind, balance)

                # Market mechanics
                ctx = None
                if self.mechanics:
                    try:
                        ctx = self.mechanics.analyze(symbol, ind.price, df,
                            high=float(df["high"].iloc[-1]), low=float(df["low"].iloc[-1]))
                        if sig.is_valid:
                            if not ctx.entry_allowed(sig.direction):
                                sig.direction = "NONE"; sig.level = "NONE"
                            else:
                                bonus = ctx.score_modifier + ctx.get_judas_bonus(sig.direction)
                                sig.score = max(0, min(100, sig.score + bonus))
                    except: pass

                entry = {"symbol": symbol, "sc_l": sig.score_long, "sc_s": sig.score_short,
                         "ses": ind.ses_label, "regime": ind.regime,
                         "level": sig.level if sig.is_valid else None,
                         "signal": sig if sig.is_valid else None, "ctx": ctx}
                scan_results.append(entry)
                if sig.is_valid: new_signals.append(entry)

                logger.info(
                    f"[Scan] {symbol:14} "
                    f"L:{sig.score_long:3d} S:{sig.score_short:3d} "
                    f"| {ind.ses_label:4} | {ind.regime:8} "
                    f"| {sig.level if sig.is_valid else '----'}"
                )
            except Exception as e:
                logger.error(f"[Bot] {symbol}: {e}", exc_info=True)

        # Resumen cada 5 ciclos
        if self._cycle % 5 == 0:
            self.tg.scan_summary(scan_results)

        # Ejecutar señales
        for result in new_signals:
            signal = result["signal"]
            if self.pos_mgr.open_count >= cfg.MAX_OPEN_TRADES: break
            if self.pos_mgr.has_position(signal.symbol): continue
            if self.pos_mgr.open_position(signal, balance):
                self.tg.signal_entry(signal, result.get("ctx"))

        # Reporte diario
        now = datetime.now(timezone.utc)
        if now.hour == 0 and now.minute < cfg.CYCLE_MINUTES:
            self.tg.daily_report(self.risk_mgr.get_stats(),
                                  self.pos_mgr.get_positions_summary())


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    Thread(target=start_health_server, daemon=True).start()
    logger.info(f"Health check → puerto {cfg.PORT}")

    bot = QFJPBot()
    bot.tg.send(
        f"🚀 *QF×JP v3.4 + Scanner INICIADO*\n"
        f"Analizando TODOS los perpetuos de BingX\n"
        f"Leverage: `{cfg.LEVERAGE}x` | Risk: `{cfg.RISK_PER_TRADE*100:.1f}%`\n"
        f"Score STD/FUEL/SUP: `{cfg.SCORE_STD}/{cfg.SCORE_FUEL}/{cfg.SCORE_SUP}`\n"
        f"Backtest: `{bot.BT_CANDLES}` velas | Top `{bot.SCANNER_TOP_N}` por volumen"
    )

    # Primer scan inmediato
    try:
        bot.run_scanner_and_backtest()
    except Exception as e:
        logger.error(f"Error en scanner inicial: {e}", exc_info=True)
        bot.tg.send(f"❌ Error scanner: `{str(e)[:200]}`")

    interval = cfg.CYCLE_MINUTES * 60
    logger.info(f"Loop cada {cfg.CYCLE_MINUTES} minutos")

    while True:
        try:
            time.sleep(interval)
            bot.run_cycle()
        except KeyboardInterrupt:
            bot.pos_mgr.close_all("shutdown")
            bot.tg.send("🔴 *Bot DETENIDO*")
            break
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            bot.tg.send(f"⚠️ Error: `{str(e)[:200]}`")
            time.sleep(30)

if __name__ == "__main__":
    main()
