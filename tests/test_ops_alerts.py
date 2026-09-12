import logging

import httpx
import respx

from handoff_agent import ops_alerts

WEBHOOK = "https://hooks.slack.com/services/T000/B000/XXXX"


def _nuestros_logs(caplog) -> str:
    """Solo las líneas que escribimos nosotros.

    Que httpx no loguee la URL completa por su cuenta se apaga una vez por
    proceso en cli.main, y eso lo prueba test_cli. Apagarlo aquí dentro haría
    que estas aserciones pasaran aunque borráramos la protección de verdad.
    """
    return "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("httpx"))


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
    """Una alerta no puede tumbar el proceso del que avisa, ni dejar la URL del
    webhook (que es la credencial) en nuestros logs."""
    monkeypatch.setenv("HANDOFF_ALERT_WEBHOOK_URL", WEBHOOK)
    respx.post(WEBHOOK).mock(return_value=httpx.Response(500))
    caplog.set_level(logging.DEBUG)
    ops_alerts.alert("slack_auth", "token revocado")
    # Si no capturáramos nuestro propio logging, lo de abajo pasaría en vacío.
    assert "ALERTA" in _nuestros_logs(caplog)
    assert "XXXX" not in _nuestros_logs(caplog)
    assert "hooks.slack.com" not in _nuestros_logs(caplog)


@respx.mock
def test_without_a_webhook_no_request_is_made(conn):
    # respx.mock sin rutas hace fallar cualquier petición: si pasa, no hubo ninguna.
    ops_alerts.alert("slack_auth", "token revocado")


@respx.mock
def test_connection_failure_never_raises(conn, monkeypatch, caplog):
    """Una alerta debe tolerar fallos de conexión al webhook."""
    monkeypatch.setenv("HANDOFF_ALERT_WEBHOOK_URL", WEBHOOK)
    respx.post(WEBHOOK).mock(side_effect=httpx.ConnectError("refused"))
    caplog.set_level(logging.DEBUG)
    ops_alerts.alert("slack_auth", "token revocado")
    assert "ALERTA" in _nuestros_logs(caplog)
    assert "XXXX" not in _nuestros_logs(caplog)
    assert "hooks.slack.com" not in _nuestros_logs(caplog)


def test_malformed_url_never_raises(conn, monkeypatch, caplog):
    """Una URL malformada (ej: puerto inválido) debe tolerarse."""
    monkeypatch.setenv("HANDOFF_ALERT_WEBHOOK_URL", "http://example.com:notaport")
    caplog.set_level(logging.DEBUG)
    ops_alerts.alert("slack_auth", "token revocado")
    # No debe crashear ni contar nada de la URL en nuestros logs.
    assert "ALERTA" in _nuestros_logs(caplog)
    assert "notaport" not in _nuestros_logs(caplog)
    assert "example.com" not in _nuestros_logs(caplog)


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
