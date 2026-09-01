"""
Suite de tests del bot. No toca la red real ni Telegram real: todo lo
externo (BingX, Telegram) se mockea. Corre con:

    cd wavelet_bot
    STATE_FILE=/tmp/test_state.json WEBHOOK_SECRET=test AUTO_TRADE=false \
        python -m pytest tests/ -v
"""
import importlib
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_STATE_FILE = "/tmp/wavelet_bot_test_state.json"


@pytest.fixture(autouse=True)
def clean_state_file():
    if os.path.exists(TEST_STATE_FILE):
        os.remove(TEST_STATE_FILE)
    yield
    if os.path.exists(TEST_STATE_FILE):
        os.remove(TEST_STATE_FILE)


@pytest.fixture
def app_module():
    """Importa (o recarga) main.py con config limpia y estado limpio para cada test."""
    os.environ["WEBHOOK_SECRET"] = "test-secret"
    os.environ["STATE_FILE"] = TEST_STATE_FILE
    os.environ["AUTO_TRADE"] = "false"
    os.environ["BINGX_API_KEY"] = ""
    os.environ["BINGX_API_SECRET"] = ""
    os.environ["TELEGRAM_BOT_TOKEN"] = ""
    os.environ["TELEGRAM_CHAT_ID"] = ""

    import config
    importlib.reload(config)
    import state_manager
    importlib.reload(state_manager)
    import bingx_client
    importlib.reload(bingx_client)
    import telegram_notifier
    importlib.reload(telegram_notifier)
    import main
    importlib.reload(main)
    return main


def entry_alert(symbol="BTCUSDT.P", side="LONG", price=65000, sl=64000, tp=67000):
    positionSide = side
    order_side = "buy" if side == "LONG" else "sell"
    return {
        "strategy": "wavelet_mra_5m", "exchange": "BingX", "symbol": symbol,
        "side": order_side, "positionSide": positionSide, "signal": "entry",
        "price": price, "sl": sl, "tp": tp, "time": 1234567890,
    }


def exit_alert(symbol="BTCUSDT.P", side="LONG", price=66000):
    order_side = "sell" if side == "LONG" else "buy"
    return {
        "strategy": "wavelet_mra_5m", "exchange": "BingX", "symbol": symbol,
        "side": order_side, "positionSide": side, "signal": "exit",
        "price": price, "time": 1234567999,
    }


# --------------------------------------------------------------------------- #
# 1. Endpoints básicos / seguridad
# --------------------------------------------------------------------------- #
class TestBasicEndpoints:
    def test_health(self, app_module):
        client = app_module.app.test_client()
        r = client.get("/")
        assert r.status_code == 200
        assert r.get_json()["status"] == "ok"

    def test_webhook_wrong_secret_rejected(self, app_module):
        client = app_module.app.test_client()
        r = client.post("/webhook/wrong-secret", json=entry_alert())
        assert r.status_code == 401

    def test_webhook_unknown_signal_type(self, app_module):
        client = app_module.app.test_client()
        alert = entry_alert()
        alert["signal"] = "something_else"
        r = client.post("/webhook/test-secret", json=alert)
        assert r.status_code == 400

    def test_webhook_malformed_json_handled(self, app_module):
        client = app_module.app.test_client()
        r = client.post(
            "/webhook/test-secret",
            data="not json at all",
            content_type="text/plain",
        )
        assert r.status_code == 400


# --------------------------------------------------------------------------- #
# 2. Mapeo de símbolos
# --------------------------------------------------------------------------- #
class TestSymbolMapping:
    @pytest.mark.parametrize("tv,expected", [
        ("BTCUSDT.P", "BTC-USDT"),
        ("BTCUSDT", "BTC-USDT"),
        ("ETHUSDT.P", "ETH-USDT"),
        ("BTC-USDT", "BTC-USDT"),
        ("SOLUSDC", "SOL-USDC"),
    ])
    def test_mapping(self, app_module, tv, expected):
        assert app_module.config.tv_symbol_to_bingx(tv) == expected


