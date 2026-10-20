"""El texto de los DMs con leads (0031): qué se lee, qué no se abre nunca y qué se
borra. Lo primero que vigila este archivo es el interruptor: apagado no se le pide
nada a Slack y lo guardado desaparece."""

import time

import pytest

from handoff_agent import db
from handoff_agent import slack_client as sc
from handoff_agent.ingest import dm_texto
from handoff_agent.slack_client import SlackAuthFailed, SlackUnavailable
from tests.slack_fakes import FakeReader
from tests.test_slack_client import JAMAS, READ_METHODS, FakeWebClient

DIA = 24.0


def ts(horas: float = 0.0) -> str:
    return f"{time.time() - horas * 3600:.6f}"


def poner(clave: str, valor: str) -> None:
    db.execute("update config set value = %s::jsonb where key = %s", (valor, clave))


@pytest.fixture
def encendido(restore_config):
    poner("dm_texto_activo", "true")
    return restore_config


@pytest.fixture
def reader(conn):
    return FakeReader(owner="UOWNER")


def ficha(user: str, estado: str = "investigado", sin_tarjeta: bool = False) -> str:
    return db.fetch_one(
        "insert into prospects (slack_user_id, full_name, state, sin_tarjeta) "
        "values (%s, %s, %s, %s) returning id",
        (user, f"Persona {user}", estado, sin_tarjeta),
    )["id"]


def traza(dm_id: str, user: str, *, prospect_id=None, horas: float = 1.0) -> str:
    """Una conversación en el rastro, como la deja ingest/dms.py."""
    db.execute(
        "insert into dm_rastro (dm_id, team_id, slack_user_id, prospect_id, ultimo_ts, ultimo_de) "
        "values (%s, 'T0OWNER', %s, %s, %s, 'otro')",
        (dm_id, user, prospect_id, ts(horas)),
    )
    return dm_id


def lead(dm_id: str, user: str, **kw) -> str:
    return traza(dm_id, user, prospect_id=ficha(user, **kw))


def guardados(dm_id: str | None = None) -> list[dict]:
    if dm_id is None:
        return db.fetch_all("select * from dm_mensajes order by dm_id, ts::numeric")
    return db.fetch_all("select * from dm_mensajes where dm_id = %s order by ts::numeric", (dm_id,))


def guardar(dm_id: str, ts_: str, texto: str = "hola", de: str = "otro") -> None:
    db.execute(
        "insert into dm_mensajes (dm_id, ts, de, texto) values (%s, %s, %s, %s)",
        (dm_id, ts_, de, texto),
    )


# --- el interruptor ---------------------------------------------------------------


def test_the_switch_is_off_by_default(conn):
    assert dm_texto.activo() is False


def test_off_means_nothing_is_fetched_and_everything_is_wiped(conn, reader):
    lead("DAAA", "UANA")
    guardar("DAAA", ts(2), "el contrato firmado")
    db.execute(
        "insert into dm_sugerencias (dm_id, ultimo_ts, contexto, sugerencia) "
        "values ('DAAA', '1', 'ctx', 'sug')"
    )
    db.execute("update dm_rastro set sin_leer = 3, leido_hasta = '1.0' where dm_id = 'DAAA'")
    reader.post("DAAA", "UANA", "algo nuevo", ts(0.5))
    # Ni siquiera auth.test: con el interruptor apagado, Slack no se entera de nada.
    reader.auth_error = SlackAuthFailed("no debería preguntarse")

    resultado = dm_texto.pasada(reader)

    assert resultado.borrados == 2
    assert guardados() == []
    assert db.fetch_all("select * from dm_sugerencias") == []
    fila = db.fetch_one("select sin_leer, leido_hasta from dm_rastro where dm_id = 'DAAA'")
    assert fila == {"sin_leer": None, "leido_hasta": None}
    assert reader.history_calls == []
    assert reader.estado_calls == []


def test_off_the_conversation_reads_as_empty_even_before_the_wipe(encendido):
    lead("DAAA", "UANA")
    guardar("DAAA", ts(1), "hola")
    assert dm_texto.conversacion("DAAA") != []
    poner("dm_texto_activo", "false")
    assert dm_texto.conversacion("DAAA") == []


def test_with_follow_ups_off_nothing_is_read_but_stored_text_stays(encendido, reader):
    """El panel promete que con el seguimiento apagado no se leen DMs: tampoco el texto."""
    poner("seguimiento_activo", "false")
    lead("DAAA", "UANA")
    guardar("DAAA", ts(2), "hola")
    reader.post("DAAA", "UANA", "nuevo", ts(0.5))
    reader.auth_error = SlackAuthFailed("no debería preguntarse")

    dm_texto.pasada(reader)

    assert reader.history_calls == []
    assert reader.estado_calls == []
    assert [m["texto"] for m in guardados("DAAA")] == ["hola"]


