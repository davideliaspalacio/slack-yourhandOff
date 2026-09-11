import logging

import httpx
import respx

from handoff_agent import ops_alerts

WEBHOOK = "https://hooks.slack.com/services/T000/B000/XXXX"


def test_an_alert_is_logged_and_recorded(conn, caplog):
    with caplog.at_level(logging.CRITICAL):
        ops_alerts.alert("slack_auth", "token revocado")
    assert "token revocado" in caplog.text
    with conn.cursor() as cur:
        cur.execute("select payload from agent_actions where action = 'alerta_operativa'")
        assert cur.fetchone()[0] == {"tipo": "slack_auth", "mensaje": "token revocado"}


@respx.mock
def test_an_alert_reaches_the_handoff_webhook_when_configured(conn, monkeypatch):
    monkeypatch.setenv("HANDOFF_ALERT_WEBHOOK_URL", WEBHOOK)
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200))
    ops_alerts.alert("slack_auth", "token revocado")
    assert route.called
    assert "token revocado" in route.calls[0].request.content.decode()


@respx.mock
def test_a_broken_webhook_never_raises(conn, monkeypatch, caplog):
    """Una alerta no puede tumbar el proceso del que avisa."""
    monkeypatch.setenv("HANDOFF_ALERT_WEBHOOK_URL", WEBHOOK)
    respx.post(WEBHOOK).mock(return_value=httpx.Response(500))
    # Suppress httpx logging to test our code's credential protection
    httpx_logger = logging.getLogger("httpx")
    old_level = httpx_logger.level
    httpx_logger.setLevel(logging.WARNING)
    try:
        caplog.set_level(logging.DEBUG)
        ops_alerts.alert("slack_auth", "token revocado")
        assert "XXXX" not in caplog.text
        assert "hooks.slack.com" not in caplog.text
    finally:
        httpx_logger.setLevel(old_level)


@respx.mock
def test_without_a_webhook_no_request_is_made(conn):
    # respx.mock sin rutas hace fallar cualquier petición: si pasa, no hubo ninguna.
    ops_alerts.alert("slack_auth", "token revocado")


@respx.mock
def test_connection_failure_never_raises(conn, monkeypatch, caplog):
    """Una alerta debe tolerar fallos de conexión al webhook."""
    monkeypatch.setenv("HANDOFF_ALERT_WEBHOOK_URL", WEBHOOK)
    respx.post(WEBHOOK).mock(side_effect=httpx.ConnectError("refused"))
    # Suppress httpx logging to test our code's credential protection
    httpx_logger = logging.getLogger("httpx")
    old_level = httpx_logger.level
    httpx_logger.setLevel(logging.WARNING)
    try:
        caplog.set_level(logging.DEBUG)
        ops_alerts.alert("slack_auth", "token revocado")
        assert "XXXX" not in caplog.text
        assert "hooks.slack.com" not in caplog.text
    finally:
        httpx_logger.setLevel(old_level)


def test_malformed_url_never_raises(conn, monkeypatch, caplog):
    """Una URL malformada (ej: puerto inválido) debe tolerarse."""
    monkeypatch.setenv("HANDOFF_ALERT_WEBHOOK_URL", "http://example.com:notaport")
    # Suppress httpx logging to test our code's credential protection
    httpx_logger = logging.getLogger("httpx")
    old_level = httpx_logger.level
    httpx_logger.setLevel(logging.WARNING)
    try:
        caplog.set_level(logging.DEBUG)
        ops_alerts.alert("slack_auth", "token revocado")
        # No debe crashear ni loguear detalles sobre la URL
        assert "notaport" not in caplog.text
        assert "example.com" not in caplog.text
    finally:
        httpx_logger.setLevel(old_level)


def test_load_settings_failure_never_raises(conn, monkeypatch, caplog):
    """Una alerta debe tolerar fallos al cargar settings."""
    caplog.set_level(logging.DEBUG)

    def raise_error(*args, **kwargs):
        raise RuntimeError("DATABASE_URL is not set")

    monkeypatch.setattr(ops_alerts, "load_settings", raise_error)
    ops_alerts.alert("slack_auth", "token revocado")

    # La alerta crítica debe seguir siendo registrada
    assert "ALERTA slack_auth: token revocado" in caplog.text
    # El ledger debe seguir siendo escrito
    with conn.cursor() as cur:
        cur.execute(
            "select payload from agent_actions where action = 'alerta_operativa' order by created_at desc limit 1"
        )
        payload = cur.fetchone()[0]
        assert payload == {"tipo": "slack_auth", "mensaje": "token revocado"}