# --------------------------------------------------------------------------- #
# 3. Firma HMAC de BingXClient — determinismo firma == transmisión
# --------------------------------------------------------------------------- #
class TestBingXSigning:
    def test_signing_uses_same_order_for_sign_and_send(self, app_module):
        """Reproduce el bug histórico: firmar en un orden y enviar en otro.
        Aquí forzamos params en desorden y comprobamos que el query string
        firmado es EXACTAMENTE el mismo que se transmite (ambos ordenados)."""
        client = app_module.bingx_client.BingXClient(
            api_key="fake", api_secret="fake_secret", base_url="https://fake"
        )

        captured = {}

        def fake_request(method, url, headers=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"code": 0, "data": {"ok": True}}
            return mock_resp

        with patch("bingx_client.requests.request", side_effect=fake_request):
            # params deliberadamente en desorden alfabético
            client._signed_request("POST", "/openApi/swap/v2/trade/order", {
                "symbol": "BTC-USDT",
                "side": "BUY",
                "quantity": 0.01,
                "positionSide": "LONG",
                "type": "MARKET",
            })

        url = captured["url"]
        query_part, sig_part = url.split("&signature=")
        query_string = query_part.split("?", 1)[1]

        import hmac, hashlib
        expected_sig = hmac.new(
            b"fake_secret", query_string.encode(), hashlib.sha256
        ).hexdigest()

        assert sig_part == expected_sig, (
            "La firma no coincide con el query string transmitido: "
            "el orden de los parámetros difiere entre firma y envío."
        )
        # además, comprobamos que va ordenado alfabéticamente (determinista)
        keys_in_url = [kv.split("=")[0] for kv in query_string.split("&")]
        assert keys_in_url == sorted(keys_in_url)

    def test_api_error_raises(self, app_module):
        client = app_module.bingx_client.BingXClient(
            api_key="fake", api_secret="fake_secret", base_url="https://fake"
        )
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"code": 80001, "msg": "insufficient balance"}
        with patch("bingx_client.requests.request", return_value=mock_resp):
            with pytest.raises(app_module.bingx_client.BingXError):
                client.get_balance()


# --------------------------------------------------------------------------- #
# 4. Webhook en modo MANUAL (AUTO_TRADE=false): solo debe avisar, no ejecutar
# --------------------------------------------------------------------------- #
class TestManualMode:
    def test_entry_sends_telegram_no_bingx_call(self, app_module):
        client = app_module.app.test_client()
        with patch.object(app_module.telegram_notifier, "send") as mock_send, \
             patch.object(app_module.bx, "place_market_order") as mock_order:
            r = client.post("/webhook/test-secret", json=entry_alert())
            assert r.status_code == 200
            mock_order.assert_not_called()
            mock_send.assert_called_once()
            sent_text = mock_send.call_args[0][0]
            assert "LONG" in sent_text
            assert "manual" in sent_text.lower()

    def test_exit_sends_telegram_no_bingx_call(self, app_module):
        client = app_module.app.test_client()
        with patch.object(app_module.telegram_notifier, "send") as mock_send, \
             patch.object(app_module.bx, "close_position") as mock_close:
            r = client.post("/webhook/test-secret", json=exit_alert())
            assert r.status_code == 200
            mock_close.assert_not_called()
            mock_send.assert_called_once()


