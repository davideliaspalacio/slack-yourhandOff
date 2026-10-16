"""El modo automático de canales: descubrimiento, rotación y el ritmo de los miembros.

Lo que estos tests cuidan, por orden de importancia:

1. Que apagado -- el camino de siempre -- nada cambie. `watch_tick` sin `rotando` no
   escribe una sola fila de rotación y revisa los miembros en cada vuelta, como antes.
2. Que el agente nunca vigile un canal donde la cuenta no está, ni el canal donde
   publica sus tarjetas.
3. Que la escalera del retroceso suba y baje donde debe, para no tirar llamadas.
"""

from datetime import UTC, datetime, timedelta

import pytest

from handoff_agent import canales, db
from handoff_agent.ingest import descubrimiento, watcher
from tests.slack_fakes import FakeReader

NOW = 1_726_000_000.0


def ts(seconds_ago: float) -> str:
    return f"{NOW - seconds_ago:.6f}"


def descubierto(channel_id: str, nombre: str, dentro: bool = True) -> dict:
    return {"id": channel_id, "name": nombre, "is_member": dentro}


def fila(channel_id: str) -> dict | None:
    return db.fetch_one("select * from canales where channel_id = %s", (channel_id,))


def modo(conn, encendido: bool) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "insert into config (key, value) values ('slack_canales_todos', %s::jsonb) "
            "on conflict (key) do update set value = excluded.value",
            ("true" if encendido else "false",),
        )


# ---------------------------------------------------------------------------------
# Descubrimiento
# ---------------------------------------------------------------------------------


def test_discovery_stores_name_and_membership(conn):
    conteo = descubrimiento.guardar_descubrimiento(
        [descubierto("C00001", "general"), descubierto("C00002", "lurking", dentro=False)]
    )
    assert conteo == {"vistos": 2, "dentro": 1}
    assert fila("C00001")["nombre"] == "general"
    assert fila("C00001")["es_miembro"] is True
    # El canal donde no está se guarda igual, para poder enseñarlo, pero no se vigila.
    assert fila("C00002")["es_miembro"] is False


def test_discovery_never_marks_the_cards_channel_as_watchable(conn):
    """El canal donde el bot publica no se vigila ni por accidente: leerse a sí mismo."""
    descubrimiento.guardar_descubrimiento(
        [descubierto("C00009", "handoff-leads")], tarjetas="C00009"
    )
    assert fila("C00009")["es_miembro"] is False
    assert canales.vigilados_automaticos() == ()


def test_discovery_keeps_a_name_it_already_knew(conn):
    """Un descubrimiento sin nombre no borra el que el watcher ya había averiguado."""
    descubrimiento.guardar_descubrimiento([descubierto("C00001", "general")])
    descubrimiento.guardar_descubrimiento([{"id": "C00001", "is_member": True}])
    assert fila("C00001")["nombre"] == "general"


def test_discovery_ignores_junk_ids(conn):
    descubrimiento.guardar_descubrimiento([{"id": "no-es-un-canal", "is_member": True}, {"id": ""}])
    assert canales.vigilados_automaticos() == ()


def test_a_channel_the_account_left_stops_being_watched(conn):
    descubrimiento.guardar_descubrimiento([descubierto("C00001", "general")])
    assert canales.vigilados_automaticos() == ("C00001",)
    descubrimiento.guardar_descubrimiento([descubierto("C00001", "general", dentro=False)])
    assert canales.vigilados_automaticos() == ()


# ---------------------------------------------------------------------------------
# Selección y rotación
# ---------------------------------------------------------------------------------


def test_a_never_polled_channel_goes_first(conn):
    descubrimiento.guardar_descubrimiento([descubierto("C00001", "a"), descubierto("C00002", "b")])
    descubrimiento.marcar_sondeado("C00001", 0)
    # C2 no se ha leído nunca: va antes que C1, que ya tiene turno asignado.
    assert canales.por_sondear()[0] == "C00002"


def test_only_channels_the_account_is_in_are_polled(conn):
    descubrimiento.guardar_descubrimiento(
        [descubierto("C00001", "a"), descubierto("C00002", "b", dentro=False)]
    )
    assert canales.por_sondear() == ["C00001"]


def test_a_channel_that_is_not_due_yet_is_left_alone(conn):
    descubrimiento.guardar_descubrimiento([descubierto("C00001", "a")])
    descubrimiento.marcar_sondeado("C00001", 3)
    assert canales.por_sondear() == []


def test_the_per_tick_limit_is_respected(conn):
    descubrimiento.guardar_descubrimiento([descubierto(f"C0000{n}", f"c{n}") for n in range(1, 8)])
    assert len(canales.por_sondear(limite=3)) == 3


def test_activity_puts_a_channel_on_the_first_step(conn):
    descubrimiento.guardar_descubrimiento([descubierto("C00001", "a")])
    # Tres vueltas vacías lo alejan; un mensaje lo trae de vuelta al principio.
    for _ in range(3):
        descubrimiento.marcar_sondeado("C00001", 0)
    assert fila("C00001")["vueltas_vacias"] == 3
    descubrimiento.marcar_sondeado("C00001", 2)
    f = fila("C00001")
    assert f["vueltas_vacias"] == 0
    assert f["mensajes_vistos"] == 2
    espera = (f["siguiente_sondeo_at"] - datetime.now(UTC)).total_seconds() / 60
    assert espera == pytest.approx(canales.ESCALERA_MINUTOS[0], abs=1)


