"""
telegram_notifier.py — Notificaciones del Sweep Reversal Map Bot.

Interfaz que espera main.py:
    TelegramNotifier(token, chat_id)
      .info(texto)
      .signal(symbol, side, entry, sl, tp, executed, reason=None)
      .exit_notice(symbol, side, exit_price)
      .error(contexto, mensaje)

REGLA: nada de aquí puede romper el flujo de trading. Si Telegram está
caído o rechaza el formato, se registra en el log y se sigue. Perder un
aviso es molesto; no cerrar una posición porque falló un aviso, no.
"""
import logging

import requests

logger = logging.getLogger("sweep_bot.telegram")

TIMEOUT = 10
API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_LEN = 3900  # Telegram corta en 4096 y devuelve error si se pasa


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.token = (token or "").strip()
        self.chat_id = (chat_id or "").strip()
        if not self.token or not self.chat_id:
            logger.warning("Telegram no configurado: los avisos solo irán al log.")

    # ------------------------------------------------------------------ #
    def _send(self, texto: str) -> bool:
        if not self.token or not self.chat_id:
            logger.info("[telegram no configurado] %s", texto.replace("\n", " | ")[:200])
            return False

        if len(texto) > MAX_LEN:
            texto = texto[:MAX_LEN] + "\n… (truncado)"

        url = API.format(token=self.token)
        # Segundo intento sin Markdown: la causa más común de rechazo es un
        # carácter de formato dentro de un mensaje de error.
        for parse_mode in ("Markdown", None):
            payload = {"chat_id": self.chat_id, "text": texto,
                       "disable_web_page_preview": True}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            try:
                resp = requests.post(url, json=payload, timeout=TIMEOUT)
                if resp.ok:
                    return True
                logger.warning("Telegram rechazó el mensaje (parse_mode=%s): %s",
                               parse_mode, resp.text[:200])
            except Exception:
                logger.exception("Fallo de red enviando a Telegram")
                return False
        return False

    @staticmethod
    def _num(v) -> str:
        """%.6g mantiene legibles tanto BTC (79380) como tokens de precio
        muy bajo (0.000012), sin notación científica en el rango habitual."""
        try:
            return f"{float(v):.6g}"
        except (TypeError, ValueError):
            return str(v)

    # ------------------------------------------------------------------ #
    def info(self, texto: str) -> bool:
        return self._send(str(texto))

    def signal(self, symbol, side, entry, sl, tp, executed: bool, reason: str = None) -> bool:
        emoji = "🟢" if str(side).upper() == "LONG" else "🔴"
        lineas = [
            f"{emoji} *{str(side).upper()} {symbol}* — Sweep Reversal 5m",
            f"Entrada: `{self._num(entry)}`",
            f"SL: `{self._num(sl)}`  TP: `{self._num(tp)}`",
        ]
        if executed:
            lineas.insert(1, "✅ *EJECUTADA con SL/TP confirmados*")
        elif reason:
            lineas.append(f"⚠️ NO ejecutada — {reason}")
        else:
            lineas.append("ℹ️ No ejecutada — señal informativa")
        return self._send("\n".join(lineas))

    def exit_notice(self, symbol, side, exit_price) -> bool:
        return self._send(
            f"🏁 *Cerrada {str(side).upper()} {symbol}*\n"
            f"Precio de salida aprox.: `{self._num(exit_price)}`\n"
            f"_Detectado por reconciliación (SL/TP ejecutado en BingX)._"
        )

    def error(self, contexto: str, mensaje: str) -> bool:
        return self._send(f"🚨 *Error en {contexto}*\n`{str(mensaje)[:600]}`")