# --------------------------------------------------------------------------- #
# 5. Webhook en modo AUTOMÁTICO (AUTO_TRADE=true)
# --------------------------------------------------------------------------- #
class TestAutoMode:
    def _enable_auto(self, app_module):
        app_module.config.AUTO_TRADE = True

    def test_entry_executes_order_and_records_state(self, app_module):
        self._enable_auto(app_module)
        client = app_module.app.test_client()
        with patch.object(app_module.bx, "get_balance", return_value=1000.0), \
             patch.object(app_module.bx, "set_leverage") as mock_lev, \
             patch.object(app_module.bx, "place_market_order") as mock_order, \
             patch.object(app_module.telegram_notifier, "send") as mock_send:
            r = client.post("/webhook/test-secret", json=entry_alert())
            assert r.status_code == 200
            mock_lev.assert_called_once()
            mock_order.assert_called_once()
            # qty = risk_amount / stop_distance = (1000*2%) / (65000-64000) = 20/1000 = 0.02
            called_kwargs = mock_order.call_args
            args, kwargs = called_kwargs
            assert args[0] == "BTC-USDT"
            assert kwargs["stop_loss"] == 64000
            assert kwargs["take_profit"] == 67000
            sent_text = mock_send.call_args[0][0]
            assert "Ejecutado" in sent_text

        pos = app_module.state.get_open("BTC-USDT")
        assert pos is not None
        assert pos["positionSide"] == "LONG"
        assert pos["qty"] == pytest.approx(0.02, rel=1e-3)

    def test_duplicate_entry_rejected(self, app_module):
        self._enable_auto(app_module)
        client = app_module.app.test_client()
        with patch.object(app_module.bx, "get_balance", return_value=1000.0), \
             patch.object(app_module.bx, "set_leverage"), \
             patch.object(app_module.bx, "place_market_order") as mock_order, \
             patch.object(app_module.telegram_notifier, "send") as mock_send:
            client.post("/webhook/test-secret", json=entry_alert())
            mock_order.reset_mock()
            mock_send.reset_mock()
            r2 = client.post("/webhook/test-secret", json=entry_alert())
            assert r2.status_code == 200
            mock_order.assert_not_called()
            assert "ya hay posición" in mock_send.call_args[0][0]

    def test_max_concurrent_positions(self, app_module):
        self._enable_auto(app_module)
        app_module.config.MAX_CONCURRENT_POSITIONS = 1
        client = app_module.app.test_client()
        with patch.object(app_module.bx, "get_balance", return_value=1000.0), \
             patch.object(app_module.bx, "set_leverage"), \
             patch.object(app_module.bx, "place_market_order") as mock_order, \
             patch.object(app_module.telegram_notifier, "send") as mock_send:
            client.post("/webhook/test-secret", json=entry_alert(symbol="BTCUSDT.P"))
            mock_order.reset_mock()
            mock_send.reset_mock()
            r2 = client.post("/webhook/test-secret", json=entry_alert(symbol="ETHUSDT.P", price=3000, sl=2950, tp=3100))
            assert r2.status_code == 200
            mock_order.assert_not_called()
            assert "límite de posiciones" in mock_send.call_args[0][0]

    def test_exit_closes_position_and_updates_state(self, app_module):
        self._enable_auto(app_module)
        client = app_module.app.test_client()
        with patch.object(app_module.bx, "get_balance", return_value=1000.0), \
             patch.object(app_module.bx, "set_leverage"), \
             patch.object(app_module.bx, "place_market_order"), \
             patch.object(app_module.telegram_notifier, "send"):
            client.post("/webhook/test-secret", json=entry_alert())

        assert app_module.state.get_open("BTC-USDT") is not None

        with patch.object(app_module.bx, "close_position") as mock_close, \
             patch.object(app_module.telegram_notifier, "send") as mock_send:
            r = client.post("/webhook/test-secret", json=exit_alert(price=66000))
            assert r.status_code == 200
            mock_close.assert_called_once()
            assert "cerrada" in mock_send.call_args[0][0].lower()

        assert app_module.state.get_open("BTC-USDT") is None

    def test_exit_without_local_position_warns_but_does_not_crash(self, app_module):
        self._enable_auto(app_module)
        client = app_module.app.test_client()
        with patch.object(app_module.bx, "close_position") as mock_close, \
             patch.object(app_module.telegram_notifier, "send") as mock_send:
            r = client.post("/webhook/test-secret", json=exit_alert(symbol="ETHUSDT.P"))
            assert r.status_code == 200
            mock_close.assert_not_called()
            assert "no había posición" in mock_send.call_args[0][0]

    def test_bingx_failure_on_entry_is_reported_not_swallowed_silently(self, app_module):
        self._enable_auto(app_module)
        client = app_module.app.test_client()
        with patch.object(app_module.bx, "get_balance", return_value=1000.0), \
             patch.object(app_module.bx, "set_leverage"), \
             patch.object(app_module.bx, "place_market_order", side_effect=app_module.bingx_client.BingXError("boom")), \
             patch.object(app_module.telegram_notifier, "send") as mock_send:
            r = client.post("/webhook/test-secret", json=entry_alert())
            assert r.status_code == 200  # el webhook no debe caerse
            assert "boom" in mock_send.call_args[0][0]
        assert app_module.state.get_open("BTC-USDT") is None  # no se registra si falló


