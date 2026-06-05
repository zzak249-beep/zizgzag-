"""
main.py — QF×JP v3.4 Bot — Estructura PLANA (todos los archivos en raíz)
=========================================================================
"""
import logging, time, sys, os
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── CONFIG ────────────────────────────────────────────────────────────────────
import config as cfg

logging.basicConfig(
    level=getattr(logging, cfg.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")

# ── IMPORTS PLANOS (sin subcarpetas) ──────────────────────────────────────────
from client           import BingXClient, parse_klines
from indicators       import QFJPIndicators
from qfxjp_signal     import QFJPScorer
from position_manager import PositionManager
from risk_manager     import RiskManager
from telegram_notifier import TelegramNotifier

# Market mechanics opcional
try:
    from market_context       import MarketContextEngine
    MECHANICS_OK = True
    logger.info("✅ Market mechanics cargado")
except ImportError:
    MECHANICS_OK = False
    logger.warning("⚠️  market_context.py no encontrado — continuando sin él")


# ═══════════════════════════════════════════════════════════════════════════════
#  HEALTH CHECK — Railway requiere puerto abierto
# ═══════════════════════════════════════════════════════════════════════════════

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
    server = HTTPServer(("0.0.0.0", cfg.PORT), HealthHandler)
    logger.info(f"Health check → puerto {cfg.PORT}")
    Thread(target=server.serve_forever, daemon=True).start()


# ═══════════════════════════════════════════════════════════════════════════════
#  BOT
# ═══════════════════════════════════════════════════════════════════════════════

class QFJPBot:
    def __init__(self):
        logger.info("Inicializando QF×JP v3.4...")
        self.client   = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET_KEY, cfg.BINGX_BASE_URL)
        self.tg       = TelegramNotifier(cfg.TELEGRAM_TOKEN, cfg.TELEGRAM_CHAT_ID)
        self.risk_mgr = RiskManager(cfg, self.client)
        self.pos_mgr  = PositionManager(self.client, cfg)
        self.indicators = {s: QFJPIndicators(cfg) for s in cfg.SYMBOLS}
        self.scorers    = {s: QFJPScorer(cfg)     for s in cfg.SYMBOLS}
        self.mechanics  = MarketContextEngine(
            api_key=cfg.BINGX_API_KEY, secret_key=cfg.BINGX_SECRET_KEY
        ) if MECHANICS_OK else None
        self._cycle = 0
        logger.info(f"Símbolos: {cfg.SYMBOLS}")

    def _fetch(self, symbol, tf, limit=300):
        return parse_klines(self.client.get_klines_raw(symbol, tf, limit))

    def run_cycle(self):
        self._cycle += 1
        logger.info(f"═══ Ciclo #{self._cycle} ═══")

        can, reason = self.risk_mgr.can_trade()
        if not can:
            logger.warning(f"[Risk] {reason}")
            if self._cycle % 20 == 0:
                self.tg.risk_alert(reason)
            return

        balance = self.risk_mgr.refresh()
        if balance is None:
            logger.error("Sin balance — saltando ciclo")
            return

        # Verificar posiciones
        prices = {s: self.client.get_last_price(s) for s in cfg.SYMBOLS}
        prices = {k: v for k, v in prices.items() if v}
        self.pos_mgr.check_positions(prices)

        # Scan
        scan_results = []
        new_signals  = []

        for symbol in cfg.SYMBOLS:
            try:
                df   = self._fetch(symbol, cfg.CANDLE_TF, cfg.CANDLE_LIMIT)
                df15 = self._fetch(symbol, cfg.HTF_15M_TF, 50)
                df1h = self._fetch(symbol, cfg.HTF_1H_TF,  50)
                df1w = self._fetch(symbol, cfg.HTF_W_TF,   20)
                df1m = self._fetch(symbol, cfg.HTF_1M_TF,  5)

                if df is None or len(df) < 50:
                    continue

                ind = self.indicators[symbol].compute(df, df15, df1h, df1w, df1m)
                sig = self.scorers[symbol].score(symbol, ind, balance)

                # Market mechanics
                ctx = None
                if self.mechanics and df is not None:
                    try:
                        ctx = self.mechanics.analyze(
                            symbol, ind.price, df,
                            high=float(df["high"].iloc[-1]),
                            low=float(df["low"].iloc[-1]),
                        )
                        if sig.is_valid:
                            if not ctx.entry_allowed(sig.direction):
                                sig.direction = "NONE"; sig.level = "NONE"
                            else:
                                bonus = ctx.score_modifier + ctx.get_judas_bonus(sig.direction)
                                sig.score = max(0, min(100, sig.score + bonus))
                    except Exception as e:
                        logger.debug(f"[Ctx] {symbol}: {e}")

                result = {
                    "symbol": symbol, "sc_l": sig.score_long,
                    "sc_s": sig.score_short, "ses": ind.ses_label,
                    "regime": ind.regime, "level": sig.level if sig.is_valid else None,
                    "signal": sig if sig.is_valid else None, "ctx": ctx,
                }
                scan_results.append(result)
                if sig.is_valid:
                    new_signals.append(result)

                logger.info(
                    f"[Scan] {symbol:12} L:{sig.score_long:3d} S:{sig.score_short:3d} "
                    f"| {ind.ses_label:4} | {ind.regime:8} | {sig.level if sig.is_valid else '----'}"
                )
            except Exception as e:
                logger.error(f"[Bot] {symbol}: {e}", exc_info=True)

        # Resumen cada 5 ciclos
        if self._cycle % 5 == 0:
            self.tg.scan_summary(scan_results)

        # Ejecutar señales
        for result in new_signals:
            signal = result["signal"]
            if self.pos_mgr.open_count >= cfg.MAX_OPEN_TRADES:
                break
            if self.pos_mgr.has_position(signal.symbol):
                continue
            ok = self.pos_mgr.open_position(signal, balance)
            if ok:
                self.tg.signal_entry(signal, result.get("ctx"))

        # Reporte diario 00:00-00:03 UTC
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        if now.hour == 0 and now.minute < cfg.CYCLE_MINUTES:
            self.tg.daily_report(self.risk_mgr.get_stats(),
                                  self.pos_mgr.get_positions_summary())


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    logger.info("╔══════════════════════════════════╗")
    logger.info("║  QF×JP v3.4 Bot  — Railway OK    ║")
    logger.info("╚══════════════════════════════════╝")

    start_health_server()
    bot = QFJPBot()
    bot.tg.bot_started(cfg.SYMBOLS, cfg)

    try:
        bot.run_cycle()
    except Exception as e:
        logger.error(f"Error primer ciclo: {e}", exc_info=True)

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
            logger.error(f"Error en ciclo: {e}", exc_info=True)
            bot.tg.send(f"⚠️ Error: `{str(e)[:200]}`")
            time.sleep(30)

if __name__ == "__main__":
    main()
