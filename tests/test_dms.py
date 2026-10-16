"""El rastro de los mensajes directos: qué se guarda, qué queda fuera y qué se
borra. Lo primero que vigila este archivo es lo que NO se guarda: el texto de
un DM no puede estar ni en el esquema ni en ninguna consulta."""

import ast
import re
import time
from pathlib import Path

import pytest

from handoff_agent import db
from handoff_agent.ingest import dms
from handoff_agent.slack_client import SlackUnavailable
from tests.slack_fakes import FakeReader

SECRETO = "mándame el contrato firmado y el número de cuenta"

# Las columnas del rastro, escritas a mano: añadir una obliga a venir aquí y
# comprobar que no es contenido de un mensaje.
COLUMNAS = {
    "dm_id",
    "team_id",
    "slack_user_id",
    "prospect_id",
    "ultimo_ts",
    "ultimo_de",
    "avisado_ts",
    # `avisado_at` (0029): cuándo salió el último aviso, para poder recordarlo pasadas
    # unas horas. Una marca de tiempo más, igual que las de arriba: sigue sin haber
    # dónde guardar lo que nadie dijo.
    "avisado_at",
    "actualizado_at",
}
# Nada que huela a contenido puede aparecer como columna.
PROHIBIDAS = ("texto", "text", "mensaje", "message", "body", "cita", "cuerpo", "asunto")


def _cadenas_sin_docstrings(arbol: ast.AST) -> list[str]:
    """Las cadenas del código que de verdad se ejecutan (SQL incluido)."""
    docstrings = {
        id(n.body[0].value)
        for n in ast.walk(arbol)
        if isinstance(n, ast.Module | ast.FunctionDef | ast.ClassDef)
        and n.body
        and isinstance(n.body[0], ast.Expr)
        and isinstance(n.body[0].value, ast.Constant)
    }
    return [
        n.value
        for n in ast.walk(arbol)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    ]


def ts(horas: float = 0.0) -> str:
    return f"{time.time() - horas * 3600:.6f}"


@pytest.fixture
def reader(conn):
    r = FakeReader(owner="UOWNER")
    r.add_profile("UOWNER", "Anthony")
    return r


def dm(reader, dm_id, user, *, de="otro", horas=0.0, texto=SECRETO, nombre="Sunny", **extra):
    """Un DM con un último mensaje de hace `horas`, escrito por él o por el otro."""
    reader.add_dm(dm_id, user, **extra)
    if nombre is not None and user not in reader.profiles:
        reader.add_profile(user, nombre)
    autor = reader.owner if de == "dueno" else user
    reader.post(dm_id, autor, texto, ts(horas))
    return dm_id


def rastro(dm_id=None):
    if dm_id is None:
        return db.fetch_all("select * from dm_rastro order by dm_id")
    return db.fetch_one("select * from dm_rastro where dm_id = %s", (dm_id,))


# --- lo que nunca se guarda ------------------------------------------------------


def test_the_schema_has_no_room_for_a_dm_body(conn):
    with conn.cursor() as cur:
        cur.execute(
            "select column_name, data_type from information_schema.columns "
            "where table_name = 'dm_rastro'"
        )
        columnas = dict(cur.fetchall())
    assert set(columnas) == COLUMNAS
    for columna in columnas:
        assert not any(p in columna.lower() for p in PROHIBIDAS), columna


def test_no_query_in_the_repo_stores_or_selects_a_dm_body(conn):
    """Ni el módulo del rastro ni el del aviso tocan el cuerpo de un mensaje."""
    for modulo in ("src/handoff_agent/ingest/dms.py", "src/handoff_agent/delivery/seguimiento.py"):
        codigo = Path(modulo).read_text()
        # Lo único que se mira de un mensaje de Slack es su ts y su autor.
        arbol = ast.parse(codigo)
        claves = {
            n.slice.value
            for n in ast.walk(arbol)
            if isinstance(n, ast.Subscript)
            and isinstance(n.slice, ast.Constant)
            and isinstance(n.slice.value, str)
        }
        claves |= {
            n.args[0].value
            for n in ast.walk(arbol)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "get"
            and n.args
            and isinstance(n.args[0], ast.Constant)
            and isinstance(n.args[0].value, str)
        }
        assert not {"text", "texto", "blocks", "attachments", "files"} & claves, modulo
        # Y ninguna consulta nombra una columna de contenido. Los docstrings,
        # que sí la nombran para prohibirla, no cuentan: no se ejecutan.
        for cadena in _cadenas_sin_docstrings(arbol):
            # "text" suelto no entra en la lista: `::text` es un cast de
            # Postgres, no una columna de contenido. El acceso al texto de un
            # mensaje ya lo cubre la comprobación de claves de arriba.
            for prohibida in ("texto", "cuerpo", "cita", "body", "mensaje"):
                assert not re.search(rf"\b{prohibida}\b", cadena, re.IGNORECASE), (
                    modulo,
                    prohibida,
                    cadena,
                )


