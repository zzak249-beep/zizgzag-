"""
QF×JP Bot v6.4 — Entry Point
"""
import asyncio
import logging
import os

from aiohttp import web

import config as C
from bingx_client import BingXClient
from risk_manager import RiskManager
from position_manager import PositionManager
from scanner import scan_loop
import telegram_client as tg

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("main")

# ── HTTP health / control ─────────────────────────────────────────────────────

async def handle_health(request):
    return web.json_response({"status": "ok", "mode": C.MODE})


async def handle_close(request):
    """POST /close?symbol=BTC-USDT  — cierre de emergencia."""
    symbol = request.rel_url.query.get("symbol", "").strip()
    if not symbol:
        return web.json_response({"error": "symbol required"}, status=400)
    pos_mgr: PositionManager = request.app["pos_mgr"]
    await pos_mgr.close_position_emergency(symbol, reason="manual_close")
    return web.json_response({"status": "closing", "symbol": symbol})


async def handle_status(request):
    risk:    RiskManager    = request.app["risk"]
    pos_mgr: PositionManager = request.app["pos_mgr"]
    client:  BingXClient    = request.app["client"]
    try:
        balance = await client.get_balance()
    except Exception:
        balance = -1.0
    return web.json_response({
        "risk":     risk.status(),
        "balance":  balance,
        "trades":   {s: vars(t) for s, t in pos_mgr.get_tracked().items()},
    })

# ── App lifecycle ─────────────────────────────────────────────────────────────

async def on_startup(app):
    client:  BingXClient    = app["client"]
    risk:    RiskManager    = app["risk"]
    pos_mgr: PositionManager = app["pos_mgr"]

    log.info("QF×JP Bot v6.4 arrancando — modo=%s", C.MODE)
    await tg.send(f"🚀 QF×JP Bot v6.4 iniciado — modo *{C.MODE}*")

    # Reconciliar posiciones abiertas tras redeploy
    await pos_mgr.reconcile_on_startup()

    # Lanzar loops en background
    asyncio.create_task(pos_mgr.monitor_loop(),  name="position_monitor")
    asyncio.create_task(scan_loop(client, risk, pos_mgr), name="scanner")
    log.info("Tasks lanzadas: position_monitor + scanner")


async def on_cleanup(app):
    await app["client"].close()
    log.info("BingXClient cerrado.")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    client  = BingXClient()
    risk    = RiskManager()
    pos_mgr = PositionManager(client, risk)

    app = web.Application()
    app["client"]  = client
    app["risk"]    = risk
    app["pos_mgr"] = pos_mgr

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    app.router.add_get("/",       handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/status", handle_status)
    app.router.add_post("/close", handle_close)

    port = int(os.getenv("PORT", C.PORT))
    log.info("HTTP server en puerto %d", port)
    web.run_app(app, host="0.0.0.0", port=port, access_log=None)


if __name__ == "__main__":
    main()
