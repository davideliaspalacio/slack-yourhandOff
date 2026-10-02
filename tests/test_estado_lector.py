import json

from handoff_agent import estado_lector


def _estado(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("select value::text from config where key = 'lector_estado'")
        return json.loads(cur.fetchone()[0])


def test_the_migration_seeds_an_empty_row(conn):
    # Antes de que nadie escriba, o con la fila recién sembrada, no hay latido.
    with conn.cursor() as cur:
        cur.execute("select count(*) from config where key = 'lector_estado'")
        assert cur.fetchone()[0] == 1


def test_an_ok_heartbeat_is_stored_with_user_team_and_channels(restore_config):
    estado_lector.registrar_ok("UANTHONY", "Handoff", 3)
    estado = _estado(restore_config)
    assert estado["ok"] is True
    assert (estado["usuario"], estado["equipo"], estado["canales"]) == ("UANTHONY", "Handoff", 3)
    assert estado["en"]


def test_a_failure_heartbeat_has_the_exception_type_and_no_credentials(restore_config):
    exc = RuntimeError("falló con xoxc-1234-abcd y la cookie xoxd-AbC%2Bdef=  rara")
    estado_lector.registrar_fallo(exc)
    estado = _estado(restore_config)
    assert estado["ok"] is False
    assert estado["error"] == "RuntimeError"
    assert "xoxc" not in estado["detalle"] and "xoxd" not in estado["detalle"]
    assert "[credencial]" in estado["detalle"]


def test_a_long_detail_is_truncated(restore_config):
    estado_lector.registrar_fallo(ValueError("x" * 1000))
    assert len(_estado(restore_config)["detalle"]) <= estado_lector.DETALLE_MAX


def test_a_write_that_fails_is_swallowed(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("base caída")

    monkeypatch.setattr(estado_lector.db, "execute", broken)
    estado_lector.registrar_ok("U1", "T", 1)
    estado_lector.registrar_fallo(ValueError("x"))
