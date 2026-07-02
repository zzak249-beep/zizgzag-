"""
Persistent bot state — survives Railway restarts.
Stores: entry_time, trail_stop, entry_reason, TP target, last_close
per symbol/side.

FIX: reemplaza los dicts en RAM que tenía daring-spontaneity
(_trail en position_manager.py; _entry_reason, _fib_tp, _uni_tp,
_last_close en main.py). Sin esto, cualquier redeploy con una
posición abierta pierde de qué estrategia venía, su TP objetivo, y
resetea el trailing stop desde cero — mismo patrón ya corregido en
renewed-love y joyful-art, aquí sin aplicar hasta ahora.
"""
import json
import logging
import os
import time

log = logging.getLogger("state")

_STATE_FILE = os.getenv("STATE_FILE", "/app/bot_state.json")


# ── Internal I/O ──────────────────────────────────────────────

def _load() -> dict:
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict):
    try:
        os.makedirs(os.path.dirname(_STATE_FILE) or ".", exist_ok=True)
        with open(_STATE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log.error(f"state write error: {e}")


def _key(symbol: str, side: str, field: str) -> str:
    return f"{symbol}_{side}_{field}"


# ── Entry time ────────────────────────────────────────────────

def save_entry(symbol: str, side: str, ts: float = None):
    d = _load()
    d[_key(symbol, side, "entry_ts")] = ts or time.time()
    _save(d)
    log.debug(f"state.save_entry {symbol} {side}")


def get_entry_ts(symbol: str, side: str) -> float | None:
    v = _load().get(_key(symbol, side, "entry_ts"))
    return float(v) if v is not None else None


# ── Trail stop ────────────────────────────────────────────────

def save_trail(symbol: str, side: str, stop: float):
    d = _load()
    d[_key(symbol, side, "trail")] = stop
    _save(d)


def get_trail(symbol: str, side: str) -> float | None:
    v = _load().get(_key(symbol, side, "trail"))
    return float(v) if v is not None else None


# ── Entry reason (qué estrategia abrió la posición) ────────────

def save_reason(symbol: str, side: str, reason: str):
    d = _load()
    d[_key(symbol, side, "reason")] = reason
    _save(d)


def get_reason(symbol: str, side: str) -> str:
    return _load().get(_key(symbol, side, "reason"), "")


# ── TP objetivo (fib / unicorn) ─────────────────────────────────

def save_tp(symbol: str, side: str, tp_type: str, price: float):
    """tp_type: 'fib' o 'unicorn'."""
    d = _load()
    d[_key(symbol, side, f"tp_{tp_type}")] = price
    _save(d)


def get_tp(symbol: str, side: str, tp_type: str) -> float | None:
    v = _load().get(_key(symbol, side, f"tp_{tp_type}"))
    return float(v) if v is not None else None


# ── Cooldown (último cierre) ────────────────────────────────────

def save_last_close(symbol: str, side: str, ts: float = None):
    d = _load()
    d[_key(symbol, side, "last_close_ts")] = ts or time.time()
    _save(d)


def get_last_close(symbol: str, side: str) -> float | None:
    v = _load().get(_key(symbol, side, "last_close_ts"))
    return float(v) if v is not None else None


# ── Clear all state for a position ───────────────────────────

def clear(symbol: str, side: str):
    """
    Borra entry_ts, trail, reason y TPs — NO toca last_close_ts,
    que debe sobrevivir al cierre para que el cooldown funcione.
    """
    d = _load()
    prefix = f"{symbol}_{side}_"
    keys_to_del = [
        k for k in d
        if k.startswith(prefix) and not k.endswith("_last_close_ts")
    ]
    for k in keys_to_del:
        del d[k]
    _save(d)
    log.debug(f"state.clear {symbol} {side} ({len(keys_to_del)} keys removed)")


# ── Debug dump ────────────────────────────────────────────────

def dump() -> dict:
    return _load()


# ── Daily PnL state (bot-wide, no por symbol/side) ──────────────

def save_day_state(day_pnl: float, day_start_eq: float, day: str):
    d = _load()
    d["_day_pnl"]      = day_pnl
    d["_day_start_eq"] = day_start_eq
    d["_day"]          = day
    _save(d)


def get_day_state() -> tuple:
    """Returns (day_pnl, day_start_eq, day_iso) — cualquiera puede ser None."""
    d = _load()
    return d.get("_day_pnl"), d.get("_day_start_eq"), d.get("_day")
