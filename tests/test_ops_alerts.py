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
def test_a_broken_webhook_never_raises(conn, monkeypatch):
    """Una alerta no puede tumbar el proceso del que avisa."""
    monkeypatch.setenv("HANDOFF_ALERT_WEBHOOK_URL", WEBHOOK)
    respx.post(WEBHOOK).mock(return_value=httpx.Response(500))
    ops_alerts.alert("slack_auth", "token revocado")


@respx.mock
def test_without_a_webhook_no_request_is_made(conn):
    # respx.mock sin rutas hace fallar cualquier petición: si pasa, no hubo ninguna.
    ops_alerts.alert("slack_auth", "token revocado")