def test_the_stored_row_contains_nothing_of_what_was_said(reader):
    dm(reader, "DAAA", "USUNNY", texto=SECRETO)
    dms.rastro_tick(reader)
    fila = rastro("DAAA")
    guardado = " ".join(str(v) for v in fila.values())
    assert SECRETO not in guardado
    for palabra in ("contrato", "cuenta", "firmado"):
        assert palabra not in guardado


def test_only_the_last_message_of_each_dm_is_read(reader):
    dm(reader, "DAAA", "USUNNY")
    reader.post("DAAA", "USUNNY", "otro mensaje viejo", ts(100))
    reader.post("DAAA", "UOWNER", "y otro", ts(200))
    dms.rastro_tick(reader)
    # Una sola llamada por conversación, y nunca el historial completo.
    assert reader.last_message_calls == ["DAAA"]
    assert reader.history_calls == []


# --- quién entra y quién no -----------------------------------------------------


def test_a_one_to_one_dm_is_tracked_with_who_wrote_last(reader):
    dm(reader, "DAAA", "USUNNY", de="otro", horas=30)
    dms.rastro_tick(reader)
    fila = rastro("DAAA")
    assert fila["slack_user_id"] == "USUNNY"
    assert fila["ultimo_de"] == "otro"
    assert fila["team_id"] == "T0OWNER"
    assert fila["avisado_ts"] is None


def test_group_dms_and_channels_stay_out(reader):
    dm(reader, "DAAA", "USUNNY")
    # Un mpim (grupo) y un canal, que Slack no debería devolver con types=im
    # pero el rastro descarta igual.
    dm(reader, "DGROUP", "UOTHER", is_mpim=True)
    reader.add_dm("C0CANAL", "UOTHER")
    reader.post("C0CANAL", "UOTHER", "hola", ts())
    resultado = dms.rastro_tick(reader)
    assert [f["dm_id"] for f in rastro()] == ["DAAA"]
    assert resultado.omitidos == 2


def test_bots_and_slackbot_stay_out(reader):
    dm(reader, "DAAA", "USUNNY")
    reader.add_dm("DBOT", "UBOT")
    reader.add_profile("UBOT", "Handoff Bot", is_bot=True)
    reader.post("DBOT", "UBOT", "hola", ts())
    dm(reader, "DSLACK", dms.SLACKBOT, nombre=None)
    # El DM consigo mismo tampoco.
    dm(reader, "DSELF", "UOWNER", de="dueno")
    dms.rastro_tick(reader)
    assert [f["dm_id"] for f in rastro()] == ["DAAA"]


def test_a_dm_with_someone_whose_profile_slack_will_not_give_is_not_tracked(reader):
    reader.add_dm("DGHOST", "UGHOST")  # sin perfil: users.info dice user_not_found
    reader.post("DGHOST", "UGHOST", "hola", ts())
    dms.rastro_tick(reader)
    assert rastro() == []


def test_an_empty_dm_is_not_tracked(reader):
    reader.add_dm("DVACIO", "USUNNY")
    reader.add_profile("USUNNY", "Sunny")
    dms.rastro_tick(reader)
    assert rastro() == []


def test_the_trace_links_to_the_prospect_when_there_is_one(reader):
    db.execute("insert into prospects (slack_user_id, full_name) values ('USUNNY', 'Sunny')")
    persona = db.fetch_one("select id from prospects where slack_user_id = 'USUNNY'")
    dm(reader, "DAAA", "USUNNY")
    dms.rastro_tick(reader)
    assert rastro("DAAA")["prospect_id"] == persona["id"]


def test_the_link_is_made_later_when_the_person_becomes_a_prospect(reader):
    dm(reader, "DAAA", "USUNNY")
    dms.rastro_tick(reader)
    assert rastro("DAAA")["prospect_id"] is None
    db.execute("insert into prospects (slack_user_id, full_name) values ('USUNNY', 'Sunny')")
    reader.messages["DAAA"] = []
    reader.post("DAAA", "USUNNY", "otra cosa", ts(1))
    dms.rastro_tick(reader)
    assert rastro("DAAA")["prospect_id"] is not None


# --- cambios, retención y fallos ------------------------------------------------


