"""
QF×JP Bot v6.4.0 — BingX Client
FIRMA: parseParam oficial BingX
  sorted(params) + &timestamp=xxx al final → HMAC → &signature=xxx

CAMBIOS v6.4.0 vs v6.3.3:
  - Detección automática de positionMode al inicio (ONE-WAY vs HEDGE)
  - set_leverage: omite 'side' en ONE-WAY, lo incluye solo en HEDGE
  - place_market_order / place_stop_market_order:
      omiten 'positionSide' en ONE-WAY
  - leverage se envía siempre como str (más seguro entre builds de la API)
  - _round_qty: acepta stepSize real además de volumePrecision
  - Logs de diagnóstico mejorados en cada llamada que puede fallar
"""
import hmac
import hashlib
import math
import time
import asyncio
import logging
from urllib.parse import urlencode
from typing import Optional

import aiohttp
import config as C

log = logging.getLogger("bingx")

# ─────────────────────────────────────────────────────────────────────────────
# Constantes de modo de posición
# ─────────────────────────────────────────────────────────────────────────────

MODE_UNKNOWN  = "UNKNOWN"   # todavía no detectado
MODE_ONE_WAY  = "ONE_WAY"   # cuenta normal (default BingX)
MODE_HEDGE    = "HEDGE"     # cuenta dual-side

# ─────────────────────────────────────────────────────────────────────────────
# FIRMA — protocolo oficial BingX (función parseParam)
# Ref: https://bingx-api.github.io/docs/#/swapV2/authentication.html
# ─────────────────────────────────────────────────────────────────────────────

def _ts() -> str:
    return str(int(time.time() * 1000))