# --------------------------------------------------------------------------- #
# 6. Circuit breaker
# --------------------------------------------------------------------------- #
class TestCircuitBreaker:
    def test_blocks_after_max_consecutive_losses(self, app_module):
        app_module.config.MAX_CONSECUTIVE_LOSSES = 2
        allowed, _ = app_module.state.check_circuit_breaker(1000.0)
        assert allowed
        app_module.state.state["consecutive_losses"] = 2
        allowed, reason = app_module.state.check_circuit_breaker(1000.0)
        assert not allowed
        assert "pérdidas consecutivas" in reason

    def test_blocks_after_daily_drawdown(self, app_module):
        app_module.config.MAX_DAILY_DRAWDOWN_PCT = 5.0
        allowed, _ = app_module.state.check_circuit_breaker(1000.0)  # ancla el día a 1000
        assert allowed
        allowed2, reason = app_module.state.check_circuit_breaker(940.0)  # -6%
        assert not allowed2
        assert "drawdown" in reason

    def test_manual_reset_clears_halt(self, app_module):
        app_module.config.MAX_CONSECUTIVE_LOSSES = 1
        app_module.state.state["consecutive_losses"] = 1
        allowed, _ = app_module.state.check_circuit_breaker(1000.0)
        assert not allowed
        app_module.state.manual_reset_breaker()
        allowed2, _ = app_module.state.check_circuit_breaker(1000.0)
        assert allowed2

    def test_entry_blocked_by_circuit_breaker_end_to_end(self, app_module):
        import datetime
        app_module.config.AUTO_TRADE = True
        # Ancla el día a HOY para que refresh_daily_anchor() no lo confunda
        # con un día nuevo y resetee el halt (eso solo debe pasar al cambiar
        # de fecha real, no en mitad de la sesión de hoy).
        app_module.state.state["daily_date"] = datetime.date.today().isoformat()
        app_module.state.state["daily_start_equity"] = 1000.0
        app_module.state.state["trading_halted"] = True
        app_module.state.state["halt_reason"] = "test halt"
        client = app_module.app.test_client()
        with patch.object(app_module.bx, "get_balance", return_value=1000.0), \
             patch.object(app_module.bx, "set_leverage") as mock_lev, \
             patch.object(app_module.bx, "place_market_order") as mock_order, \
             patch.object(app_module.telegram_notifier, "send") as mock_send:
            r = client.post("/webhook/test-secret", json=entry_alert())
            assert r.status_code == 200
            mock_lev.assert_not_called()
            mock_order.assert_not_called()
            assert "circuit breaker" in mock_send.call_args[0][0].lower()


# --------------------------------------------------------------------------- #
# 7. Persistencia / reconciliación
# --------------------------------------------------------------------------- #
class TestStateAndReconciliation:
    def test_state_persists_to_disk(self, app_module):
        app_module.state.record_open("BTC-USDT", "LONG", 0.01, 65000, 64000, 67000)
        with open(TEST_STATE_FILE) as f:
            data = json.load(f)
        assert "BTC-USDT" in data["positions"]

    def test_reconcile_removes_stale_local_position(self, app_module):
        app_module.state.record_open("BTC-USDT", "LONG", 0.01, 65000, 64000, 67000)
        fake_bx = MagicMock()
        fake_bx.get_positions.return_value = []  # BingX dice que no hay nada abierto
        app_module.state.reconcile(fake_bx)
        assert app_module.state.get_open("BTC-USDT") is None

    def test_reconcile_imports_untracked_live_position(self, app_module):
        fake_bx = MagicMock()
        fake_bx.get_positions.return_value = [
            {"symbol": "ETH-USDT", "positionAmt": "1.5", "positionSide": "LONG", "avgPrice": "3000"}
        ]
        app_module.state.reconcile(fake_bx)
        pos = app_module.state.get_open("ETH-USDT")
        assert pos is not None
        assert pos["qty"] == 1.5
        assert pos.get("imported_on_reconcile") is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