# --- solo leads -------------------------------------------------------------------


def test_only_researched_live_leads_are_opened(encendido, reader):
    lead("DLEAD", "ULEAD")
    traza("DNADIE", "UNADIE")  # nunca investigado: sin ficha
    lead("DDESC", "UDESC", estado="descartado")
    lead("DSINT", "USINT", sin_tarjeta=True)
    for dm_id, user in [
        ("DLEAD", "ULEAD"),
        ("DNADIE", "UNADIE"),
        ("DDESC", "UDESC"),
        ("DSINT", "USINT"),
    ]:
        reader.post(dm_id, user, "secreto", ts(0.5))

    resultado = dm_texto.pasada(reader)

    abiertos = {canal for canal, _ in reader.history_calls}
    assert abiertos == {"DLEAD"}
    assert set(reader.estado_calls) == {"DLEAD"}
    assert {m["dm_id"] for m in guardados()} == {"DLEAD"}
    assert resultado.conversaciones == 1


def test_a_conversation_quiet_for_longer_than_the_retention_is_not_opened(encendido, reader):
    traza("DVIEJO", "UVIEJO", prospect_id=ficha("UVIEJO"), horas=11 * DIA)
    dm_texto.pasada(reader)
    assert reader.history_calls == []


def test_someone_discarded_after_the_fact_loses_their_stored_text(encendido, reader):
    lead("DAAA", "UANA")
    guardar("DAAA", ts(1), "hola")
    db.execute("update prospects set state = 'descartado' where slack_user_id = 'UANA'")

    resultado = dm_texto.pasada(reader)

    assert guardados() == []
    assert resultado.purgados == 1
    assert reader.history_calls == []


def sugerencia_guardada(dm_id: str, ultimo_ts: str) -> None:
    db.execute(
        "insert into dm_sugerencias (dm_id, ultimo_ts, contexto, sugerencia) "
        "values (%s, %s, 'She asked about support reps.', 'Happy to help, how many?')",
        (dm_id, ultimo_ts),
    )


def sugerencias() -> list[str]:
    return [f["dm_id"] for f in db.fetch_all("select dm_id from dm_sugerencias")]


def test_someone_discarded_after_the_fact_loses_their_suggestion_too(encendido, reader):
    """La sugerencia corre la suerte del texto del que salió: un lead descartado no
    puede seguir enseñándola en el panel ni en Claude."""
    lead("DAAA", "UANA")
    guardar("DAAA", ts(1), "hola")
    sugerencia_guardada("DAAA", ts(1))
    db.execute("update prospects set state = 'descartado' where slack_user_id = 'UANA'")

    dm_texto.pasada(reader)

    assert sugerencias() == []


def test_a_suggestion_older_than_the_retention_goes_with_its_text(encendido, reader):
    """La línea de contexto resume el texto: cuando el texto caduca, ella también."""
    lead("DAAA", "UANA")
    sugerencia_guardada("DAAA", ts(24 * 30))

    dm_texto.pasada(reader)

    assert sugerencias() == []


def test_a_live_leads_recent_suggestion_stays(encendido, reader):
    lead("DAAA", "UANA")
    sugerencia_guardada("DAAA", ts(1))
    dm_texto.pasada(reader)
    assert sugerencias() == ["DAAA"]


def test_the_pass_is_capped(encendido, reader, monkeypatch):
    monkeypatch.setattr(dm_texto, "LIMITE_POR_PASADA", 2)
    for i, horas in enumerate([5.0, 1.0, 3.0]):
        traza(f"D00{i}", f"U00{i}", prospect_id=ficha(f"U00{i}"), horas=horas)
    dm_texto.pasada(reader)
    # Las dos con movimiento más reciente primero.
    assert [canal for canal, _ in reader.history_calls] == ["D001", "D002"]


# --- qué se trae --------------------------------------------------------------------


def test_the_first_read_starts_at_the_retention_cutoff(encendido, reader):
    lead("DAAA", "UANA")
    dm_texto.pasada(reader)
    ((_, oldest),) = reader.history_calls
    assert float(oldest) == pytest.approx(time.time() - 10 * 86400, abs=60)


def test_only_messages_after_the_last_stored_one_are_fetched(encendido, reader):
    lead("DAAA", "UANA")
    ya = ts(3)
    guardar("DAAA", ya, "lo de antes")
    reader.post("DAAA", "UANA", "lo de antes", ya)
    reader.post("DAAA", "UANA", "aún más viejo", ts(4))
    reader.post("DAAA", "UANA", "lo nuevo", ts(1))

    resultado = dm_texto.pasada(reader)

    assert reader.history_calls == [("DAAA", ya)]
    assert [m["texto"] for m in guardados("DAAA")] == ["lo de antes", "lo nuevo"]
    assert resultado.guardados == 1


