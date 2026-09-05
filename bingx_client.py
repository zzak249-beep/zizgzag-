"""
bingx_client.py — Cliente REST para BingX Perpetual Futures (USDT-M).

Notas de implementación importantes (aprendidas de bots anteriores en
esta misma cuenta, ver README):

1. TODO va en el query string de la URL, incluida la firma, incluso en
   peticiones POST. El body se manda siempre vacío. Si se deja que
   `requests` serialice el dict por su cuenta (params=... o data=...)
   puede re-codificarlo de forma distinta a como se firmó -> error de
   firma 100001. Por eso aquí se construye la URL final a mano y se
   pasa tal cual, sin params/data adicionales.
2. Cuenta en modo Hedge: para abrir se usa side=BUY/SELL con
   positionSide=LONG/SHORT; para cerrar se manda el side contrario
   manteniendo el mismo positionSide (NUNCA reduceOnly ni
   positionSide=BOTH).
3. Los klines se devuelven como lista de objetos con claves nombradas
   ("open","close","high","low","volume","time") y NO como arrays
   posicionales — nótese que "close" va antes que "high" en la
   respuesta real de BingX. Aquí se parsea siempre por nombre de
   clave para no repetir ese bug.
4. Se manda siempre recvWindow para evitar firmas rechazadas por
   desfases de reloj.
5. Símbolos en formato "BASE-QUOTE" (ej. "BTC-USDT"). En DEMO_MODE se
   usa el sufijo "-VST" (saldo de práctica de BingX) en vez de
   "-USDT", sobre el mismo API.
6. El SL y el TP van como ÓRDENES CONDICIONALES SEPARADAS con stopPrice
   numérico, no como JSON embebido en la orden de entrada. El JSON
   embebido obliga a decidir si se percent-codifica antes de firmar, y
   ahí es donde se rompen las firmas. A cambio queda una ventana entre
   la entrada y el SL en la que la posición está desnuda -> por eso
   open_protected_position() verifica y, si no consigue proteger,
   CIERRA.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any, Optional
from urllib.parse import quote_plus

import requests

logger = logging.getLogger("wavelet_bot.bingx")

# Códigos de error de negocio de BingX que el bot necesita reconocer
# explícitamente (el resto se trata como error genérico).
ERR_SIGNATURE_FAILED = 100001
ERR_INSUFFICIENT_BALANCE = 100202
ERR_INVALID_PARAMETER = 100400
ERR_PRICE_DEVIATION = 100440
ERR_POSITION_NOT_EXIST = 109420
# En modo Hedge, BingX rechaza cualquier orden que lleve reduceOnly -- y,
# según cuenta, también closePosition. Se reconoce para reintentar con el
# equivalente basado en quantity.
ERR_HEDGE_FIELD_NOT_ALLOWED = 109400


class BingXAPIError(Exception):
    def __init__(self, code: int, msg: str, path: str):
        self.code = code
        self.msg = msg
        self.path = path
        super().__init__(f"BingX [{path}] code={code} msg={msg}")


class BingXClient:
    def __init__(self, api_key: str, api_secret: str, base_url: str, recv_window_ms: int = 5000,
                 timeout: float = 15.0, max_retries: int = 3, pool_maxsize: int = 32):
        self.api_key = api_key
        self.api_secret = api_secret.encode("utf-8")
        self.base_url = base_url.rstrip("/")
        self.recv_window_ms = recv_window_ms
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = requests.Session()
        self._session.headers.update({"X-BX-APIKEY": self.api_key})

        # Pool de conexiones dimensionado a la concurrencia real. El
        # default de requests es pool_maxsize=10; el escaneo lanza un
        # hilo por símbolo del lote (SYMBOL_BATCH_SIZE, 20 por defecto),
        # así que las conexiones que no caben se crean, se usan y se
        # descartan -- un handshake TLS nuevo por llamada y el log lleno
        # de "Connection pool is full, discarding connection".
        #
        # pool_block=True: si no hay hueco, el hilo ESPERA en vez de
        # abrir una conexión de usar y tirar. Además actúa como freno
        # natural contra el rate limit de BingX.
        adaptador = requests.adapters.HTTPAdapter(
            pool_connections=pool_maxsize,
            pool_maxsize=pool_maxsize,
            pool_block=True,
            max_retries=0,   # los reintentos los gestiona _request
        )
        self._session.mount("https://", adaptador)
        self._session.mount("http://", adaptador)
        self._contract_cache: dict[str, dict] = {}
        self._contract_cache_ts: float = 0.0

    # ── Firma ────────────────────────────────────────────────────────
    @staticmethod
    def _build_query_string(params: dict[str, Any]) -> str:
        # Orden ALFABÉTICO por clave: verificado con el vector de prueba
        # oficial de BingX (REST API.md) — con orden de inserción
        # arbitrario la firma NO coincide con la esperada por el
        # servidor. Además de firmar, esta es la misma cadena que se
        # manda, así que ambas quedan siempre consistentes entre sí.
        parts = []
        for key, value in sorted(params.items()):
            if value is None:
                continue
            if isinstance(value, bool):
                value = "true" if value else "false"
            elif isinstance(value, float):
                # format(x, "f") por defecto trunca a 6 decimales, lo que
                # PIERDE precisión real en cantidades pequeñas (monedas de
                # precio alto). 8 decimales cubre la máxima precisión real
                # de cualquier contrato BingX sin exponer ruido de coma
                # flotante binaria.
                value = f"{value:.8f}".rstrip("0").rstrip(".")
                if value == "" or value == "-":
                    value = "0"
            parts.append(f"{key}={quote_plus(str(value))}")
        return "&".join(parts)

    def _sign(self, query_string: str) -> str:
        return hmac.new(self.api_secret, query_string.encode("utf-8"), hashlib.sha256).hexdigest()

    def _request(self, method: str, path: str, params: Optional[dict[str, Any]] = None,
                 signed: bool = True) -> Any:
        params = dict(params or {})
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = self.recv_window_ms

        query_string = self._build_query_string(params)

        if signed:
            signature = self._sign(query_string)
            query_string = f"{query_string}&signature={signature}"

        url = f"{self.base_url}{path}"
        if query_string:
            url = f"{url}?{query_string}"

        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._session.request(method, url, timeout=self.timeout)
                data = resp.json()
                code = data.get("code")
                if code not in (0, None):
                    raise BingXAPIError(code, data.get("msg", ""), path)
                return data.get("data", data)
            except BingXAPIError:
                raise  # error de negocio de BingX: no tiene sentido reintentar igual
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                wait = min(2 ** attempt, 8)
                logger.warning("Fallo de red en %s (intento %d/%d): %s — reintento en %ds",
                                path, attempt, self.max_retries, exc, wait)
                time.sleep(wait)
        raise RuntimeError(f"No se pudo completar {path} tras {self.max_retries} intentos: {last_exc}")

    # ── Mercado ──────────────────────────────────────────────────────
    def get_contracts(self) -> list[dict]:
        """Lista de contratos USDT-M con precisión de cantidad/precio y mínimos."""
        return self._request("GET", "/openApi/swap/v2/quote/contracts", signed=False)

    def get_contract(self, symbol: str, ttl_seconds: float = 3600.0) -> dict:
        """Ficha de UN contrato, cacheada. Se busca por símbolo dentro de la
        lista completa: el endpoint puede ignorar el filtro y devolverlos
        todos, y coger el primero a ciegas daría la precisión de otro
        contrato -> cantidades mal redondeadas y órdenes rechazadas."""
        now = time.time()
        if not self._contract_cache or (now - self._contract_cache_ts) > ttl_seconds:
            try:
                rows = self.get_contracts() or []
                self._contract_cache = {
                    str(r.get("symbol")): r for r in rows if isinstance(r, dict) and r.get("symbol")
                }
                self._contract_cache_ts = now
            except Exception:
                logger.exception("No se pudo refrescar la lista de contratos")
        return self._contract_cache.get(symbol, {})

    def round_qty(self, symbol: str, qty: float) -> float:
        c = self.get_contract(symbol)
        try:
            precision = int(c.get("quantityPrecision", 3) or 0)
        except (TypeError, ValueError):
            precision = 3
        try:
            min_qty = float(c.get("tradeMinQuantity", c.get("minQty", 0)) or 0)
        except (TypeError, ValueError):
            min_qty = 0.0
        rounded = round(float(qty), precision)
        if min_qty and 0 < rounded < min_qty:
            rounded = min_qty
        return rounded

    def round_price(self, symbol: str, price: float) -> float:
        c = self.get_contract(symbol)
        try:
            precision = int(c.get("pricePrecision", 6) or 0)
        except (TypeError, ValueError):
            precision = 6
        return round(float(price), precision)

    def get_klines(self, symbol: str, interval: str, limit: int = 200) -> list[dict]:
        """
        Devuelve velas ordenadas cronológicamente ascendente (la más
        antigua primero, la más reciente al final), cada una como dict
        con claves: open, high, low, close, volume, time (ms).
        """
        raw = self._request(
            "GET", "/openApi/swap/v2/quote/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
            signed=False,
        )
        candles = [
            {
                "time": int(c["time"]),
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "volume": float(c["volume"]),
            }
            for c in raw
        ]
        candles.sort(key=lambda c: c["time"])
        return candles

    # ── Cuenta ───────────────────────────────────────────────────────
    def get_balance(self) -> dict:
        """Balance de la cuenta USDT-M (equity, disponible, etc.)."""
        data = self._request("GET", "/openApi/swap/v2/user/balance", {})
        # La respuesta trae {"balance": {...}} en algunas cuentas y
        # el dict plano en otras; se normaliza aquí.
        if isinstance(data, dict) and "balance" in data:
            return data["balance"]
        return data

    def get_positions(self, symbol: Optional[str] = None) -> list[dict]:
        params = {"symbol": symbol} if symbol else {}
        data = self._request("GET", "/openApi/swap/v2/user/positions", params)
        return data or []

    def get_position_amt(self, symbol: str, position_side: str) -> float:
        """Tamaño REAL abierto en BingX para ese símbolo y lado (absoluto).
        0.0 si no hay nada. Cerrar SIEMPRE con esto, nunca con la cantidad
        calculada al abrir: difiere por fills parciales o por un SL/TP que
        ya redujo parte de la posición."""
        wanted = (position_side or "").upper()
        try:
            positions = self.get_positions(symbol)
        except Exception:
            logger.exception("No se pudo leer la posición real de %s", symbol)
            return 0.0
        for p in positions:
            if str(p.get("symbol")) != symbol:
                continue
            side = str(p.get("positionSide", "")).upper()
            if side and side != wanted:
                continue
            amt = float(p.get("positionAmt", 0) or 0)
            if amt:
                return abs(amt)
        return 0.0

    def get_open_orders(self, symbol: str) -> list[dict]:
        data = self._request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol})
        if isinstance(data, dict):
            data = data.get("orders", [])
        return data if isinstance(data, list) else []

    def has_stop_and_take_profit(self, symbol: str, position_side: str = None) -> tuple[bool, bool]:
        """(hay_sl, hay_tp) para el símbolo/lado. Se consulta DESPUÉS de
        colocar las condicionales: BingX puede aceptar la orden de entrada
        y rechazar la protección, y sin esta comprobación la posición se
        queda desnuda sin que nadie se entere."""
        wanted = (position_side or "").upper()
        try:
            orders = self.get_open_orders(symbol)
        except Exception:
            logger.exception("No se pudo verificar SL/TP de %s", symbol)
            return (False, False)
        has_sl = has_tp = False
        for o in orders:
            side = str(o.get("positionSide", "")).upper()
            if wanted and side and side != wanted:
                continue
            otype = str(o.get("type", "")).upper()
            if "TAKE" in otype:
                has_tp = True
            elif "STOP" in otype:
                has_sl = True
        return (has_sl, has_tp)

    def set_leverage(self, symbol: str, side: str, leverage: int) -> None:
        """side: 'LONG' o 'SHORT' (modo Hedge)."""
        self._request("POST", "/openApi/swap/v2/trade/leverage", {
            "symbol": symbol, "side": side, "leverage": leverage,
        })

    # ── Trading ──────────────────────────────────────────────────────
    def place_market_order(self, symbol: str, side: str, position_side: str, quantity: float) -> dict:
        return self._request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol,
            "side": side,                # BUY / SELL
            "positionSide": position_side,  # LONG / SHORT
            "type": "MARKET",
            "quantity": quantity,
        })

    def place_stop_market(self, symbol: str, side: str, position_side: str,
                           stop_price: float, close_position: bool = False,
                           quantity: Optional[float] = None) -> dict:
        """close_position=False por defecto: en Hedge, closePosition puede
        rechazarse igual que reduceOnly (109400). Con quantity explícita
        funciona en ambos modos."""
        params = {
            "symbol": symbol,
            "side": side,
            "positionSide": position_side,
            "type": "STOP_MARKET",
            "stopPrice": self.round_price(symbol, stop_price),
        }
        if close_position:
            params["closePosition"] = True
        elif quantity is not None:
            params["quantity"] = self.round_qty(symbol, quantity)
        return self._request("POST", "/openApi/swap/v2/trade/order", params)

    def place_take_profit_market(self, symbol: str, side: str, position_side: str,
                                  stop_price: float, close_position: bool = False,
                                  quantity: Optional[float] = None) -> dict:
        params = {
            "symbol": symbol,
            "side": side,
            "positionSide": position_side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": self.round_price(symbol, stop_price),
        }
        if close_position:
            params["closePosition"] = True
        elif quantity is not None:
            params["quantity"] = self.round_qty(symbol, quantity)
        return self._request("POST", "/openApi/swap/v2/trade/order", params)

    def cancel_all_open_orders(self, symbol: str) -> dict:
        return self._request("DELETE", "/openApi/swap/v2/trade/allOpenOrders", {"symbol": symbol})

    def close_position_market(self, symbol: str, side: str, position_side: str,
                               quantity: Optional[float] = None) -> Optional[dict]:
        """Cierre a mercado. Si no se pasa quantity, se lee el tamaño real.

        Se intenta primero con quantity (funciona en Hedge y en One-Way).
        Solo si eso falla se prueba closePosition, que en Hedge puede dar
        109400 igual que reduceOnly.
        """
        position_side = (position_side or "").upper()
        if quantity is None:
            quantity = self.get_position_amt(symbol, position_side)
        quantity = abs(float(quantity or 0))
        if quantity <= 0:
            logger.info("%s (%s): no hay posición que cerrar", symbol, position_side)
            return None
        base = {
            "symbol": symbol,
            "side": side,
            "positionSide": position_side,
            "type": "MARKET",
        }
        try:
            return self._request("POST", "/openApi/swap/v2/trade/order",
                                 {**base, "quantity": self.round_qty(symbol, quantity)})
        except BingXAPIError as exc:
            if exc.code == ERR_POSITION_NOT_EXIST:
                logger.info("%s (%s): la posición ya no existe", symbol, position_side)
                return None
            logger.warning("%s: cierre por quantity falló (%s) — probando closePosition",
                           symbol, exc)
            return self._request("POST", "/openApi/swap/v2/trade/order",
                                 {**base, "closePosition": True})

    def close_position_and_verify(self, symbol: str, position_side: str,
                                   retries: int = 2) -> bool:
        """Cierra y COMPRUEBA contra BingX que quedó en cero, cancelando
        después las condicionales huérfanas (si se quedan vivas pueden
        dispararse más tarde y ABRIR una posición nueva)."""
        position_side = (position_side or "").upper()
        exit_side = "SELL" if position_side == "LONG" else "BUY"
        for attempt in range(1, retries + 2):
            try:
                self.close_position_market(symbol, exit_side, position_side)
            except Exception:
                logger.exception("%s (%s): fallo enviando el cierre (intento %d)",
                                 symbol, position_side, attempt)
            time.sleep(1.0)
            if self.get_position_amt(symbol, position_side) <= 0:
                try:
                    self.cancel_all_open_orders(symbol)
                except Exception:
                    logger.warning("%s: cerrada, pero quedaron condicionales sin cancelar", symbol)
                logger.info("%s (%s): cerrada y verificada", symbol, position_side)
                return True
        logger.error("%s (%s): NO se pudo cerrar tras %d intentos — REVISAR A MANO",
                     symbol, position_side, retries + 1)
        return False

    # ── Apertura protegida ───────────────────────────────────────────
    def open_protected_position(self, symbol: str, position_side: str, quantity: float,
                                 stop_loss: float, take_profit: float,
                                 leverage: Optional[int] = None) -> dict:
        """Abre y NO deja la posición sin protección.

        Secuencia: apalancamiento -> entrada a mercado -> leer el tamaño
        REAL rellenado -> SL -> TP -> verificar contra openOrders que ambos
        existen. Si al terminar falta el SL, la posición se CIERRA: una
        entrada sin stop en una cuenta cruzada es peor que no haber
        entrado.

        Devuelve {"ok", "quantity", "has_sl", "has_tp", "closed", "error"}.
        """
        position_side = (position_side or "").upper()
        if position_side not in ("LONG", "SHORT"):
            raise ValueError(f"position_side inválido: {position_side}")
        entry_side = "BUY" if position_side == "LONG" else "SELL"
        exit_side = "SELL" if position_side == "LONG" else "BUY"
        result = {"ok": False, "quantity": 0.0, "has_sl": False,
                  "has_tp": False, "closed": False, "error": None}

        # Coherencia dirección/precios: un SL al otro lado del precio de
        # entrada se dispara al instante y cierra nada más abrir.
        if position_side == "LONG" and not (stop_loss < take_profit):
            raise ValueError(f"{symbol} LONG: SL {stop_loss} debe ser < TP {take_profit}")
        if position_side == "SHORT" and not (stop_loss > take_profit):
            raise ValueError(f"{symbol} SHORT: SL {stop_loss} debe ser > TP {take_profit}")

        if leverage:
            try:
                self.set_leverage(symbol, position_side, int(leverage))
            except Exception as exc:
                # No aborta: BingX devuelve error si ya estaba en ese valor.
                logger.warning("%s: no se pudo fijar apalancamiento %s (%s)",
                               symbol, leverage, exc)

        qty = self.round_qty(symbol, quantity)
        if qty <= 0:
            result["error"] = "cantidad redondeada a 0"
            return result

        try:
            self.place_market_order(symbol, entry_side, position_side, qty)
        except Exception as exc:
            result["error"] = f"entrada rechazada: {exc}"
            logger.error("%s (%s): %s", symbol, position_side, result["error"])
            return result

        # Tamaño REAL rellenado. Proteger por la cantidad pedida dejaría
        # parte de la posición sin cubrir si el fill fue parcial.
        time.sleep(0.5)
        filled = self.get_position_amt(symbol, position_side)
        if filled <= 0:
            result["error"] = "la entrada no aparece como posición abierta"
            logger.error("%s (%s): %s", symbol, position_side, result["error"])
            return result
        result["quantity"] = filled

        for label, placer, price in (
            ("SL", self.place_stop_market, stop_loss),
            ("TP", self.place_take_profit_market, take_profit),
        ):
            for attempt in (1, 2):
                try:
                    placer(symbol, exit_side, position_side, price, quantity=filled)
                    break
                except Exception as exc:
                    logger.warning("%s (%s): fallo colocando %s (intento %d): %s",
                                   symbol, position_side, label, attempt, exc)
                    time.sleep(0.5)

        has_sl, has_tp = self.has_stop_and_take_profit(symbol, position_side)
        result["has_sl"], result["has_tp"] = has_sl, has_tp

        if not has_sl:
            logger.error("%s (%s): SIN STOP tras abrir — cerrando la posición",
                         symbol, position_side)
            result["closed"] = self.close_position_and_verify(symbol, position_side)
            result["error"] = "no se pudo colocar el SL; posición cerrada"
            return result

        if not has_tp:
            # Con SL puesto el riesgo está acotado: se deja abierta y se
            # avisa, en vez de cerrar una operación válida por el TP.
            logger.warning("%s (%s): abierta con SL pero SIN TP", symbol, position_side)

        result["ok"] = True
        return result

    # ── Auditoría ────────────────────────────────────────────────────
    def find_unprotected_positions(self) -> list[dict]:
        """Posiciones abiertas sin SL. SOLO informa — no cierra nada.

        Quien llame decide qué hacer, y debe filtrar por las que el bot
        abrió de verdad: esta cuenta también lleva operaciones manuales, y
        cerrarlas automáticamente sería destruir operativa ajena al bot.
        """
        salida = []
        try:
            positions = self.get_positions()
        except Exception:
            logger.exception("No se pudieron listar las posiciones")
            return salida
        for p in positions:
            amt = float(p.get("positionAmt", 0) or 0)
            if amt == 0:
                continue
            symbol = str(p.get("symbol"))
            side = str(p.get("positionSide", "")).upper()
            has_sl, has_tp = self.has_stop_and_take_profit(symbol, side)
            if not has_sl:
                salida.append({"symbol": symbol, "positionSide": side,
                               "quantity": abs(amt), "has_tp": has_tp})
        return salida
