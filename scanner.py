"""
scanner.py — Scanner de todos los símbolos de BingX Perpetuals
==============================================================
Obtiene TODOS los contratos perpetuos de BingX, filtra por volumen
y devuelve lista ordenada por liquidez.
"""
from __future__ import annotations
import logging, time
from typing import Optional
import requests

logger = logging.getLogger(__name__)
BINGX_BASE = "https://open-api.bingx.com"


class BingXScanner:
    """
    Escanea todos los perpetuos de BingX y filtra los tradeable.

    Uso:
        scanner = BingXScanner(min_volume_usdt=500_000)
        symbols = scanner.get_tradeable_symbols()
        # → ['BTC-USDT', 'ETH-USDT', 'SOL-USDT', ...]
    """

    def __init__(
        self,
        min_volume_usdt: float = 500_000,   # mínimo volumen 24h en USDT
        min_price:       float = 0.00001,   # excluir dust tokens
        exclude:         list  = None,      # símbolos a excluir
        timeout:         int   = 15,
    ):
        self.min_volume  = min_volume_usdt
        self.min_price   = min_price
        self.exclude     = set(exclude or ["USDC-USDT", "USDT-USDT", "BUSD-USDT"])
        self.timeout     = timeout
        self._cache:     list  = []
        self._cache_ts:  float = 0
        self._cache_ttl: int   = 300  # 5 min

    def _get(self, path: str, params: dict = None) -> Optional[dict]:
        try:
            r = requests.get(
                f"{BINGX_BASE}{path}",
                params=params or {},
                timeout=self.timeout,
            )
            r.raise_for_status()
            j = r.json()
            if j.get("code") == 0:
                return j.get("data")
        except Exception as e:
            logger.error(f"[Scanner] {path}: {e}")
        return None

    def get_all_contracts(self) -> list[dict]:
        """Obtiene todos los contratos perpetuos de BingX."""
        data = self._get("/openApi/swap/v2/quote/contracts")
        if isinstance(data, list):
            return data
        return []

    def get_24h_tickers(self) -> dict[str, dict]:
        """Obtiene volumen y precio 24h de todos los símbolos."""
        data = self._get("/openApi/swap/v2/quote/ticker")
        tickers = {}
        if isinstance(data, list):
            for t in data:
                sym = t.get("symbol", "")
                if sym:
                    tickers[sym] = t
        return tickers

    def get_tradeable_symbols(self, force_refresh: bool = False) -> list[str]:
        """
        Retorna lista de símbolos tradeable ordenados por volumen 24h.
        Filtra por: volumen mínimo, precio mínimo, excluidos.
        """
        now = time.time()
        if not force_refresh and self._cache and (now - self._cache_ts) < self._cache_ttl:
            return self._cache

        logger.info("[Scanner] Obteniendo todos los símbolos de BingX...")

        contracts = self.get_all_contracts()
        tickers   = self.get_24h_tickers()

        if not contracts:
            logger.warning("[Scanner] No se pudieron obtener contratos")
            return self._cache or []

        results = []
        for c in contracts:
            sym    = c.get("symbol", "")
            status = c.get("status", 0)

            # Filtros básicos
            if not sym or sym in self.exclude:
                continue
            if not sym.endswith("-USDT"):
                continue
            if status not in (1, "1", True):   # solo activos
                continue

            # Datos de ticker
            t = tickers.get(sym, {})
            vol_24h = float(t.get("quoteVolume", t.get("volume", 0)) or 0)
            price   = float(t.get("lastPrice", t.get("last", 0)) or 0)

            if price < self.min_price:
                continue
            if vol_24h < self.min_volume:
                continue

            results.append({
                "symbol":    sym,
                "price":     price,
                "volume_24h": vol_24h,
                "change_24h": float(t.get("priceChangePercent", 0) or 0),
            })

        # Ordenar por volumen descendente
        results.sort(key=lambda x: x["volume_24h"], reverse=True)

        symbols = [r["symbol"] for r in results]
        logger.info(f"[Scanner] {len(symbols)} símbolos tradeable encontrados")

        self._cache    = symbols
        self._cache_ts = now
        return symbols

    def get_top_symbols(self, n: int = 30) -> list[str]:
        """Top N símbolos por volumen."""
        return self.get_tradeable_symbols()[:n]

    def format_telegram(self, symbols: list[str], tickers: dict = None) -> str:
        lines = [f"🔍 *BingX Scanner* — {len(symbols)} símbolos activos\n"]
        for i, sym in enumerate(symbols[:20], 1):
            t = (tickers or {}).get(sym, {})
            vol = float(t.get("quoteVolume", 0) or 0)
            chg = float(t.get("priceChangePercent", 0) or 0)
            icon = "🟢" if chg > 0 else "🔴"
            lines.append(f"{i:2d}. `{sym:15}` {icon} `{chg:+.1f}%` Vol:`{vol/1e6:.1f}M`")
        if len(symbols) > 20:
            lines.append(f"_... y {len(symbols)-20} más_")
        return "\n".join(lines)
