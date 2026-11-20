"""El aviso de miembro nuevo (0037).

Lo que cuidan estos tests:

1. Que solo se anuncie a quien es nuevo de verdad: nunca la primera foto de un canal,
   nunca quien ya estaba en otro canal o ya tiene ficha, nunca dos veces la misma persona.
2. Cuándo sale: al terminar el research, o tras la espera si el research no llega.
3. Los frenos de siempre: reserva antes de publicar, tope diario, descartados, bots.
4. Que tenga su propia forma, y que lo que venga de un perfil ajeno no haga daño.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from handoff_agent import db
from handoff_agent.ingest import loop, queue, watcher
from handoff_agent.signals import miembros
from tests.slack_fakes import FakeReader

NOW = 1_726_000_000.0
PERFIL = "https://www.linkedin.com/in/dana-reyes"


@pytest.fixture(autouse=True)
def slack(monkeypatch):
    monkeypatch.setenv("HANDOFF_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("HANDOFF_SLACK_CHANNEL_ID", "CHANDOFF")
    posted = []

    def post_card(blocks, text):
        posted.append({"json": json.dumps(blocks, ensure_ascii=False), "text": text})
        return "CHANDOFF", f"1700000000.{len(posted):06d}"

    monkeypatch.setattr(miembros.slack_writer, "post_card", post_card)
    return posted


@pytest.fixture
def reader():
    lector = FakeReader()
    lector.add_profile("UDANA", "Dana Reyes", title="COO at Northwind")
    return lector


def tick(reader, channels=("C1",)):
    return watcher.watch_tick(reader, list(channels), lookback_hours=1, now=lambda: NOW)


def entra(reader, user="UDANA", canal="C1", ya=("U1",)):
    """Alguien aparece en el padrón de un canal que ya tenía línea base."""
    reader.set_members(canal, *ya)
    tick(reader, (canal,))
    reader.set_members(canal, *ya, user)
    tick(reader, (canal,))


def research_hecho(user="UDANA"):
    """Su research terminó: ya no tiene tarea abierta."""
    db.execute(
        "update research_jobs set status = 'hecho', finished_at = now() where slack_user_id = %s",
        (user,),
    )


def ficha(user="UDANA", nombre="Dana Reyes", empresa="Northwind", dossier=None, **columnas):
    pid = db.fetch_one(
        "insert into prospects (slack_user_id, full_name, company_name) "
        "values (%s, %s, %s) returning id::text as id",
        (user, nombre, empresa),
    )["id"]
    for columna, valor in columnas.items():
        db.execute(f"update prospects set {columna} = %s where id = %s", (valor, pid))
    if dossier is not None:
        db.execute(
            "insert into dossiers (prospect_id, version, content, sources) "
            "values (%s, 1, %s, '[]'::jsonb)",
            (pid, json.dumps(dossier)),
        )
    return pid


def filas():
    return db.fetch_all("select * from miembros_nuevos order by created_at")


def mas_tarde(minutos):
    return datetime.now(UTC) + timedelta(minutes=minutos)


# ---------------------------------------------------------------------------------
# A quién se apunta
# ---------------------------------------------------------------------------------


def test_the_first_member_list_is_a_baseline_and_announces_nobody(conn, reader, slack):
    """La primera foto de un canal es línea base: ni research ni aviso para el padrón."""
    reader.set_members("C1", "U1", "U2", "UDANA")
    tick(reader)
    assert filas() == []
    assert miembros.publicar_pendientes(reader, now=mas_tarde(24 * 60)) == 0
    assert slack == []


def test_a_new_member_is_noted_but_not_announced_on_the_spot(conn, reader, slack):
    """Al detectar solo se apunta: el aviso sale después, desde el bucle."""
    entra(reader)
    (fila,) = filas()
    assert (fila["slack_user_id"], fila["channel_id"], fila["estado"]) == (
        "UDANA",
        "C1",
        "pendiente",
    )
    assert slack == []


def test_the_owner_is_never_a_new_member(conn, reader):
    entra(reader, user="UOWNER")
    assert filas() == []


def test_someone_with_a_research_already_open_is_noted_too(conn, reader):
    """Escribió antes de que se viera su alta: su tarea ya estaba abierta y `enqueue`
    devuelve False, pero es igual de nuevo."""
    queue.enqueue("UDANA", "mensaje")
    entra(reader)
    assert [f["slack_user_id"] for f in filas()] == ["UDANA"]


def test_joining_one_more_channel_does_not_make_someone_new(conn, reader):
    """Ya estaba en otro canal vigilado: ha entrado en un canal más, no en el club."""
    reader.set_members("C1", "U1", "UDANA")
    reader.set_members("C2", "U1")
    tick(reader, ("C1", "C2"))
    reader.set_members("C2", "U1", "UDANA")
    tick(reader, ("C1", "C2"))
    assert filas() == []


def test_someone_we_already_have_on_file_is_not_new(conn, reader):
    ficha()
    entra(reader)
    assert filas() == []


def test_joining_several_channels_at_once_is_one_new_member(conn, reader, slack):
    reader.set_members("C1", "U1")
    reader.set_members("C2", "U1")
    tick(reader, ("C1", "C2"))
    reader.set_members("C1", "U1", "UDANA")
    reader.set_members("C2", "U1", "UDANA")
    tick(reader, ("C1", "C2"))
    assert len(filas()) == 1
    research_hecho()
    assert miembros.publicar_pendientes(reader) == 1


def test_noting_never_raises(conn, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("base caída")

    monkeypatch.setattr(miembros.db, "fetch_one", boom)
    assert miembros.apuntar("UDANA", "C1") is False


def test_a_failure_noting_does_not_cost_the_research_or_the_snapshot(conn, reader, monkeypatch):
    """Apuntar es un extra del watcher: si falla, la persona se investiga igual."""
    monkeypatch.setattr(miembros, "_ya_estaba", lambda *a: 1 / 0)
    entra(reader)
    assert db.fetch_one("select count(*) as n from research_jobs")["n"] == 1
    assert db.fetch_one("select count(*) as n from member_snapshots")["n"] == 2


# ---------------------------------------------------------------------------------
# Cuándo sale
# ---------------------------------------------------------------------------------


def test_it_waits_while_the_research_is_still_open(conn, reader, slack):
    entra(reader)
    assert miembros.publicar_pendientes(reader) == 0
    assert slack == []
    assert filas()[0]["estado"] == "pendiente"


def test_it_goes_out_once_the_research_is_done_with_what_the_dossier_knows(conn, reader, slack):
    entra(reader)
    ficha(
        dossier={
            "persona": {"nombre": "Dana Reyes", "cargo": "COO", "fuente": PERFIL},
            "encaje_handoff": {"puntuacion": 2, "razon": "r"},
        }
    )
    research_hecho()
    assert miembros.publicar_pendientes(reader) == 1
    aviso = slack[0]["json"]
    assert "NEW MEMBER" in aviso and "*Dana Reyes* — COO, Northwind" in aviso
    assert "just joined C1" in aviso
    assert "Dossier: fit 2/3" in aviso
    assert f"<{PERFIL}|Open their profile>" in aviso
    assert slack[0]["text"] == "New member: Dana Reyes — welcome them"
    (fila,) = filas()
    assert fila["estado"] == "publicada" and fila["aviso_ts"] and fila["publicado_at"]


def test_a_research_waiting_for_its_retry_still_counts_as_open(conn, reader, slack):
    entra(reader)
    db.execute("update research_jobs set status = 'pendiente', attempts = 1, last_error = 'x'")
    assert miembros.publicar_pendientes(reader, now=mas_tarde(5)) == 0


def test_after_the_wait_it_goes_out_without_the_research(conn, reader, slack):
    """La bienvenida no puede depender de un research que puede no llegar: pasada la
    espera sale con el nombre del perfil de Slack y una búsqueda de LinkedIn."""
    entra(reader)
    assert miembros.publicar_pendientes(reader, now=mas_tarde(29)) == 0
    assert miembros.publicar_pendientes(reader, now=mas_tarde(31)) == 1
    aviso = slack[0]["json"]
    assert "*Dana Reyes* — Northwind" in aviso
    assert "Not researched yet" in aviso
    assert "keywords=Dana+Reyes+Northwind|Find them on LinkedIn" in aviso
    assert "not a confirmed profile" in aviso


def test_the_wait_is_configurable(conn, reader, slack, restore_config):
    db.execute("update config set value = '0'::jsonb where key = 'miembro_nuevo_espera_min'")
    entra(reader)
    assert miembros.publicar_pendientes(reader, now=mas_tarde(1)) == 1


def test_the_link_to_the_panel_needs_the_panel_url(conn, reader, slack, monkeypatch):
    monkeypatch.setenv("PANEL_URL", "https://panel.example.com")
    entra(reader)
    pid = ficha(dossier={"encaje_handoff": {"puntuacion": 1}})
    research_hecho()
    miembros.publicar_pendientes(reader)
    assert f"<https://panel.example.com/personas/{pid}|Open in panel>" in slack[0]["json"]


# ---------------------------------------------------------------------------------
# No repetirse, topes y frenos
# ---------------------------------------------------------------------------------


def test_a_member_is_only_announced_once(conn, reader, slack):
    entra(reader)
    research_hecho()
    assert miembros.publicar_pendientes(reader) == 1
    assert miembros.publicar_pendientes(reader) == 0
    # Se va del canal y vuelve: sigue siendo una fila, ya publicada.
    reader.set_members("C1", "U1")
    tick(reader)
    reader.set_members("C1", "U1", "UDANA")
    tick(reader)
    research_hecho()
    assert miembros.publicar_pendientes(reader) == 0
    assert len(slack) == 1


def test_the_row_is_reserved_before_posting(conn, reader, monkeypatch):
    """Otro worker que mire mientras se publica ya la ve como publicada."""
    entra(reader)
    research_hecho()
    visto = []

    def post_card(blocks, text):
        visto.append(filas()[0]["estado"])
        return "CHANDOFF", "1.1"

    monkeypatch.setattr(miembros.slack_writer, "post_card", post_card)
    miembros.publicar_pendientes(reader)
    assert visto == ["publicada"]


def test_a_failed_post_leaves_it_pending_for_the_next_pass(conn, reader, slack, monkeypatch):
    entra(reader)
    research_hecho()

    def boom(blocks, text):
        raise RuntimeError("slack caído")

    with monkeypatch.context() as m:
        m.setattr(miembros.slack_writer, "post_card", boom)
        assert miembros.publicar_pendientes(reader) == 0
    (fila,) = filas()
    assert fila["estado"] == "pendiente" and fila["publicado_at"] is None
    assert "RuntimeError" in fila["motivo"]
    assert (
        db.fetch_one(
            "select count(*) as n from agent_actions where action = 'miembro_nuevo_fallido'"
        )["n"]
        == 1
    )
    assert miembros.publicar_pendientes(reader) == 1
    assert len(slack) == 1


def test_the_daily_cap(conn, reader, slack, restore_config):
    db.execute("update config set value = '1'::jsonb where key = 'miembros_nuevos_por_dia'")
    reader.add_profile("UEVA", "Eva Sanz")
    entra(reader, user="UDANA")
    reader.set_members("C1", "U1", "UDANA", "UEVA")
    tick(reader)
    db.execute("update research_jobs set status = 'hecho'")
    assert miembros.publicar_pendientes(reader) == 1
    assert miembros.publicar_pendientes(reader) == 0
    assert [f["estado"] for f in filas()] == ["publicada", "pendiente"]


def test_a_cap_of_zero_switches_it_off(conn, reader, slack, restore_config):
    db.execute("update config set value = '0'::jsonb where key = 'miembros_nuevos_por_dia'")
    entra(reader)
    research_hecho()
    assert miembros.publicar_pendientes(reader) == 0
    assert slack == []


def test_someone_left_waiting_for_days_is_no_longer_new(conn, reader, slack):
    """Quien quedó sin avisar (tope alcanzado, bot sin configurar) no se anuncia como
    nuevo dos días después."""
    entra(reader)
    research_hecho()
    assert miembros.publicar_pendientes(reader, now=mas_tarde(49 * 60)) == 0
    (fila,) = filas()
    assert (fila["estado"], fila["motivo"]) == ("omitida", "caducado")


@pytest.mark.parametrize(
    ("columnas", "motivo"),
    [({"state": "descartado"}, "descartado"), ({"sin_tarjeta": True}, "modo de pruebas")],
)
def test_discarded_and_test_mode_people_are_never_announced(conn, reader, slack, columnas, motivo):
    entra(reader)
    ficha(**columnas)
    research_hecho()
    assert miembros.publicar_pendientes(reader) == 0
    assert (filas()[0]["estado"], filas()[0]["motivo"]) == ("omitida", motivo)


def test_a_bot_is_not_welcomed(conn, reader, slack):
    reader.add_profile("UBOT", "Deploy Bot", is_bot=True)
    entra(reader, user="UBOT")
    research_hecho("UBOT")
    assert miembros.publicar_pendientes(reader) == 0
    assert filas()[0]["estado"] == "omitida"


def test_without_the_bot_configured_nothing_is_published(conn, reader, slack, monkeypatch):
    monkeypatch.delenv("HANDOFF_SLACK_BOT_TOKEN", raising=False)
    entra(reader)
    research_hecho()
    assert miembros.publicar_pendientes(reader) == 0
    assert filas()[0]["estado"] == "pendiente"


def test_publishing_never_raises(conn, reader, monkeypatch):
    monkeypatch.setattr(miembros, "_publicar", lambda ahora, reader: 1 / 0)
    assert miembros.publicar_pendientes(reader) == 0


def test_it_is_audited_and_costs_nothing(conn, reader, slack):
    entra(reader)
    research_hecho()
    miembros.publicar_pendientes(reader)
    accion = db.fetch_one(
        "select payload from agent_actions where action = 'miembro_nuevo_publicado'"
    )
    assert accion["payload"] == {"canal": "C1", "con_dossier": False}
    assert db.fetch_one("select count(*) as n from llm_calls")["n"] == 0
    assert db.fetch_one("select count(*) as n from cost_events")["n"] == 0


# ---------------------------------------------------------------------------------
# Su propia forma, y nada ajeno que haga daño
# ---------------------------------------------------------------------------------


def test_it_has_its_own_header_and_shape(conn, reader, slack):
    """Ni tarjeta de research ni presentación: cabecera propia, sin cita y sin botones."""
    entra(reader)
    research_hecho()
    miembros.publicar_pendientes(reader)
    bloques = json.loads(slack[0]["json"])
    assert [b["type"] for b in bloques] == ["section", "context", "section", "section"]
    assert bloques[0]["text"]["text"].startswith("🆕 *NEW MEMBER · welcome them*")
    assert "Welcome them" in bloques[2]["text"]["text"]
    assert "Connect on LinkedIn" in bloques[3]["text"]["text"]
    for ajeno in ("SIGNAL", "NEW INTRODUCTION", "YOU WERE MENTIONED", "THEY ASKED"):
        assert ajeno not in slack[0]["json"]


def test_the_channel_goes_by_its_name_when_known(conn, reader, slack):
    db.execute("insert into canales (channel_id, nombre) values ('C1', 'founders-general')")
    entra(reader)
    research_hecho()
    miembros.publicar_pendientes(reader)
    assert "just joined #founders-general" in slack[0]["json"]


def test_a_hostile_profile_cannot_ping_the_channel(conn, reader, slack):
    reader.add_profile("UMALO", "<!channel> Eve", title="CEO at <@U999>|x")
    entra(reader, user="UMALO")
    research_hecho("UMALO")
    miembros.publicar_pendientes(reader)
    for vivo in ("<!channel>", "<@U999>"):
        assert vivo not in slack[0]["json"] and vivo not in slack[0]["text"]


def test_without_a_slack_profile_it_still_goes_out(conn, slack):
    """Slack no contesta por el perfil: sale con el código, y sin enlace de LinkedIn
    porque no hay nombre con el que buscar."""
    lector = FakeReader()
    entra(lector, user="UFANTASMA")
    research_hecho("UFANTASMA")
    assert miembros.publicar_pendientes(lector) == 1
    assert "*UFANTASMA*" in slack[0]["json"] and "LinkedIn" not in slack[0]["json"]


# ---------------------------------------------------------------------------------
# En el bucle del worker
# ---------------------------------------------------------------------------------


def test_the_worker_loop_publishes_them_with_the_reader(monkeypatch):
    visto = []
    monkeypatch.setattr(loop.miembros, "publicar_pendientes", lambda r: visto.append(r) or 1)
    loop._miembros_nuevos("lector")
    assert visto == ["lector"]


def test_a_failure_here_never_takes_the_worker_loop_down(monkeypatch):
    monkeypatch.setattr(loop.miembros, "publicar_pendientes", lambda r: 1 / 0)
    loop._miembros_nuevos("lector")
