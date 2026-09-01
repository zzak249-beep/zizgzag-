"""
Wavelet MRA Haar 5m — Webhook receiver para TradingView -> BingX + Telegram.

Flujo:
  TradingView (alert() del Pine, formato JSON) --POST--> /webhook/<WEBHOOK_SECRET>
  -> valida y parsea el JSON
  -> si AUTO_TRADE=true: ejecuta en BingX (con circuit breaker + sizing)
  -> en todos los casos: manda la señal a Telegram (para operar manualmente
     si AUTO_TRADE=false, o como confirmación si AUTO_TRADE=true)
  -> persiste el estado para reconciliación tras un restart de Railway

Configura la alerta en TradingView con "Webhook URL":
  https://<tu-app>.up.railway.app/webhook/<WEBHOOK_SECRET>
y como mensaje: {{strategy.order.alert_message}}  (o deja que sea el propio
JSON que genera `alert(json_..., alert.freq_once_per_bar_close)` del script;
en ese caso usa "Any alert() function call" al crear la alerta).
"""
import logging
import sys

from flask import Flask, jsonify, request

import bingx_client
import config
import telegram_notifier
from state_manager import StateManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("main")

app = Flask(__name__)
bx = bingx_client.BingXClient()
state = StateManager()

# Reconciliación al arrancar (best-effort; si BingX no está configurado,
# se registra el error y el bot sigue en modo señal/Telegram).
if config.BINGX_API_KEY:
    try:
        state.reconcile(bx)
    except Exception:
        log.exception("Reconciliación inicial falló, continuando de todas formas")


# --------------------------------------------------------------------------- #
@app.route("/", methods=["GET"])
def health():
    return jsonify(
        status="ok",
        auto_trade=config.AUTO_TRADE,
        open_positions=state.open_count(),
        halted=state.state.get("trading_halted"),
    )


@app.route("/webhook/<secret>", methods=["POST"])
def webhook(secret):
    if not config.WEBHOOK_SECRET or secret != config.WEBHOOK_SECRET:
        log.warning("Intento de webhook con secret inválido")
        return jsonify(error="unauthorized"), 401

    payload = request.get_json(silent=True)
    if payload is None:
        # TradingView a veces manda el JSON como texto plano
        try:
            import json as _json
            payload = _json.loads(request.data.decode("utf-8"))
        except Exception:
            log.error("Payload no parseable: %s", request.data)
            return jsonify(error="invalid json"), 400

    log.info("Alerta recibida: %s", payload)

    signal = payload.get("signal")
    try:
        if signal == "entry":
            _handle_entry(payload)
        elif signal == "exit":
            _handle_exit(payload)
        else:
            log.warning("Tipo de señal desconocido: %s", signal)
            return jsonify(error="unknown signal type"), 400
    except Exception as e:
        log.exception("Error procesando la alerta")
        telegram_notifier.send(f"🚨 Error procesando alerta: `{e}`\nPayload: `{payload}`")
        return jsonify(error=str(e)), 500

    return jsonify(status="processed"), 200


# --------------------------------------------------------------------------- #
def _handle_entry(alert: dict):
    tv_symbol = alert["symbol"]
    symbol = config.tv_symbol_to_bingx(tv_symbol)
    position_side = alert["positionSide"]          # LONG / SHORT
    side = "BUY" if position_side == "LONG" else "SELL"
    price = float(alert["price"])
    sl = float(alert["sl"])
    tp = float(alert["tp"])

    if not config.AUTO_TRADE:
        telegram_notifier.send(
            telegram_notifier.format_entry_signal(alert, executed=False)
        )
        return

    if state.get_open(symbol):
        telegram_notifier.send(
            telegram_notifier.format_entry_signal(
                alert, executed=False, error=f"ya hay posición abierta en {symbol}"
            )
        )
        return

    if state.open_count() >= config.MAX_CONCURRENT_POSITIONS:
        telegram_notifier.send(
            telegram_notifier.format_entry_signal(
                alert, executed=False, error="límite de posiciones concurrentes alcanzado"
            )
        )
        return

    try:
        equity = bx.get_balance()
    except Exception as e:
        telegram_notifier.send(
            telegram_notifier.format_entry_signal(alert, executed=False, error=f"no se pudo leer balance: {e}")
        )
        return

    allowed, reason = state.check_circuit_breaker(equity)
    if not allowed:
        telegram_notifier.send(f"⛔ Trading pausado (circuit breaker): {reason}")
        return

    # Sizing: arriesgar RISK_PCT_PER_TRADE% del equity en la distancia al SL.
    risk_amount = equity * (config.RISK_PCT_PER_TRADE / 100)
    stop_distance = abs(price - sl)
    if stop_distance <= 0:
        telegram_notifier.send(
            telegram_notifier.format_entry_signal(alert, executed=False, error="distancia a SL inválida")
        )
        return
    qty = round(risk_amount / stop_distance, 3)
    if qty <= 0:
        telegram_notifier.send(
            telegram_notifier.format_entry_signal(alert, executed=False, error="qty calculada es 0")
        )
        return

    try:
        bx.set_leverage(symbol, position_side, config.LEVERAGE)
        bx.place_market_order(
            symbol, side, position_side, qty, stop_loss=sl, take_profit=tp
        )
    except Exception as e:
        log.exception("Fallo abriendo orden en BingX")
        telegram_notifier.send(
            telegram_notifier.format_entry_signal(alert, executed=False, error=str(e))
        )
        return

    state.record_open(symbol, position_side, qty, price, sl, tp)
    telegram_notifier.send(
        telegram_notifier.format_entry_signal(alert, executed=True, qty=qty)
    )


def _handle_exit(alert: dict):
    tv_symbol = alert["symbol"]
    symbol = config.tv_symbol_to_bingx(tv_symbol)
    position_side = alert["positionSide"]

    if not config.AUTO_TRADE:
        telegram_notifier.send(telegram_notifier.format_exit_signal(alert, executed=False))
        return

    pos = state.get_open(symbol)
    if not pos:
        # puede que ya se haya cerrado por SL/TP directamente en BingX; solo avisamos
        telegram_notifier.send(
            telegram_notifier.format_exit_signal(
                alert, executed=False, error="no había posición registrada localmente (¿cerrada ya por SL/TP?)"
            )
        )
        return

    try:
        exit_price = float(alert.get("price", 0))
        bx.close_position(symbol, position_side, pos["qty"])
        entry_price = pos["entry_price"]
        pnl = (exit_price - entry_price) if position_side == "LONG" else (entry_price - exit_price)
    except Exception as e:
        log.exception("Fallo cerrando orden en BingX")
        telegram_notifier.send(
            telegram_notifier.format_exit_signal(alert, executed=False, error=str(e))
        )
        return

    state.record_close(symbol, pnl=pnl)
    telegram_notifier.send(telegram_notifier.format_exit_signal(alert, executed=True))


# --------------------------------------------------------------------------- #
@app.route("/reset-breaker/<secret>", methods=["POST"])
def reset_breaker(secret):
    """Endpoint manual para reactivar el trading tras un circuit breaker."""
    if secret != config.WEBHOOK_SECRET:
        return jsonify(error="unauthorized"), 401
    state.manual_reset_breaker()
    telegram_notifier.send("✅ Circuit breaker reseteado manualmente.")
    return jsonify(status="reset"), 200


if __name__ == "__main__":
    import os

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