def test_a_second_pass_stores_nothing_twice(encendido, reader):
    lead("DAAA", "UANA")
    reader.post("DAAA", "UANA", "hola", ts(1))
    dm_texto.pasada(reader)
    segunda = dm_texto.pasada(reader)
    assert len(guardados("DAAA")) == 1
    assert segunda.guardados == 0


def test_who_wrote_it_and_what_is_skipped(encendido, reader):
    lead("DAAA", "UANA")
    reader.post("DAAA", "UOWNER", "te paso la propuesta", ts(5))
    reader.post("DAAA", "UANA", "genial, gracias", ts(4))
    reader.post("DAAA", "UANA", "   ", ts(3.5))  # sin texto
    reader.post("DAAA", "UANA", "", ts(3.4), files=[{"id": "F1"}])  # un archivo suelto
    reader.post("DAAA", "UAPP", "recordatorio", ts(3), bot_id="B1")
    reader.post("DAAA", "USLACKBOT", "Reminder", ts(2.5), subtype="bot_message")
    reader.post("DAAA", "UANA", "(borrado)", ts(2), subtype="tombstone")
    reader.post("DAAA", None, "sin autor", ts(1.5))

    resultado = dm_texto.pasada(reader)

    assert [(m["de"], m["texto"]) for m in guardados("DAAA")] == [
        ("dueno", "te paso la propuesta"),
        ("otro", "genial, gracias"),
    ]
    assert resultado.omitidos == 6


def test_messages_older_than_the_retention_are_never_stored(encendido, reader, monkeypatch):
    """Aunque Slack los devuelva (por ejemplo, desde un último guardado viejo)."""
    lead("DAAA", "UANA")
    reader.post("DAAA", "UANA", "viejísimo", ts(12 * DIA))
    reader.post("DAAA", "UANA", "reciente", ts(1))
    # Un Slack que ignora `oldest`.
    monkeypatch.setattr(reader, "history", lambda canal, oldest: reader.messages[canal])
    dm_texto.pasada(reader)
    assert [m["texto"] for m in guardados("DAAA")] == ["reciente"]


# --- lo no leído ----------------------------------------------------------------------


def test_the_unread_state_is_stored(encendido, reader):
    lead("DAAA", "UANA")
    reader.post("DAAA", "UANA", "¿lo viste?", ts(1))
    reader.estados["DAAA"] = {"sin_leer": 2, "leido_hasta": "1700000000.000100"}
    dm_texto.pasada(reader)
    fila = db.fetch_one("select sin_leer, leido_hasta from dm_rastro where dm_id = 'DAAA'")
    assert fila == {"sin_leer": 2, "leido_hasta": "1700000000.000100"}


def test_the_unread_state_is_refreshed_even_with_nothing_new(encendido, reader):
    lead("DAAA", "UANA")
    reader.estados["DAAA"] = {"sin_leer": 0, "leido_hasta": "1.0"}
    dm_texto.pasada(reader)
    assert reader.estado_calls == ["DAAA"]
    assert db.fetch_one("select sin_leer from dm_rastro where dm_id = 'DAAA'")["sin_leer"] == 0


# --- retención --------------------------------------------------------------------------


def test_the_retention_purge(encendido, reader):
    lead("DAAA", "UANA")
    guardar("DAAA", ts(11 * DIA), "viejo")
    guardar("DAAA", ts(9 * DIA), "aún vale")

    resultado = dm_texto.pasada(reader)

    assert [m["texto"] for m in guardados("DAAA")] == ["aún vale"]
    assert resultado.purgados == 1


def test_the_retention_follows_the_setting(encendido, reader):
    poner("dm_texto_retencion_dias", "1")
    lead("DAAA", "UANA")
    guardar("DAAA", ts(2 * DIA), "de anteayer")
    guardar("DAAA", ts(2), "de hoy")
    dm_texto.pasada(reader)
    assert [m["texto"] for m in guardados("DAAA")] == ["de hoy"]


# --- fallos ---------------------------------------------------------------------------


def test_one_failing_conversation_does_not_stop_the_others(encendido, reader):
    lead("DAAA", "UANA")
    lead("DBBB", "UBEA")
    lead("DCCC", "UCRIS")
    for dm_id, user in [("DAAA", "UANA"), ("DBBB", "UBEA"), ("DCCC", "UCRIS")]:
        reader.post(dm_id, user, f"hola desde {dm_id}", ts(1))
    reader.unavailable_channels.add("DBBB")

    resultado = dm_texto.pasada(reader)

    assert {m["dm_id"] for m in guardados()} == {"DAAA", "DCCC"}
    assert len(resultado.errores) == 1
    assert resultado.errores[0].startswith("DBBB")


