"""
Cliente mínimo para BingX Perpetual Swap (v2).
Firma HMAC-SHA256: el query string se construye UNA sola vez, ordenado,
y se usa exactamente igual para firmar y para enviar (evita el bug clásico
de firmar en un orden y transmitir en otro).
"""
import hashlib
import hmac
import logging
import time
from urllib.parse import urlencode

import requests

import config

log = logging.getLogger("bingx")

TIMEOUT = 15


class BingXError(Exception):
    pass


class BingXClient:
    def __init__(self, api_key=None, api_secret=None, base_url=None):
        # .strip() defensivo: una key/secret con un '\n' o espacio colado
        # (típico al pegar variables en Railway) rompe la cabecera HTTP
        # X-BX-APIKEY con un ValueError críptico en pleno reconcile/entrada.
        self.api_key = (api_key or config.BINGX_API_KEY or "").strip()
        self.api_secret = (api_secret or config.BINGX_API_SECRET or "").strip()
        self.base_url = (base_url or config.BINGX_BASE_URL or "").strip()
        if not self.api_key or not self.api_secret:
            log.warning("BINGX_API_KEY / BINGX_API_SECRET no configuradas.")

    # ------------------------------------------------------------------ #
    def _signed_request(self, method: str, path: str, params: dict):
        params = {k: v for k, v in params.items() if v is not None}
        params["timestamp"] = str(int(time.time() * 1000))
        params["recvWindow"] = params.get("recvWindow", "10000")

        # orden fijo (sorted) usado TANTO para firmar COMO para transmitir
        ordered_items = sorted(params.items())
        query_string = urlencode(ordered_items)

        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        url = f"{self.base_url}{path}?{query_string}&signature={signature}"
        headers = {"X-BX-APIKEY": self.api_key}

        try:
            resp = requests.request(method, url, headers=headers, timeout=TIMEOUT)
            data = resp.json()
        except Exception as e:
            raise BingXError(f"Fallo de red/parseo llamando a {path}: {e}") from e

        if data.get("code") not in (0, None):
            raise BingXError(f"BingX API error en {path}: {data}")
        return data.get("data", data)

    # ------------------------------------------------------------------ #
    def get_balance(self) -> float:
        """Devuelve el equity disponible en USDT de la cuenta de swap."""
        data = self._signed_request("GET", "/openApi/swap/v2/user/balance", {})
        balances = data.get("balance", data)
        if isinstance(balances, dict):
            return float(balances.get("equity", balances.get("balance", 0)))
        if isinstance(balances, list):
            for b in balances:
                if b.get("asset") == "USDT":
                    return float(b.get("equity", b.get("balance", 0)))
        return 0.0

    def get_positions(self, symbol: str = None):
        params = {"symbol": symbol} if symbol else {}
        data = self._signed_request("GET", "/openApi/swap/v2/user/positions", params)
        return data if isinstance(data, list) else data.get("positions", [])

    def set_leverage(self, symbol: str, side: str, leverage: int):
        """side: 'LONG' o 'SHORT' (modo hedge, que es el que usa este bot)."""
        return self._signed_request(
            "POST",
            "/openApi/swap/v2/trade/leverage",
            {"symbol": symbol, "side": side, "leverage": leverage},
        )

    def set_margin_mode(self, symbol: str, mode: str = "ISOLATED"):
        return self._signed_request(
            "POST",
            "/openApi/swap/v2/trade/marginType",
            {"symbol": symbol, "marginType": mode},
        )

    def place_market_order(
        self,
        symbol: str,
        side: str,           # "BUY" / "SELL"
        position_side: str,  # "LONG" / "SHORT"
        quantity: float,
        stop_loss: float = None,
        take_profit: float = None,
        reduce_only: bool = False,
    ):
        params = {
            "symbol": symbol,
            "side": side,
            "positionSide": position_side,
            "type": "MARKET",
            "quantity": quantity,
            "reduceOnly": "true" if reduce_only else "false",
        }
        if stop_loss:
            params["stopLoss"] = (
                '{"type":"STOP_MARKET","stopPrice":%s,"workingType":"MARK_PRICE"}'
                % stop_loss
            )
        if take_profit:
            params["takeProfit"] = (
                '{"type":"TAKE_PROFIT_MARKET","stopPrice":%s,"workingType":"MARK_PRICE"}'
                % take_profit
            )
        return self._signed_request("POST", "/openApi/swap/v2/trade/order", params)

    def close_position(self, symbol: str, position_side: str, quantity: float):
        """Cierra con una orden de mercado reduceOnly en sentido contrario."""
        side = "SELL" if position_side == "LONG" else "BUY"
        return self.place_market_order(
            symbol, side, position_side, quantity, reduce_only=True
        )

    def get_symbol_filters(self, symbol: str):
        """Precisión de cantidad/precio para el símbolo (evita rechazos por decimales)."""
        data = self._signed_request(
            "GET", "/openApi/swap/v2/quote/contracts", {"symbol": symbol}
        )
        items = data if isinstance(data, list) else [data]
        return items[0] if items else {}