def test_an_empty_poll_backs_the_channel_off_one_step(conn):
    descubrimiento.guardar_descubrimiento([descubierto("C00001", "a")])
    descubrimiento.marcar_sondeado("C00001", 0)
    espera = (fila("C00001")["siguiente_sondeo_at"] - datetime.now(UTC)).total_seconds() / 60
    assert espera == pytest.approx(canales.ESCALERA_MINUTOS[1], abs=1)


def test_the_backoff_stops_at_the_last_step(conn):
    """Un canal muerto no se aleja para siempre: se queda en el último escalón."""
    descubrimiento.guardar_descubrimiento([descubierto("C00001", "a")])
    for _ in range(len(canales.ESCALERA_MINUTOS) + 5):
        descubrimiento.marcar_sondeado("C00001", 0)
    espera = (fila("C00001")["siguiente_sondeo_at"] - datetime.now(UTC)).total_seconds() / 60
    assert espera == pytest.approx(canales.ESCALERA_MINUTOS[-1], abs=1)


# ---------------------------------------------------------------------------------
# El ritmo de la lista de miembros
# ---------------------------------------------------------------------------------


def test_members_are_due_for_a_channel_never_checked(conn):
    descubrimiento.guardar_descubrimiento([descubierto("C00001", "a")])
    assert canales.toca_miembros("C00001") is True


def test_members_are_not_due_right_after_checking(conn):
    descubrimiento.guardar_descubrimiento([descubierto("C00001", "a")])
    descubrimiento.marcar_miembros("C00001")
    assert canales.toca_miembros("C00001") is False


def test_members_are_due_again_after_the_wait(conn):
    descubrimiento.guardar_descubrimiento([descubierto("C00001", "a")])
    with conn.cursor() as cur:
        cur.execute(
            "update canales set miembros_revisados_at = %s where channel_id = 'C00001'",
            (datetime.now(UTC) - timedelta(hours=canales.HORAS_ENTRE_MIEMBROS + 1),),
        )
    assert canales.toca_miembros("C00001") is True


# ---------------------------------------------------------------------------------
# El watcher: el camino de siempre no cambia
# ---------------------------------------------------------------------------------


def test_the_usual_path_writes_no_rotation_state(conn):
    """Sin `rotando`, el watcher no toca nada de la rotación: el modo a mano va igual."""
    reader = FakeReader()
    reader.post("C00001", "U1", "hola", ts(60))
    watcher.watch_tick(reader, ["C00001"], lookback_hours=1, now=lambda: NOW)
    f = fila("C00001")
    # La fila existe porque el watcher guarda el nombre, pero sin rastro de rotación.
    assert f["sondeado_at"] is None
    assert f["siguiente_sondeo_at"] is None
    assert f["mensajes_vistos"] == 0
    assert f["miembros_revisados_at"] is None


def test_the_usual_path_still_diffs_members_every_tick(conn):
    reader = FakeReader()
    reader.post("C00001", "U1", "hola", ts(60))
    for _ in range(2):
        watcher.watch_tick(reader, ["C00001"], lookback_hours=1, now=lambda: NOW)
    assert reader.member_calls.count("C00001") == 2


# ---------------------------------------------------------------------------------
# El watcher rotando
# ---------------------------------------------------------------------------------


def test_rotating_records_the_poll_and_the_messages(conn):
    reader = FakeReader()
    reader.post("C00001", "U1", "hola", ts(60))
    descubrimiento.guardar_descubrimiento([descubierto("C00001", "a")])
    watcher.watch_tick(reader, ["C00001"], lookback_hours=1, now=lambda: NOW, rotando=True)
    f = fila("C00001")
    assert f["sondeado_at"] is not None
    assert f["mensajes_vistos"] == 1
    assert f["vueltas_vacias"] == 0


def test_rotating_only_diffs_members_when_due(conn):
    reader = FakeReader()
    reader.post("C00001", "U1", "hola", ts(60))
    descubrimiento.guardar_descubrimiento([descubierto("C00001", "a")])
    for _ in range(3):
        watcher.watch_tick(reader, ["C00001"], lookback_hours=1, now=lambda: NOW, rotando=True)
    # La primera vuelta sí (nunca se había mirado); las siguientes no, porque el ritmo
    # de los miembros es de horas. Es la llamada más cara que hay.
    assert reader.member_calls.count("C00001") == 1


def test_a_failing_channel_still_backs_off(conn):
    """Si un canal falla, su escalón sube igual: no se queda pidiendo turno cada vuelta
    por delante de los demás."""
    reader = FakeReader()
    reader.unavailable_channels = {"C00001"}
    descubrimiento.guardar_descubrimiento([descubierto("C00001", "a")])
    result = watcher.watch_tick(reader, ["C00001"], lookback_hours=1, now=lambda: NOW, rotando=True)
    assert result.errors
    assert fila("C00001")["siguiente_sondeo_at"] is not None
    assert canales.por_sondear() == []


# ---------------------------------------------------------------------------------
# El interruptor
# ---------------------------------------------------------------------------------


def test_the_switch_is_off_by_default(restore_config):
    assert canales.modo_todos() is False


def test_the_switch_reads_the_database(restore_config):
    modo(restore_config, True)
    assert canales.modo_todos() is True
    modo(restore_config, False)
    assert canales.modo_todos() is False