def _build_signed_qs(params: dict) -> str:
    """
    Construye el query string firmado exactamente como parseParam oficial:

        sorted_params_string + &timestamp=xxx + &signature=HMAC(todo_eso)

    Pasos:
      1. sorted(params.keys()) — sin timestamp
      2. "key=val&key=val" — concatenación simple (NO urlencode)
      3. "&timestamp=xxx"  — siempre al final del payload firmado
      4. HMAC-SHA256 del payload completo
      5. "&signature=xxx"  — appended a la URL
    """
    sorted_keys = sorted(params.keys())
    parts       = ["%s=%s" % (k, params[k]) for k in sorted_keys]
    base        = "&".join(parts)
    ts          = _ts()
    payload     = (base + "&timestamp=" + ts) if base else ("timestamp=" + ts)
    signature   = hmac.new(
        C.BINGX_SECRET_KEY.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    result = payload + "&signature=" + signature
    log.debug("FIRMA payload=%s sig=%s...", payload[:80], signature[:12])
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Cliente HTTP
# ─────────────────────────────────────────────────────────────────────────────

class BingXClient:
    BASE = C.BINGX_BASE_URL

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

        # Precisión de cantidad: symbol → int (decimales)
        self._precision_map: dict[str, int]   = {}
        # Cantidad mínima: symbol → float
        self._min_qty_map:   dict[str, float] = {}
        # stepSize real: symbol → float (0.0 = no cargado todavía)
        self._step_size_map: dict[str, float] = {}

        # Modo de cuenta — detectado una vez en _ensure_position_mode()
        self._position_mode: str = MODE_UNKNOWN

        log.info(
            "BingXClient v6.4.0 iniciado — "
            "firma: sorted+ts_al_final (parseParam oficial)"
        )

    # ── Sesión HTTP ──────────────────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"X-BX-APIKEY": C.BINGX_API_KEY},
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ── HTTP primitives ──────────────────────────────────────────────────────

    async def _get(
        self, path: str, params: dict | None = None, signed: bool = False
    ) -> dict:
        session = await self._get_session()
        base = params or {}
        for attempt in range(3):
            try:
                if signed:
                    qs  = _build_signed_qs(base)
                    url = "%s%s?%s" % (self.BASE, path, qs)
                elif base:
                    url = "%s%s?%s" % (self.BASE, path, urlencode(base))
                else:
                    url = "%s%s" % (self.BASE, path)
                async with session.get(
                    url, headers={"X-BX-APIKEY": C.BINGX_API_KEY}
                ) as r:
                    data = await r.json(content_type=None)
                    if signed and data.get("code") == 100001:
                        log.error(
                            "GET %s firma inválida — url=%s", path, url[:120]
                        )
                    return data
            except Exception as e:
                if attempt == 2:
                    log.error("GET %s error: %s", path, e)
                    raise
                await asyncio.sleep(1.5 ** attempt)
        return {}

    async def _post(self, path: str, params: dict) -> dict:
        session = await self._get_session()
        for attempt in range(3):
            try:
                qs  = _build_signed_qs(params)
                url = "%s%s?%s" % (self.BASE, path, qs)
                async with session.post(
                    url, headers={"X-BX-APIKEY": C.BINGX_API_KEY}
                ) as r:
                    return await r.json(content_type=None)
            except Exception as e:
                if attempt == 2:
                    log.error("POST %s error: %s", path, e)
                    raise
                await asyncio.sleep(1.5 ** attempt)
        return {}

    async def _delete(self, path: str, params: dict) -> dict:
        session = await self._get_session()
        for attempt in range(3):
            try:
                qs  = _build_signed_qs(params)
                url = "%s%s?%s" % (self.BASE, path, qs)
                async with session.delete(
                    url, headers={"X-BX-APIKEY": C.BINGX_API_KEY}
                ) as r:
                    return await r.json(content_type=None)
            except Exception as e:
                if attempt == 2:
                    log.error("DELETE %s error: %s", path, e)
                    raise
                await asyncio.sleep(1.5 ** attempt)
        return {}

    # ── Detección de modo de cuenta (ONE-WAY vs HEDGE) ────────────────────────
    #
    # BingX en ONE-WAY:
    #   - /trade/leverage  NO acepta el campo 'side'       → 109400
    #   - /trade/order     NO acepta el campo 'positionSide' → 109400
    #
    # BingX en HEDGE:
    #   - /trade/leverage  REQUIERE 'side' (LONG|SHORT)
    #   - /trade/order     REQUIERE 'positionSide' (LONG|SHORT)
    #
    # Se detecta UNA VEZ al inicio y se cachea en self._position_mode.
    # ─────────────────────────────────────────────────────────────────────────

    async def _ensure_position_mode(self) -> str:
        """
        Detecta y cachea el modo de cuenta.
        Devuelve MODE_ONE_WAY o MODE_HEDGE.
        """
        if self._position_mode != MODE_UNKNOWN:
            return self._position_mode

        try:
            # BingX expone el modo en algunos endpoints de cuenta
            data = await self._get(
                "/openApi/swap/v2/user/positionMode",
                signed=True,
            )
            code = data.get("code", -1)
            if code == 0:
                # positionMode: 0 = one-way, 1 = hedge (varía por build de API)
                raw = data.get("data", {})
                pm  = int(raw.get("positionMode", 0) if isinstance(raw, dict) else 0)
                self._position_mode = MODE_HEDGE if pm == 1 else MODE_ONE_WAY
                log.info(
                    "positionMode detectado: %d → %s",
                    pm, self._position_mode,
                )
                return self._position_mode
        except Exception as exc:
            log.debug("positionMode endpoint no disponible: %s", exc)

        # Fallback: inferir desde posiciones abiertas
        # Si alguna posición tiene positionSide=BOTH → ONE-WAY
        # Si tiene LONG o SHORT → HEDGE
        try:
            data = await self._get(
                "/openApi/swap/v2/user/positions", signed=True
            )
            positions = data.get("data", [])
            if isinstance(positions, list) and positions:
                side_sample = positions[0].get("positionSide", "BOTH")
                if side_sample in ("LONG", "SHORT"):
                    self._position_mode = MODE_HEDGE
                else:
                    self._position_mode = MODE_ONE_WAY
                log.info(
                    "positionMode inferido desde posiciones: positionSide=%s → %s",
                    side_sample, self._position_mode,
                )
                return self._position_mode
        except Exception as exc:
            log.debug("inferencia de positionMode falló: %s", exc)

        # Si no hay posiciones abiertas ni endpoint disponible → asumir ONE-WAY
        # (es el default de BingX y el más común)
        log.info(
            "positionMode no detectable — asumiendo ONE-WAY (default BingX)"
        )
        self._position_mode = MODE_ONE_WAY
        return self._position_mode

    @property
    def is_hedge_mode(self) -> bool:
        return self._position_mode == MODE_HEDGE

    # ── Redondeo de cantidad ──────────────────────────────────────────────────

    def _round_qty(self, symbol: str, qty: float) -> float:
        """
        Redondea hacia abajo al step permitido por el contrato.
        Usa stepSize si está cargado, si no volumePrecision.
        """
        step = self._step_size_map.get(symbol, 0.0)
        if step > 0:
            # floor al múltiplo de step más cercano
            return math.floor(qty / step) * step

        precision = self._precision_map.get(symbol, 6)
        if precision == 0:
            return float(math.floor(qty))
        factor = 10 ** precision
        return math.floor(qty * factor) / factor

    def _check_min_qty(self, symbol: str, qty: float) -> bool:
        min_q = self._min_qty_map.get(symbol, 0.0)
        return qty >= min_q if min_q > 0 else True

    # ── Símbolos ──────────────────────────────────────────────────────────────

    async def get_all_symbols(self) -> list[str]:
        data = await self._get("/openApi/swap/v2/quote/contracts")
        raw  = data.get("data", [])
        if isinstance(raw, dict):
            raw = raw.get("contracts", raw.get("list", []))
        if not isinstance(raw, list):
            raw = []

        symbols      = []
        vol_map      = {}
        vol_detected = 0

        for item in raw:
            if not isinstance(item, dict):
                continue
            sym = item.get("symbol", "")
            if not sym:
                continue
            if "-" not in sym and sym.endswith("USDT"):
                sym = sym[:-4] + "-USDT"
            if not sym.endswith("-USDT"):
                continue
            if sym in C.BLACKLIST:
                continue
            base_coin = sym.replace("-USDT", "")
            if any(base_coin.startswith(p) for p in ("BEAR", "BULL", "PUMP", "NCS")):
                continue

            precision = int(item.get("volumePrecision", 6) or 6)
            self._precision_map[sym] = precision
            self._min_qty_map[sym]   = float(item.get("tradeMinQuantity", 0) or 0)

            # Cargar stepSize si está disponible (algunos builds lo exponen)
            step_raw = (
                item.get("stepSize") or item.get("quantityStep") or
                item.get("lotSize")  or 0
            )
            if step_raw:
                try:
                    self._step_size_map[sym] = float(step_raw)
                except Exception:
                    pass

            vol_raw = (
                item.get("volume24h") or item.get("vol24h") or
                item.get("quoteVolume") or item.get("turnover24h") or
                item.get("tradeAmt") or item.get("vol") or 0
            )
            vol = float(vol_raw) if vol_raw else 0.0
            if vol > 0:
                vol_detected += 1
            vol_map[sym] = vol
            symbols.append(sym)

        # Enriquecer volumen desde /ticker si contracts no lo tiene
        if vol_detected == 0 and symbols:
            log.info("contracts sin volumen → enriqueciendo con /ticker")
            try:
                td = await self._get("/openApi/swap/v2/quote/ticker")
                for t in (td.get("data", []) or []):
                    s = t.get("symbol", "")
                    if "-" not in s and s.endswith("USDT"):
                        s = s[:-4] + "-USDT"
                    qv = float(
                        t.get("quoteVolume", 0) or t.get("volume", 0) or 0
                    )
                    if s in vol_map:
                        vol_map[s] = qv
                        if qv > 0:
                            vol_detected += 1
            except Exception as e:
                log.warning("ticker fallback error: %s", e)

        if vol_detected > 0 and C.MIN_VOLUME_USDT > 0:
            symbols = [s for s in symbols if vol_map.get(s, 0) >= C.MIN_VOLUME_USDT]

        symbols.sort(key=lambda s: vol_map.get(s, 0), reverse=True)
        if C.TOP_N_SYMBOLS > 0:
            symbols = symbols[: C.TOP_N_SYMBOLS]

        log.info(
            "get_all_symbols: %d símbolos (raw=%d, con_vol=%d)",
            len(symbols), len(raw), vol_detected,
        )
        return symbols

    async def get_klines(
        self, symbol: str, interval: str, limit: int = 200
    ) -> list[list]:
        data = await self._get(
            "/openApi/swap/v3/quote/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
        )
        raw = data.get("data", [])
        if isinstance(raw, dict):
            raw = raw.get("klines", [])
        if not raw:
            return []
        result = []
        for c in raw:
            try:
                if isinstance(c, dict):
                    result.append([
                        int(c.get("time", c.get("openTime", 0))),
                        float(c.get("open",   c.get("o", 0))),
                        float(c.get("high",   c.get("h", 0))),
                        float(c.get("low",    c.get("l", 0))),
                        float(c.get("close",  c.get("c", 0))),
                        float(c.get("volume", c.get("v", 0))),
                    ])
                elif isinstance(c, (list, tuple)) and len(c) >= 6:
                    result.append([
                        int(c[0]), float(c[1]), float(c[2]),
                        float(c[3]), float(c[4]), float(c[5]),
                    ])
            except Exception:
                continue
        return sorted(result, key=lambda x: x[0])

    async def get_ticker(self, symbol: str) -> dict:
        data = await self._get(
            "/openApi/swap/v2/quote/ticker", {"symbol": symbol}
        )
        return data.get("data", {})

    # ── Cuenta ────────────────────────────────────────────────────────────────

    async def get_balance(self) -> float:
        """
        Retorna availableMargin USDT.
        Si availableMargin=0 pero hay equity (posiciones abiertas),
        usa equity como proxy del capital real disponible.
        """
        data = await self._get(
            "/openApi/swap/v3/user/balance",
            {"currency": "USDT"},
            signed=True,
        )
        if data.get("code", -1) != 0:
            log.warning(
                "get_balance v3 falló (code=%s) → probando v2", data.get("code")
            )
            data = await self._get(
                "/openApi/swap/v2/user/balance",
                {"currency": "USDT"},
                signed=True,
            )
        raw = data.get("data", {})
        log.info(
            "get_balance: code=%s data=%s", data.get("code"), str(raw)[:300]
        )

        def _extract(d: dict) -> float:
            avail  = float(d.get("availableMargin", 0) or 0)
            equity = float(d.get("equity",          0) or 0)
            if avail > 0:
                return avail
            if equity > 0:
                log.debug("availableMargin=0, usando equity=%.4f", equity)
                return equity
            return 0.0

        if isinstance(raw, list):
            for a in raw:
                if isinstance(a, dict) and a.get("asset", "") == "USDT":
                    return _extract(a)
            for a in raw:
                if isinstance(a, dict) and (
                    "availableMargin" in a or "equity" in a
                ):
                    return _extract(a)
            return 0.0

        if isinstance(raw, dict):
            bal = raw.get("balance", raw)
            if isinstance(bal, list):
                for a in bal:
                    if isinstance(a, dict) and a.get("asset", "") == "USDT":
                        return _extract(a)
            if isinstance(bal, dict):
                return _extract(bal)

        log.warning("get_balance: formato inesperado %s", str(data)[:200])
        return 0.0

    # ── Posiciones ────────────────────────────────────────────────────────────

    async def get_open_positions(self) -> list[dict]:
        data = await self._get(
            "/openApi/swap/v2/user/positions", None, signed=True
        )
        positions = data.get("data", [])
        if not isinstance(positions, list):
            return []
        return [p for p in positions if float(p.get("positionAmt", 0)) != 0]

    async def get_open_orders(self, symbol: str) -> list[dict]:
        data = await self._get(
            "/openApi/swap/v2/trade/openOrders",
            {"symbol": symbol},
            signed=True,
        )
        return data.get("data", {}).get("orders", [])

    # ── Apalancamiento ────────────────────────────────────────────────────────

    async def set_leverage(
        self, symbol: str, leverage: int, side: str = "LONG"
    ) -> bool:
        """
        Establece leverage.

        ONE-WAY mode: una sola llamada SIN el campo 'side'.
        HEDGE mode:   dos llamadas (LONG + SHORT) CON el campo 'side'.

        leverage se envía siempre como str para máxima compatibilidad.
        """
        await self._ensure_position_mode()

        leverage_str = str(leverage)

        if self.is_hedge_mode:
            # HEDGE: necesita side=LONG y side=SHORT por separado
            ok = True
            for s in ("LONG", "SHORT"):
                data = await self._post(
                    "/openApi/swap/v2/trade/leverage",
                    {
                        "symbol":   symbol,
                        "side":     s,
                        "leverage": leverage_str,
                    },
                )
                code = data.get("code", -1)
                if code != 0:
                    log.warning(
                        "[%s] set_leverage HEDGE side=%s → code=%s msg=%s",
                        symbol, s, code, data.get("msg", ""),
                    )
                    ok = False
            return ok
        else:
            # ONE-WAY: sin campo 'side' — enviarlo causa 109400
            data = await self._post(
                "/openApi/swap/v2/trade/leverage",
                {
                    "symbol":   symbol,
                    "leverage": leverage_str,
                },
            )
            code = data.get("code", -1)
            if code != 0:
                log.warning(
                    "[%s] set_leverage ONE-WAY → code=%s msg=%s",
                    symbol, code, data.get("msg", ""),
                )
                return False
            return True

    # ── Órdenes ───────────────────────────────────────────────────────────────

    def _order_params_base(
        self, symbol: str, side: str, position_side: str
    ) -> dict:
        """
        Construye los campos base de una orden.

        ONE-WAY: NO incluye 'positionSide' (causa 109400 si se incluye).
        HEDGE:   SÍ incluye 'positionSide'.
        """
        params: dict = {"symbol": symbol, "side": side}
        if self.is_hedge_mode:
            params["positionSide"] = position_side
        return params

    async def place_market_order(
        self,
        symbol:        str,
        side:          str,
        quantity:      float,
        position_side: str = "LONG",
    ) -> dict:
        await self._ensure_position_mode()

        qty = self._round_qty(symbol, quantity)
        if not self._check_min_qty(symbol, qty):
            log.warning("[%s] qty %.6f < min_qty — skip", symbol, qty)
            return {"code": -1, "msg": "qty_below_minimum"}

        params = self._order_params_base(symbol, side, position_side)
        params.update({
            "type":     "MARKET",
            "quantity": str(qty),
        })
        log.info(
            "[%s] MARKET order mode=%s params=%s",
            symbol, self._position_mode, params,
        )
        resp = await self._post("/openApi/swap/v2/trade/order", params)
        if resp.get("code", -1) != 0:
            log.error(
                "[%s] MARKET order falló: code=%s msg=%s",
                symbol, resp.get("code"), resp.get("msg", ""),
            )
        return resp

    async def place_stop_market_order(
        self,
        symbol:         str,
        side:           str,
        quantity:       float,
        stop_price:     float,
        position_side:  str  = "LONG",
        close_position: bool = True,
        order_type:     str  = "STOP_MARKET",
    ) -> dict:
        await self._ensure_position_mode()

        qty = self._round_qty(symbol, quantity)
        if stop_price <= 0:
            log.warning(
                "[%s] place_stop: stopPrice inválido (%.8f) — skip",
                symbol, stop_price,
            )
            return {"code": -1, "msg": "invalid_stop_price"}

        qty_str = str(qty) if qty > 0 else "0"
        params  = self._order_params_base(symbol, side, position_side)
        params.update({
            "type":          order_type,
            "stopPrice":     str(round(stop_price, 8)),
            "closePosition": "true" if close_position else "false",
            "quantity":      qty_str,
            "workingType":   "MARK_PRICE",
            "priceProtect":  "true",
        })
        resp = await self._post("/openApi/swap/v2/trade/order", params)
        if resp.get("code", -1) != 0:
            log.warning(
                "[%s] %s order falló: code=%s msg=%s",
                symbol, order_type, resp.get("code"), resp.get("msg", ""),
            )
        return resp

    async def cancel_order(self, symbol: str, order_id: str) -> dict:
        return await self._delete(
            "/openApi/swap/v2/trade/order",
            {"symbol": symbol, "orderId": order_id},
        )

    async def cancel_all_orders(self, symbol: str) -> dict:
        return await self._delete(
            "/openApi/swap/v2/trade/allOpenOrders",
            {"symbol": symbol},
        )

    async def get_order_book(self, symbol: str, limit: int = 5) -> dict:
        data = await self._get(
            "/openApi/swap/v2/quote/depth",
            {"symbol": symbol, "limit": limit},
        )
        return data.get("data", {})

    async def get_funding_rate(self, symbol: str) -> float:
        data = await self._get(
            "/openApi/swap/v2/quote/premiumIndex",
            {"symbol": symbol},
        )
        d = data.get("data", {})
        if isinstance(d, list) and d:
            d = d[0]
        try:
            return float(d.get("lastFundingRate", 0) or 0)
        except Exception:
            return 0.0

    async def close_position_market(
        self, symbol: str, quantity: float, position_side: str
    ) -> dict:
        await self._ensure_position_mode()
        side = "SELL" if position_side == "LONG" else "BUY"
        qty  = self._round_qty(symbol, quantity)
        params = self._order_params_base(symbol, side, position_side)
        params.update({
            "type":     "MARKET",
            "quantity": str(qty),
        })
        return await self._post("/openApi/swap/v2/trade/order", params)

    # ── open_trade completo ───────────────────────────────────────────────────

    async def open_trade(
        self,
        symbol:    str,
        direction: str,
        quantity:  float,
        sl_price:  float,
        tp1_price: float,
        tp2_price: float,
    ) -> dict:
        """
        Flujo completo de apertura:
          1. Detectar modo cuenta (si no hecho todavía)
          2. set_leverage
          3. Entrada MARKET
          4. SL + TP1 + TP2 como stop orders
        """
        await self._ensure_position_mode()

        side_entry = "BUY"  if direction == "LONG" else "SELL"
        side_close = "SELL" if direction == "LONG" else "BUY"
        results: dict = {}

        # 1. Leverage (respeta modo)
        await self.set_leverage(symbol, C.LEVERAGE, direction)

        # 2. Verificar qty
        qty = self._round_qty(symbol, quantity)
        if not self._check_min_qty(symbol, qty):
            log.warning("[%s] qty %.6f < min → skip", symbol, qty)
            return {"entry": {"code": -1, "msg": "qty_below_minimum"}}

        # 3. Entrada
        entry_resp = await self.place_market_order(
            symbol, side_entry, qty, direction
        )
        results["entry"] = entry_resp
        if entry_resp.get("code", -1) != 0:
            log.error("[%s] Entrada fallida: %s", symbol, entry_resp)
            return results

        await asyncio.sleep(0.5)

        # 4. SL
        results["sl"] = await self.place_stop_market_order(
            symbol, side_close, qty, sl_price, direction,
            close_position=True, order_type="STOP_MARKET",
        )

        # 5. TP1 + TP2 (mitad cada uno)
        qty_half = self._round_qty(symbol, qty / 2)
        results["tp1"] = await self.place_stop_market_order(
            symbol, side_close, qty_half, tp1_price, direction,
            close_position=False, order_type="TAKE_PROFIT_MARKET",
        )
        results["tp2"] = await self.place_stop_market_order(
            symbol, side_close, qty_half, tp2_price, direction,
            close_position=False, order_type="TAKE_PROFIT_MARKET",
        )

        log.info(
            "[%s] open_trade completo mode=%s entry=%s sl=%s tp1=%s tp2=%s",
            symbol, self._position_mode,
            results["entry"].get("code"),
            results["sl"].get("code"),
            results["tp1"].get("code"),
            results["tp2"].get("code"),
        )
        return results
