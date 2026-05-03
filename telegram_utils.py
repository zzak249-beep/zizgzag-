import requests
import logging
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

log = logging.getLogger(__name__)


def send(message: str) -> None:
    """Envía un mensaje al chat de Telegram configurado."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram no configurado (TOKEN o CHAT_ID vacío).")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        }, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        log.error(f"Error enviando Telegram: {e}")


def signal_msg(symbol: str, side: str, price: float) -> str:
    icon = "🟢" if side == "buy" else "🔴"
    action = "LONG ▲" if side == "buy" else "SHORT ▼"
    return (
        f"{icon} <b>SEÑAL DETECTADA</b>\n"
        f"Par: <code>{symbol}</code>\n"
        f"Dirección: <b>{action}</b>\n"
        f"Precio: <code>{price:.4f}</code>"
    )


def order_msg(symbol: str, side: str, amount: float,
              price: float, order_id: str) -> str:
    icon = "✅"
    action = "LONG ▲" if side == "buy" else "SHORT ▼"
    return (
        f"{icon} <b>ORDEN EJECUTADA</b>\n"
        f"Par: <code>{symbol}</code>\n"
        f"Dirección: <b>{action}</b>\n"
        f"Cantidad: <code>{amount} USDT</code>\n"
        f"Precio: <code>{price:.4f}</code>\n"
        f"ID: <code>{order_id}</code>"
    )


def error_msg(context: str, err: Exception) -> str:
    return (
        f"⚠️ <b>ERROR</b>\n"
        f"Contexto: <code>{context}</code>\n"
        f"Detalle: <code>{str(err)[:200]}</code>"
    )


def startup_msg(pairs: list[str]) -> str:
    pair_list = "\n".join(f"  • {p}" for p in pairs)
    return (
        f"🤖 <b>Bot BingX iniciado</b>\n"
        f"Escaneando {len(pairs)} pares:\n{pair_list}"
    )