def test_a_new_message_updates_the_trace_and_resets_the_alert(reader):
    dm(reader, "DAAA", "USUNNY", horas=50)
    dms.rastro_tick(reader)
    viejo = rastro("DAAA")["ultimo_ts"]
    db.execute("update dm_rastro set avisado_ts = ultimo_ts where dm_id = 'DAAA'")
    # Él contesta: cambia el último mensaje, y el aviso vuelve a estar vivo.
    reader.messages["DAAA"] = []
    reader.post("DAAA", "UOWNER", "perdona el retraso", ts(0.1))
    dms.rastro_tick(reader)
    fila = rastro("DAAA")
    assert fila["ultimo_de"] == "dueno"
    assert fila["ultimo_ts"] != viejo
    assert fila["avisado_ts"] is None


def test_the_same_last_message_keeps_the_alert_marked(reader):
    dm(reader, "DAAA", "USUNNY", horas=50)
    dms.rastro_tick(reader)
    db.execute("update dm_rastro set avisado_ts = ultimo_ts where dm_id = 'DAAA'")
    dms.rastro_tick(reader)
    fila = rastro("DAAA")
    assert fila["avisado_ts"] == fila["ultimo_ts"]


def test_retention_deletes_what_is_too_old(reader, restore_config):
    db.execute("update config set value = '7'::jsonb where key = 'seguimiento_retencion_dias'")
    dm(reader, "DVIEJO", "UOLD", horas=24 * 10)
    dm(reader, "DNUEVO", "USUNNY", horas=24 * 2)
    resultado = dms.rastro_tick(reader)
    # El viejo no entra siquiera.
    assert [f["dm_id"] for f in rastro()] == ["DNUEVO"]
    assert resultado.omitidos == 1
    # Y si estaba guardado de antes, la pasada lo borra.
    db.execute(
        "insert into dm_rastro (dm_id, slack_user_id, ultimo_ts, ultimo_de) "
        "values ('DANTIGUO', 'UOLD', %s, 'otro')",
        (ts(24 * 30),),
    )
    assert dms.limpiar_retencion() == 1
    assert [f["dm_id"] for f in rastro()] == ["DNUEVO"]


def test_with_the_switch_off_nothing_is_read_or_stored(reader, restore_config):
    db.execute("update config set value = 'false'::jsonb where key = 'seguimiento_activo'")
    dm(reader, "DAAA", "USUNNY")
    resultado = dms.rastro_tick(reader)
    assert (resultado.vistos, resultado.guardados) == (0, 0)
    assert rastro() == []
    # Ni una llamada a Slack.
    assert reader.last_message_calls == []


def test_a_failure_in_one_dm_does_not_stop_the_others(reader):
    dm(reader, "DAAA", "USUNNY")
    dm(reader, "DBBB", "UOTHER", nombre="Other")
    reader.unavailable_channels.add("DAAA")
    resultado = dms.rastro_tick(reader)
    assert [f["dm_id"] for f in rastro()] == ["DBBB"]
    assert resultado.errores and "DAAA" in resultado.errores[0]


def test_a_failure_listing_the_dms_is_reported_without_raising(reader):
    reader.dms_error = SlackUnavailable("conversations_list: ratelimited")
    resultado = dms.rastro_tick(reader)
    assert resultado.errores == ["conversations_list: conversations_list: ratelimited"]
    assert rastro() == []


def test_without_the_workspace_code_the_trace_is_still_stored(reader, monkeypatch):
    dm(reader, "DAAA", "USUNNY")
    monkeypatch.setattr(
        reader, "team_id", lambda: (_ for _ in ()).throw(SlackUnavailable("auth_test: fallo"))
    )
    resultado = dms.rastro_tick(reader)
    assert rastro("DAAA")["team_id"] is None
    assert resultado.guardados == 1
    assert any("team_id" in e for e in resultado.errores)


def test_the_database_refuses_a_trace_that_is_not_a_one_to_one_dm(conn):
    """La última red: aunque el código se equivocara, la tabla no admite un
    grupo ni un canal."""
    import psycopg

    for canal in ("C0CANAL", "GGRUPO", "", "d-minuscula"):
        with pytest.raises(psycopg.errors.CheckViolation), conn.cursor() as cur:
            cur.execute(
                "insert into dm_rastro (dm_id, slack_user_id, ultimo_ts, ultimo_de) "
                "values (%s, 'U1', '1.1', 'otro')",
                (canal,),
            )


def test_the_database_refuses_an_unknown_author(conn):
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation), conn.cursor() as cur:
        cur.execute(
            "insert into dm_rastro (dm_id, slack_user_id, ultimo_ts, ultimo_de) "
            "values ('DAAA', 'U1', '1.1', 'alguien')"
        )