def test_a_failing_unread_lookup_keeps_the_text_and_the_others(encendido, reader, monkeypatch):
    lead("DAAA", "UANA")
    lead("DBBB", "UBEA")
    reader.post("DAAA", "UANA", "uno", ts(1))
    reader.post("DBBB", "UBEA", "dos", ts(1))
    original = reader.dm_estado

    def estado(dm_id):
        if dm_id == "DAAA":
            raise SlackUnavailable("conversations_info: internal_error")
        return original(dm_id)

    monkeypatch.setattr(reader, "dm_estado", estado)
    resultado = dm_texto.pasada(reader)
    assert {m["dm_id"] for m in guardados()} == {"DAAA", "DBBB"}
    assert [e.split(":")[0] for e in resultado.errores] == ["DAAA"]


def test_a_dead_credential_propagates(encendido, reader):
    lead("DAAA", "UANA")
    reader.auth_error = SlackAuthFailed("token_revoked")
    with pytest.raises(SlackAuthFailed):
        dm_texto.pasada(reader)


def test_with_no_leads_slack_is_not_asked_anything(encendido, reader):
    traza("DNADIE", "UNADIE")
    reader.auth_error = SlackAuthFailed("no debería preguntarse")
    assert dm_texto.pasada(reader).conversaciones == 0


# --- conversacion() -----------------------------------------------------------------------


def test_the_conversation_is_oldest_first_and_limited(encendido):
    lead("DAAA", "UANA")
    lead("DBBB", "UBEA")
    for i in range(5):
        guardar("DAAA", ts(10 - i), f"m{i}", de="dueno" if i % 2 else "otro")
    guardar("DBBB", ts(1), "de otra conversación")

    todo = dm_texto.conversacion("DAAA")
    assert [m["texto"] for m in todo] == ["m0", "m1", "m2", "m3", "m4"]
    assert set(todo[0]) == {"ts", "de", "texto"}
    assert todo[1]["de"] == "dueno"

    ultimos = dm_texto.conversacion("DAAA", limite=2)
    assert [m["texto"] for m in ultimos] == ["m3", "m4"]


def test_the_conversation_orders_timestamps_as_numbers(encendido):
    """`ts` es texto: '999.1' va antes que '1000.1', aunque como cadena vaya después."""
    lead("DAAA", "UANA")
    guardar("DAAA", "1000.000100", "segundo")
    guardar("DAAA", "999.000100", "primero")
    assert [m["texto"] for m in dm_texto.conversacion("DAAA")] == ["primero", "segundo"]


# --- la lista blanca del lector ---------------------------------------------------------


def test_the_pass_only_calls_whitelisted_slack_methods(encendido):
    """De punta a punta con el lector real: nada fuera de READ_METHODS, nada de JAMAS y,
    sobre todo, nada de `conversations.mark`."""
    lead("DAAA", "UANA")
    fake = FakeWebClient(
        history_pages=[
            {
                "ok": True,
                "messages": [
                    {"ts": ts(1), "user": "UANA", "text": "hola"},
                    {"ts": ts(2), "user": "UOWNER", "text": "¿qué tal?"},
                ],
            }
        ],
        channel={"id": "DAAA", "is_im": True, "unread_count_display": 1, "last_read": "5.0"},
    )
    reader = sc.FoundersClubReader(client=fake)

    resultado = dm_texto.pasada(reader)

    llamados = {nombre for nombre, _ in fake.calls}
    assert llamados <= READ_METHODS
    assert not (llamados & JAMAS)
    assert llamados == {"auth_test", "conversations_history", "conversations_info"}
    assert resultado.guardados == 2
    fila = db.fetch_one("select sin_leer, leido_hasta from dm_rastro where dm_id = 'DAAA'")
    assert fila == {"sin_leer": 1, "leido_hasta": "5.0"}


def test_dm_estado_reads_conversations_info_and_nothing_else():
    fake = FakeWebClient(
        channel={"id": "D1", "unread_count": 4, "unread_count_display": 2, "last_read": "9.1"}
    )
    reader = sc.FoundersClubReader(client=fake)
    assert reader.dm_estado("D1") == {"sin_leer": 2, "leido_hasta": "9.1"}
    assert fake.calls == [("conversations_info", {"channel": "D1"})]


def test_dm_estado_falls_back_to_unread_count():
    reader = sc.FoundersClubReader(client=FakeWebClient(channel={"id": "D1", "unread_count": 4}))
    assert reader.dm_estado("D1") == {"sin_leer": 4, "leido_hasta": None}


def test_dm_estado_says_unknown_rather_than_zero():
    """Sin contador, None: un 0 diría que lo leyó todo, y no se sabe."""
    reader = sc.FoundersClubReader(client=FakeWebClient(channel={"id": "D1"}))
    assert reader.dm_estado("D1") == {"sin_leer": None, "leido_hasta": None}
