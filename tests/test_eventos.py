"""Los dos avisos de canal que no cuestan una llamada al modelo (0030).

Lo que cuidan estos tests:

1. Que detecten lo que tienen que detectar y nada más: una mención es su id en el texto,
   una presentación es el canal configurado, y sin canal configurado no se avisa de
   ninguna presentación.
2. Que no cuesten nada: ni una llamada al modelo, ni un apunte de coste.
3. Que lo que escribió un tercero no pueda hacer daño en el aviso -- una URL de phishing
   en un mensaje no se convierte en enlace.
"""

import json
import time

import pytest

from handoff_agent import db
from handoff_agent.signals import eventos

DUENO = "UANTHONY"
CANAL = "C0INTROS"
OTRO_CANAL = "C0GENERAL"


@pytest.fixture(autouse=True)
def slack(monkeypatch):
    monkeypatch.setenv("HANDOFF_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("HANDOFF_SLACK_CHANNEL_ID", "CHANDOFF")
    posted = []

    def post_card(blocks, text):
        posted.append({"json": json.dumps(blocks, ensure_ascii=False), "text": text})
        return "CHANDOFF", f"1700000000.{len(posted):06d}"

    monkeypatch.setattr(eventos.slack_writer, "post_card", post_card)
    return posted


class FakeLector:
    def owner_id(self):
        return DUENO

    def permalink(self, channel, ts):
        return f"https://fake.slack.com/archives/{channel}/p{ts.replace('.', '')}"


@pytest.fixture
def lector():
    return FakeLector()


def mensaje(texto, canal=OTRO_CANAL, user="UMIKE", ts=None):
    ts = ts or f"{time.time():.6f}"
    db.execute(
        "insert into slack_messages (channel_id, ts, user_id, text) values (%s, %s, %s, %s)",
        (canal, ts, user, texto),
    )
    return ts


def persona(slack_user_id="UMIKE", nombre="Mike Black", estado="nuevo"):
    return db.fetch_one(
        "insert into prospects (slack_user_id, full_name, company_name, state) "
        "values (%s, %s, 'Acme', %s) returning id",
        (slack_user_id, nombre, estado),
    )["id"]


def con_canal_de_intros(conn):
    db.execute(
        "update config set value = %s::jsonb where key = 'canal_presentaciones'",
        (json.dumps(CANAL),),
    )


def filas(tipo=None):
    if tipo:
        return db.fetch_all("select * from eventos_canal where tipo = %s", (tipo,))
    return db.fetch_all("select * from eventos_canal")


# ---------------------------------------------------------------------------------
# Menciones
# ---------------------------------------------------------------------------------


def test_a_mention_is_detected(conn, lector, slack):
    persona()
    mensaje(f"hey <@{DUENO}> can you intro me to someone?")
    assert eventos.publicar_pendientes(lector) == 1
    assert "YOU WERE MENTIONED" in slack[0]["json"]
    assert "Mike Black" in slack[0]["json"]


def test_a_message_without_the_mention_is_not_an_event(conn, lector, slack):
    mensaje("hola a todos, buenos días")
    assert eventos.publicar_pendientes(lector) == 0
    assert filas() == []


def test_mentioning_someone_else_is_not_an_event(conn, lector, slack):
    mensaje("hey <@UOTRAPERSONA> mira esto")
    assert eventos.publicar_pendientes(lector) == 0


def test_the_owner_mentioning_himself_is_not_an_event(conn, lector, slack):
    """Sus propios mensajes no avisan: ni mencionarse ni presentarse en su club."""
    mensaje(f"recordatorio para mí <@{DUENO}>", user=DUENO)
    assert eventos.publicar_pendientes(lector) == 0


def test_an_ignored_message_is_not_an_event(conn, lector, slack):
    ts = mensaje(f"<@{DUENO}> mira", user="UBOT")
    db.execute("update slack_messages set status = 'ignorado' where ts = %s", (ts,))
    assert eventos.publicar_pendientes(lector) == 0


def test_a_discarded_person_never_triggers_an_event(conn, lector, slack):
    persona(estado="descartado")
    mensaje(f"<@{DUENO}> hola")
    assert eventos.publicar_pendientes(lector) == 0


def test_a_mention_from_someone_unknown_still_alerts(conn, lector, slack):
    """Quien menciona a AG puede no estar investigado todavía: se avisa igual."""
    mensaje(f"<@{DUENO}> hi there", user="UNUEVO")
    assert eventos.publicar_pendientes(lector) == 1
    assert "UNUEVO" in slack[0]["json"]


# ---------------------------------------------------------------------------------
# Presentaciones
# ---------------------------------------------------------------------------------


def test_without_a_configured_channel_there_are_no_introductions(conn, lector, slack):
    """No se adivina por el nombre: sin canal configurado, no se avisa de nada."""
    mensaje("Hi everyone, I'm new here!", canal=CANAL)
    assert eventos.publicar_pendientes(lector) == 0


def test_a_message_in_the_introductions_channel_is_an_introduction(
    conn, lector, slack, restore_config
):
    con_canal_de_intros(conn)
    persona()
    mensaje("Hi everyone, I run a logistics company", canal=CANAL)
    assert eventos.publicar_pendientes(lector) == 1
    assert "NEW INTRODUCTION" in slack[0]["json"]


def test_a_message_in_another_channel_is_not_an_introduction(conn, lector, slack, restore_config):
    con_canal_de_intros(conn)
    mensaje("Hi everyone", canal=OTRO_CANAL)
    assert eventos.publicar_pendientes(lector) == 0


def test_an_empty_message_is_not_an_introduction(conn, lector, slack, restore_config):
    con_canal_de_intros(conn)
    mensaje("   ", canal=CANAL)
    assert eventos.publicar_pendientes(lector) == 0


def test_one_message_can_be_both_things(conn, lector, slack, restore_config):
    """Alguien que se presenta y menciona a AG en la misma línea: dos avisos."""
    con_canal_de_intros(conn)
    persona()
    mensaje(f"Hi all, I'm Mike. <@{DUENO}> told me to join", canal=CANAL)
    assert eventos.publicar_pendientes(lector) == 2
    assert {f["tipo"] for f in filas()} == {eventos.MENCION, eventos.PRESENTACION}


# ---------------------------------------------------------------------------------
# No repetirse, topes y frenos
# ---------------------------------------------------------------------------------


def test_an_event_is_only_published_once(conn, lector, slack):
    persona()
    mensaje(f"<@{DUENO}> hola")
    assert eventos.publicar_pendientes(lector) == 1
    assert eventos.publicar_pendientes(lector) == 0
    assert len(slack) == 1


def test_the_daily_cap_applies_per_type(conn, lector, slack, restore_config):
    db.execute("update config set value = '1'::jsonb where key = 'menciones_por_dia'")
    mensaje(f"<@{DUENO}> uno", user="U1", ts=f"{time.time() - 20:.6f}")
    mensaje(f"<@{DUENO}> dos", user="U2", ts=f"{time.time() - 10:.6f}")
    assert eventos.publicar_pendientes(lector) == 1
    # La segunda sigue pendiente: mañana sale.
    assert len([f for f in filas(eventos.MENCION) if f["estado"] == "pendiente"]) == 1


def test_a_failed_post_leaves_the_event_pending(conn, lector, slack, monkeypatch):
    persona()
    mensaje(f"<@{DUENO}> hola")

    def boom(blocks, text):
        raise RuntimeError("slack caído")

    monkeypatch.setattr(eventos.slack_writer, "post_card", boom)
    assert eventos.publicar_pendientes(lector) == 0
    assert filas(eventos.MENCION)[0]["estado"] == "pendiente"
    # Y en la vuelta siguiente, con Slack de vuelta, sale.
    monkeypatch.undo()


def test_without_the_bot_configured_nothing_is_published(conn, lector, slack, monkeypatch):
    monkeypatch.delenv("HANDOFF_SLACK_BOT_TOKEN", raising=False)
    mensaje(f"<@{DUENO}> hola")
    assert eventos.publicar_pendientes(lector) == 0


def test_publishing_never_raises(conn, monkeypatch, slack):
    class RotoLector:
        def owner_id(self):
            raise RuntimeError("credencial muerta")

    assert eventos.publicar_pendientes(RotoLector()) == 0


# ---------------------------------------------------------------------------------
# Lo que escribió un tercero no puede hacer daño
# ---------------------------------------------------------------------------------


def test_a_url_in_the_message_is_not_turned_into_a_link(conn, lector, slack):
    """Una URL de phishing en el mensaje de alguien no puede salir clicable."""
    mensaje(f"<@{DUENO}> verify here: https://evil.co/verify")
    eventos.publicar_pendientes(lector)
    bloque = slack[0]["json"]
    # La cita va dentro de un código en línea: Slack no autoenlaza ahí.
    assert "`" in bloque
    assert "<https://evil.co/verify|" not in bloque


def test_an_event_costs_nothing(conn, lector, slack):
    """Ni una llamada al modelo: es la razón de que esto viva fuera de las señales."""
    persona()
    mensaje(f"<@{DUENO}> hola")
    eventos.publicar_pendientes(lector)
    assert db.fetch_one("select count(*) as n from llm_calls")["n"] == 0
    assert db.fetch_one("select count(*) as n from cost_events")["n"] == 0


# ---------------------------------------------------------------------------------
# Nada de histórico: el fallo que esto evita costó 40 mensajes en el canal.
# ---------------------------------------------------------------------------------


def test_an_old_message_is_never_announced(conn, lector, slack, restore_config):
    """`slack_messages` lleva meses de histórico de los canales vigilados. Sin el corte
    de edad, la primera pasada anuncia como recién llegadas decenas de presentaciones y
    menciones de hace semanas -- que es exactamente lo que pasó la primera vez."""
    con_canal_de_intros(conn)
    viejo = f"{time.time() - 40 * 86400:.6f}"
    mensaje("Hi everyone, I'm new!", canal=CANAL, ts=viejo)
    mensaje(f"<@{DUENO}> mira esto", ts=viejo)
    assert eventos.publicar_pendientes(lector) == 0
    assert filas() == []


def test_the_whole_history_of_the_introductions_channel_stays_quiet(
    conn, lector, slack, restore_config
):
    con_canal_de_intros(conn)
    for n in range(30):
        mensaje(f"intro {n}", canal=CANAL, user=f"U{n}", ts=f"{time.time() - (n + 2) * 86400:.6f}")
    assert eventos.publicar_pendientes(lector) == 0
    assert slack == []


def test_a_recent_message_still_gets_through(conn, lector, slack, restore_config):
    con_canal_de_intros(conn)
    mensaje("Hi, just joined", canal=CANAL, ts=f"{time.time() - 600:.6f}")
    assert eventos.publicar_pendientes(lector) == 1
