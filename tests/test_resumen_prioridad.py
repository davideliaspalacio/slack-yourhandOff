"""El resumen del día por prioridad (0039).

Lo que cuidan estos tests:

1. Que solo traiga lo pendiente de verdad: nadie ya marcado, nada ya contestado.
2. Que salga ordenado y agrupado por nivel, corto y con enlaces.
3. Que sea UN resumen al día: reserva antes de publicar, nunca junto a "FOLLOW-UPS",
   y nada si no hay nada pendiente.
4. Que lo que venga de un perfil ajeno no haga daño, y que nada de esto lance.
"""

import json
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
import pytest

from handoff_agent import db
from handoff_agent.delivery import prioridad, resumen_prioridad, seguimiento, slack_writer
from tests.slack_fakes import FakeReader
from tests.test_panel_rls import ALLOWED, STRANGER, as_user

OWNER = "UOWNER"
CANAL = "CGEN"
INTROS = "C0INTROS"
NY = ZoneInfo("America/New_York")
CLAVES = (
    "prioridad_activa",
    "resumen_prioridad_activo",
    "resumen_prioridad_hora",
    "resumen_prioridad_max_por_nivel",
    "resumen_prioridad_dias",
)


@pytest.fixture(autouse=True)
def slack(monkeypatch):
    """Bot configurado y `post_card` sustituido: lo publicado queda en la lista. El
    enlace a una tarjeta (`chat.getPermalink`) tampoco sale a la red."""
    monkeypatch.setenv("HANDOFF_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("HANDOFF_SLACK_CHANNEL_ID", "CHANDOFF")
    posted = []

    def post_card(blocks, text):
        posted.append(
            {"blocks": blocks, "text": text, "json": json.dumps(blocks, ensure_ascii=False)}
        )
        return "CHANDOFF", f"1700000000.{len(posted):06d}"

    monkeypatch.setattr(slack_writer, "post_card", post_card)
    monkeypatch.setattr(
        slack_writer,
        "card_permalink",
        lambda canal, ts: f"https://handoff.slack.com/archives/{canal}/p{ts.replace('.', '')}",
    )
    return posted


def ajuste(clave, valor):
    db.execute("update config set value = %s::jsonb where key = %s", (json.dumps(valor), clave))


def valor(clave):
    return db.fetch_one("select value from config where key = %s", (clave,))["value"]


@pytest.fixture
def listo(conn, restore_config):
    """A cualquier hora del día toca resumen: la hora tiene sus propios tests."""
    ajuste("resumen_prioridad_hora", 0)
    return conn


def ts(horas: float) -> str:
    return f"{time.time() - horas * 3600:.6f}"


def persona(user="UADA", nombre="Ada Ruiz", empresa="Acme", estado="investigado", nota=None):
    pid = db.fetch_one(
        "insert into prospects (slack_user_id, full_name, company_name, state) "
        "values (%s, %s, %s, %s) returning id::text as id",
        (user, nombre, empresa, estado),
    )["id"]
    if nota is not None:
        dossier(pid, nota)
    return pid


def dossier(pid, nota, version=1):
    db.execute(
        "insert into dossiers (prospect_id, version, content, sources) "
        "values (%s, %s, %s, '[]'::jsonb)",
        (pid, version, json.dumps({"encaje_handoff": {"puntuacion": nota, "razon": "r"}})),
    )


def tarjeta(pid, banda="alta", dias=0.0, version=1, message_ts="1699999999.000100"):
    """La tarjeta de research que `deliver.py` publicó para esa versión del dossier."""
    db.execute(
        "insert into deliveries (prospect_id, kind, band, dossier_version, channel_id, "
        "message_ts, created_at) values (%s, 'slack', %s, %s, 'CHANDOFF', %s, "
        "now() - make_interval(secs => %s))",
        (pid, banda, version, message_ts, dias * 86400),
    )


def con_tarjeta(user="UADA", nombre="Ada Ruiz", banda="alta", dias=0.0, **kwargs):
    pid = persona(user, nombre, nota=3 if banda == "alta" else 2, **kwargs)
    tarjeta(pid, banda, dias)
    return pid


def pidio(user="UADA", tipo="soporte", confianza="alta", dias=0.0):
    """Un "they asked" ya publicado de esa persona."""
    marca = ts(dias * 24 + 0.5)
    db.execute(
        "insert into slack_messages (channel_id, ts, user_id, text) "
        "values ('C1', %s, %s, 'we need help')",
        (marca, user),
    )
    db.execute(
        "insert into message_signals (channel_id, ts, detectada, tipo, cita, confianza, "
        "alerta_estado, alerta_channel_id, alerta_ts, alerta_at) "
        "values ('C1', %s, true, %s, 'we need help', %s, 'publicada', 'CHANDOFF', "
        "'1699999999.000200', now() - make_interval(secs => %s))",
        (marca, tipo, confianza, dias * 86400),
    )


def dm(dm_id, pid, user, de="otro", horas=30.0):
    db.execute(
        "insert into dm_rastro (dm_id, team_id, slack_user_id, prospect_id, ultimo_ts, ultimo_de) "
        "values (%s, 'T0OWNER', %s, %s, %s, %s)",
        (dm_id, user, pid, ts(horas), de),
    )


def presentacion(reader, user="UDANA", horas=5.0, en_slack=True, **extra):
    """Alguien se presentó y `signals/eventos.py` ya avisó de ello."""
    marca = ts(horas)
    db.execute(
        "insert into slack_messages (channel_id, ts, user_id, text) values (%s, %s, %s, 'Hi all')",
        (INTROS, marca, user),
    )
    db.execute(
        "insert into eventos_canal (channel_id, ts, tipo, slack_user_id, estado, aviso_ts) "
        "values (%s, %s, 'presentacion', %s, 'publicada', '1.1')",
        (INTROS, marca, user),
    )
    if en_slack:
        reader.post(INTROS, user, "Hi all", marca, **extra)
    return marca


@pytest.fixture
def reader(listo):
    lector = FakeReader(owner=OWNER)
    lector.add_profile("UDANA", "Dana Reyes")
    return lector


def fila(dia=None):
    filas = db.fetch_all("select * from resumenes_prioridad order by dia")
    return filas[-1] if filas else None


def acciones(nombre):
    return db.fetch_all("select * from agent_actions where action = %s", (nombre,))


def textos(publicado):
    return [
        b["text"]["text"] if b["type"] == "section" else b["elements"][0]["text"]
        for b in publicado["blocks"]
    ]


# ---------------------------------------------------------------------------------
# Lo que deja la migración
# ---------------------------------------------------------------------------------


def test_the_defaults_of_the_migration(conn):
    assert valor("prioridad_activa") is True
    assert valor("resumen_prioridad_activo") is True
    assert valor("resumen_prioridad_hora") == resumen_prioridad.HORA == 8
    assert valor("resumen_prioridad_max_por_nivel") == resumen_prioridad.MAX_POR_NIVEL == 5
    assert valor("resumen_prioridad_dias") == resumen_prioridad.DIAS == 7


# ---------------------------------------------------------------------------------
# Cómo se ve
# ---------------------------------------------------------------------------------


def test_one_message_grouped_by_level_most_urgent_first(listo, slack, monkeypatch):
    monkeypatch.setenv("PANEL_URL", "https://panel.example.com")
    ada = con_tarjeta("UADA", "Ada Ruiz", "alta")
    bob = con_tarjeta("UBOB", "Bob Lee", "media", empresa="Beta")
    cleo = persona("UCLEO", "Cleo Diaz", "Gamma", estado="nuevo")
    dm("DCLEO", cleo, "UCLEO", de="dueno", horas=24 * 4)

    assert resumen_prioridad.publicar() == 3
    (resumen,) = slack
    assert textos(resumen) == [
        "🧭 *DAILY BRIEF · what to handle first*",
        (
            "🔴 *ACT NOW* — a likely customer is waiting (1)\n"
            "• *Ada Ruiz* — Acme · high signal, fit 3/3 · today · "
            "<https://handoff.slack.com/archives/CHANDOFF/p1699999999000100|Open card> · "
            f"<https://panel.example.com/personas/{ada}|Open in panel>"
        ),
        (
            "🟡 *TODAY* — worth a few minutes before the day ends (1)\n"
            "• *Bob Lee* — Beta · medium signal, fit 2/3 · today · "
            "<https://handoff.slack.com/archives/CHANDOFF/p1699999999000100|Open card> · "
            f"<https://panel.example.com/personas/{bob}|Open in panel>"
        ),
        (
            "⚪ *FYI* — good to know, nothing waiting on you (1)\n"
            "• *Cleo Diaz* — Gamma · you wrote 4 days ago, no answer yet · "
            "<https://app.slack.com/client/T0OWNER/DCLEO|Open in Slack>"
        ),
        (
            "People drop off this list once they are marked Contacted or Discard. "
            "<https://panel.example.com/|Open the panel> to see everything."
        ),
    ]
    assert resumen["text"] == "Daily brief: 1 act now · 1 today · 1 FYI"


def test_a_level_with_nothing_in_it_is_not_shown(listo, slack):
    con_tarjeta(banda="media")
    resumen_prioridad.publicar()
    assert "ACT NOW" not in slack[0]["json"] and "FYI" not in slack[0]["json"]
    assert slack[0]["text"] == "Daily brief: 1 today"


def test_inside_a_level_who_waits_on_him_comes_before_who_asked_and_then_the_cards(listo, slack):
    con_tarjeta("UCARD", "Carla Card", "alta")
    persona("UASK", "Asha Ask", nota=1)
    pidio("UASK")
    hot = persona("UHOT", "Hana Hot", nota=3)
    dm("DHOT", hot, "UHOT")
    resumen_prioridad.publicar()
    ahora = textos(slack[0])[1]
    assert ahora.index("Hana Hot") < ahora.index("Asha Ask") < ahora.index("Carla Card")
    assert "• *Asha Ask* — Acme · they asked: support · today" in ahora
    assert "• *Hana Hot* — Acme · Hana replied 1 day ago" in ahora


def test_a_person_with_a_card_who_also_asked_is_one_line_at_the_higher_level(listo, slack):
    con_tarjeta(banda="media")
    pidio(tipo="ventas", confianza="alta")
    assert resumen_prioridad.publicar() == 1
    linea = textos(slack[0])[1]
    assert linea.startswith("🔴 *ACT NOW*")
    assert linea.count("Ada Ruiz") == 1
    assert "they asked: sales · medium signal, fit 2/3" in linea
    # El enlace es el de su tarjeta de research, que es la que lleva los botones.
    assert "p1699999999000100|Open card>" in linea


def test_a_doubtful_ask_is_for_today(listo, slack):
    persona(nota=1)
    pidio(confianza="media")
    resumen_prioridad.publicar()
    assert textos(slack[0])[1].startswith("🟡 *TODAY*")
    assert "p1699999999000200|Open card>" in slack[0]["json"]


def test_each_level_is_capped_and_the_rest_is_counted(listo, slack):
    ajuste("resumen_prioridad_max_por_nivel", 2)
    for i in range(5):
        con_tarjeta(f"U{i}", f"Persona {i}", "alta", dias=i)
    con_tarjeta("UMED", "Mia Media", "media")
    assert resumen_prioridad.publicar() == 3
    ahora = textos(slack[0])[1].splitlines()
    # Las dos más recientes, y las otras tres solo contadas.
    assert ahora[0] == "🔴 *ACT NOW* — a likely customer is waiting (5)"
    assert [linea.split("*")[1] for linea in ahora[1:3]] == ["Persona 0", "Persona 1"]
    assert ahora[3] == "_+3 more not shown_"
    assert slack[0]["text"] == "Daily brief: 5 act now · 1 today"


def test_a_name_from_slack_cannot_break_the_block(listo, slack):
    con_tarjeta("UX", "<!channel> *Ada* & co", empresa="Acme <https://evil|click>")
    resumen_prioridad.publicar()
    assert "<!channel>" not in slack[0]["json"] and "<https://evil" not in slack[0]["json"]
    assert "&lt;!channel&gt;" in slack[0]["json"]


def test_many_long_lines_never_go_over_the_block_limit(listo, slack, monkeypatch):
    monkeypatch.setenv("PANEL_URL", "https://panel.example.com")
    ajuste("resumen_prioridad_max_por_nivel", 20)
    for i in range(20):
        con_tarjeta(f"U{i}", "&" * 200, "alta", empresa="&" * 200)
    assert resumen_prioridad.publicar() == 20
    bloques = slack[0]["blocks"]
    assert len(bloques) > 4 and len(bloques) < 50
    assert all(len(b["text"]["text"]) <= 3000 for b in bloques if b["type"] == "section")
    assert slack[0]["json"].count("Open card") == 20


def test_a_link_that_slack_would_not_take_is_left_out(listo, slack, monkeypatch):
    monkeypatch.setattr(slack_writer, "card_permalink", lambda c, t: "https://x.co/a|<!here>")
    con_tarjeta()
    assert resumen_prioridad.publicar() == 1
    assert "Open card" not in slack[0]["json"] and "<!here>" not in slack[0]["json"]


def test_without_a_card_link_the_line_still_goes_out(listo, slack, monkeypatch):
    def boom(canal, ts):
        raise RuntimeError("slack caído")

    monkeypatch.setattr(slack_writer, "card_permalink", boom)
    con_tarjeta()
    assert resumen_prioridad.publicar() == 1
    assert "*Ada Ruiz* — Acme · high signal, fit 3/3 · today" in slack[0]["json"]


# ---------------------------------------------------------------------------------
# Solo lo pendiente de verdad
# ---------------------------------------------------------------------------------


@pytest.mark.parametrize("estado", ["contactado", "descartado", "cliente"])
def test_someone_already_marked_is_not_in_the_brief(listo, slack, estado):
    pid = con_tarjeta()
    pidio()
    db.execute("update prospects set state = %s, updated_at = now() where id = %s", (estado, pid))
    assert resumen_prioridad.publicar() == 0
    assert slack == []


def test_a_test_mode_person_is_not_in_the_brief(listo, slack):
    pid = con_tarjeta()
    db.execute("update prospects set sin_tarjeta = true where id = %s", (pid,))
    assert resumen_prioridad.publicar() == 0


def test_a_card_older_than_the_window_is_no_longer_brought_up(listo, slack):
    con_tarjeta("UOLD", "Olga Old", dias=8)
    con_tarjeta("UNEW", "Nico New", dias=6)
    assert resumen_prioridad.publicar() == 1
    assert "Nico New" in slack[0]["json"] and "Olga Old" not in slack[0]["json"]
    assert "6 days ago" in slack[0]["json"]


def test_the_window_comes_from_config(listo, slack):
    ajuste("resumen_prioridad_dias", 2)
    con_tarjeta(dias=3)
    pidio(dias=3)
    assert resumen_prioridad.publicar() == 0


def test_a_card_from_an_older_dossier_does_not_count(listo, slack):
    """Se la volvió a investigar y el dossier nuevo ya no da para tarjeta."""
    pid = con_tarjeta()
    dossier(pid, 1, version=2)
    assert resumen_prioridad.publicar() == 0


def test_a_card_that_never_reached_slack_is_not_listed(listo, slack):
    pid = persona(nota=3)
    db.execute(
        "insert into deliveries (prospect_id, kind, band, dossier_version) "
        "values (%s, 'slack', 'alta', 1)",
        (pid,),
    )
    assert resumen_prioridad.publicar() == 0


def test_an_ask_from_someone_without_a_file_is_not_listed(listo, slack):
    """Sin ficha no hay botón con el que quitarla de la lista."""
    pidio("UNADIE")
    assert resumen_prioridad.publicar() == 0


def test_an_answered_conversation_is_not_in_the_brief(listo, slack):
    pid = persona(nota=3)
    dm("DAAA", pid, "UADA", de="otro", horas=30)
    db.execute("update dm_rastro set respondido_con_emoji = true")
    assert resumen_prioridad.publicar() == 0


def escribe(reader, user, horas, hilo=None, texto="hola", **extra):
    marca = ts(horas)
    db.execute(
        "insert into slack_messages (channel_id, ts, user_id, text, thread_ts) "
        "values (%s, %s, %s, %s, %s)",
        (CANAL, marca, user, texto, hilo),
    )
    if hilo:
        reader.reply(CANAL, hilo, user, texto, marca, **extra)
    else:
        reader.post(CANAL, user, texto, marca, **extra)
    return marca


def menciona(reader, user, horas):
    marca = escribe(reader, user, horas, texto=f"<@{OWNER}> ping")
    db.execute(
        "insert into eventos_canal (channel_id, ts, tipo, slack_user_id, estado) "
        "values (%s, %s, 'mencion', %s, 'publicada')",
        (CANAL, marca, user),
    )
    return marca


@pytest.fixture
def canales(reader):
    """Los recordatorios de canal encendidos hace un mes (ver `test_seguimiento_canal`)."""
    ajuste("leer_hilos", True)
    db.execute(
        "update config set updated_at = now() - interval '30 days' "
        "where key in ('seguimiento_hilos_activo', 'seguimiento_menciones_activo', 'leer_hilos')"
    )
    db.execute("insert into canales (channel_id, nombre) values (%s, 'founders-general')", (CANAL,))
    return reader


def test_an_unanswered_mention_is_there_and_an_answered_one_is_not(canales, slack):
    persona("UHOT", "Hana Hot", nota=3)
    persona("UCOLD", "Cora Cold", estado="nuevo")
    persona("UDONE", "Dora Done", nota=3)
    menciona(canales, "UHOT", 6)
    menciona(canales, "UCOLD", 5)
    hecha = menciona(canales, "UDONE", 7)
    # La base aún no lo sabe, pero en Slack ya reaccionó a esa mención.
    for mensaje in canales.messages[CANAL]:
        if mensaje["ts"] == hecha:
            mensaje["reactions"] = [{"name": "thumbsup", "users": [OWNER]}]

    assert resumen_prioridad.publicar(reader=canales) == 2
    ahora, hoy = textos(slack[0])[1:3]
    assert ahora.startswith("🔴 *ACT NOW*") and "Hana mentioned you in #founders-general" in ahora
    assert hoy.startswith("🟡 *TODAY*") and "Cora mentioned you in #founders-general" in hoy
    assert "Dora" not in slack[0]["json"]
    assert "|Open in Slack>" in ahora


def test_without_the_reader_the_channel_cases_stay_out(canales, slack):
    persona("UHOT", "Hana Hot", nota=3)
    menciona(canales, "UHOT", 6)
    assert resumen_prioridad.publicar() == 0


# ---------------------------------------------------------------------------------
# Presentaciones sin saludar
# ---------------------------------------------------------------------------------


def test_an_introduction_nobody_greeted_is_for_today(reader, slack):
    marca = presentacion(reader)
    assert resumen_prioridad.publicar(reader=reader) == 1
    assert textos(slack[0])[1] == (
        "🟡 *TODAY* — worth a few minutes before the day ends (1)\n"
        "• *Dana Reyes* · introduced themselves 5 hours ago, no hello from you yet · "
        f"<https://fake.slack.com/archives/{INTROS}/p{marca.replace('.', '')}|Say hi in Slack>"
    )


def test_an_introduction_he_reacted_to_is_done(reader, slack):
    presentacion(reader, reactions=[{"name": "wave", "users": [OWNER]}])
    assert resumen_prioridad.publicar(reader=reader) == 0


def test_an_introduction_he_replied_to_is_done(reader, slack):
    presentacion(reader, reply_users=[OWNER], reply_users_count=1)
    assert resumen_prioridad.publicar(reader=reader) == 0


def test_with_threads_on_a_reply_in_the_intro_thread_is_a_hello(reader, slack):
    ajuste("leer_hilos", True)
    marca = presentacion(reader)
    reader.reply(INTROS, marca, OWNER, "Welcome Dana!", ts(1))
    assert resumen_prioridad.publicar(reader=reader) == 0


def test_when_slack_cannot_tell_the_introduction_is_not_brought_up(reader, slack):
    """Ante la duda, no se recuerda."""
    presentacion(reader)
    reader.unavailable_channels.add(INTROS)
    assert resumen_prioridad.publicar(reader=reader) == 0


def test_an_old_introduction_or_one_without_reader_or_name_stays_out(reader, slack):
    presentacion(reader, "UDANA", horas=50)
    presentacion(reader, "USINNOMBRE", horas=3)
    assert resumen_prioridad.publicar(reader=reader) == 0
    presentacion(reader, "UDANA", horas=4)
    assert resumen_prioridad.pendientes() == []


def test_an_introduction_from_someone_already_contacted_stays_out(reader, slack):
    persona("UDANA", "Dana Reyes", estado="contactado")
    presentacion(reader)
    assert resumen_prioridad.publicar(reader=reader) == 0


# ---------------------------------------------------------------------------------
# La credencial del lector
# ---------------------------------------------------------------------------------


def latido(estado):
    db.execute(
        "insert into config (key, value) values ('lector_estado', %s::jsonb) "
        "on conflict (key) do update set value = excluded.value",
        (json.dumps(estado),),
    )


def test_an_expired_credential_is_the_first_line(listo, slack, monkeypatch):
    monkeypatch.setenv("PANEL_URL", "https://panel.example.com")
    latido({"ok": False, "en": "2026-11-20T10:00:00+00:00", "error": "SlackAuthFailed"})
    con_tarjeta()
    assert resumen_prioridad.publicar() == 2
    ahora = textos(slack[0])[1].splitlines()
    assert ahora[1] == (
        "• ⚠️ I can no longer read Slack — the reader credential expired. "
        "Paste a fresh one in the panel → https://panel.example.com/ajustes"
    )
    assert "Ada Ruiz" in ahora[2]


@pytest.mark.parametrize(
    "estado",
    [
        {"ok": True, "en": "x"},
        {"ok": False, "en": "x", "error": "SlackUnavailable"},
        {},
    ],
)
def test_a_working_reader_or_a_passing_failure_is_not_a_line(listo, slack, estado):
    latido(estado)
    assert resumen_prioridad.publicar() == 0


# ---------------------------------------------------------------------------------
# Absorbe "FOLLOW-UPS": un solo resumen al día
# ---------------------------------------------------------------------------------


def test_the_follow_up_cases_are_marked_exactly_as_follow_ups_would(listo, slack):
    sunny = persona("USUNNY", "Sunny Patel", estado="nuevo")
    dm("DAAA", sunny, "USUNNY", de="otro", horas=30)
    cleo = persona("UCLEO", "Cleo Diaz", estado="contactado")
    db.execute("update prospects set updated_at = now() - interval '9 days' where id = %s", (cleo,))
    assert resumen_prioridad.publicar() == 2
    assert "Sunny replied 1 day ago" in slack[0]["json"]
    assert "marked contacted 9 days ago, no conversation in Slack" in slack[0]["json"]
    rastro = db.fetch_one("select avisado_ts, ultimo_ts from dm_rastro where dm_id = 'DAAA'")
    assert rastro["avisado_ts"] == rastro["ultimo_ts"]
    (avisado,) = acciones(seguimiento.ACCION_AVISADO)
    assert str(avisado["prospect_id"]) == cleo
    # Y "FOLLOW-UPS" no ha salido: no hay ningún resumen de seguimiento anotado.
    assert acciones(seguimiento.ACCION_RESUMEN) == []
    assert "FOLLOW-UPS" not in slack[0]["json"]


def test_a_case_cut_by_the_cap_is_not_marked(listo, slack):
    ajuste("resumen_prioridad_max_por_nivel", 1)
    dm("DAAA", persona("UA", "Ana Uno", estado="nuevo"), "UA", horas=40)
    dm("DBBB", persona("UB", "Bea Dos", estado="nuevo"), "UB", horas=30)
    assert resumen_prioridad.publicar() == 1
    marcados = db.fetch_all("select dm_id from dm_rastro where avisado_ts = ultimo_ts")
    assert [m["dm_id"] for m in marcados] == ["DAAA"]


def test_with_follow_ups_switched_off_their_cases_stay_out(listo, slack):
    ajuste("seguimiento_activo", False)
    dm("DAAA", persona(estado="nuevo"), "UADA")
    assert resumen_prioridad.publicar() == 0


def test_if_follow_ups_already_went_out_today_the_brief_waits_for_tomorrow(listo, slack):
    """El día en que se enciende: nunca dos resúmenes el mismo día."""
    dm("DAAA", persona(estado="nuevo"), "UADA")
    assert seguimiento.publicar_resumen() == 1
    con_tarjeta("UBOB", "Bob Lee")
    assert resumen_prioridad.publicar() == 0
    assert len(slack) == 1
    assert fila() is None


# ---------------------------------------------------------------------------------
# Una vez al día, a su hora, reservando antes
# ---------------------------------------------------------------------------------


def a_las(hora, minuto=0):
    return datetime.now(NY).replace(hour=hora, minute=minuto, second=0, microsecond=0)


def test_it_waits_for_its_hour_in_the_configured_time_zone(conn, slack, restore_config):
    con_tarjeta()
    assert resumen_prioridad.publicar(now=a_las(7, 59)) == 0
    assert slack == [] and fila() is None
    assert resumen_prioridad.publicar(now=a_las(8).astimezone(UTC)) == 1
    assert fila()["dia"] == a_las(8).date()


def test_the_hour_comes_from_config(conn, slack, restore_config):
    ajuste("resumen_prioridad_hora", 15)
    con_tarjeta()
    assert resumen_prioridad.publicar(now=a_las(14, 59)) == 0
    assert resumen_prioridad.publicar(now=a_las(15)) == 1


def test_the_day_is_the_one_in_the_time_zone_not_utc(conn, slack, restore_config):
    """Las 23:30 de Nueva York ya son el día siguiente en UTC: el resumen es del de allí."""
    ajuste("resumen_prioridad_hora", 0)
    con_tarjeta()
    tarde = a_las(23, 30)
    assert resumen_prioridad.publicar(now=tarde.astimezone(UTC)) == 1
    assert fila()["dia"] == tarde.date()
    assert resumen_prioridad.publicar(now=tarde + timedelta(minutes=10)) == 0


def test_only_one_brief_a_day_and_another_the_next(listo, slack):
    con_tarjeta()
    ahora = a_las(9)
    assert resumen_prioridad.publicar(now=ahora) == 1
    assert resumen_prioridad.publicar(now=ahora + timedelta(hours=3)) == 0
    assert len(slack) == 1
    # Mañana sigue sin tocar: vuelve a salir.
    assert resumen_prioridad.publicar(now=ahora + timedelta(days=1)) == 1
    assert len(slack) == 2


def test_what_went_out_is_written_down(listo, slack):
    con_tarjeta("UADA", "Ada Ruiz", "alta")
    con_tarjeta("UBOB", "Bob Lee", "media")
    resumen_prioridad.publicar()
    f = fila()
    assert (f["estado"], f["ahora"], f["hoy"], f["fyi"]) == ("publicado", 1, 1, 0)
    assert (f["channel_id"], f["message_ts"]) == ("CHANDOFF", "1700000000.000001")
    (accion,) = acciones(resumen_prioridad.ACCION_PUBLICADO)
    assert accion["payload"] == {
        "dia": str(f["dia"]),
        "ahora": 1,
        "hoy": 1,
        "fyi": 0,
        "canal": "CHANDOFF",
        "ts": "1700000000.000001",
    }


def test_the_day_is_reserved_before_publishing(listo, slack, monkeypatch):
    con_tarjeta()
    visto = {}

    def post_card(blocks, text):
        visto["estado"] = fila()["estado"]
        return "CHANDOFF", "1.1"

    monkeypatch.setattr(slack_writer, "post_card", post_card)
    assert resumen_prioridad.publicar() == 1
    assert visto["estado"] == "reservado"
    assert fila()["estado"] == "publicado"


def test_a_day_another_worker_already_holds_is_not_published_again(listo, slack):
    con_tarjeta()
    hoy = datetime.now(NY).date()
    db.execute("insert into resumenes_prioridad (dia, estado) values (%s, 'reservado')", (hoy,))
    assert resumen_prioridad.publicar() == 0
    assert resumen_prioridad._reservar(hoy) is False
    assert slack == []


def test_with_nothing_pending_nothing_is_posted_and_the_day_is_closed(listo, slack):
    ahora = a_las(9)
    assert resumen_prioridad.publicar(now=ahora) == 0
    assert slack == []
    assert fila()["estado"] == "vacio"
    # Lo que llegue después ya tuvo su aviso inmediato: sale mañana si sigue sin tocar.
    con_tarjeta()
    assert resumen_prioridad.publicar(now=ahora + timedelta(hours=2)) == 0
    assert resumen_prioridad.publicar(now=ahora + timedelta(days=1)) == 1


def test_a_slack_failure_is_retried_a_few_times_and_never_marks_anything(listo, slack, monkeypatch):
    dm("DAAA", persona(estado="nuevo"), "UADA")

    def boom(blocks, text):
        raise RuntimeError("slack caído")

    real = slack_writer.post_card
    monkeypatch.setattr(slack_writer, "post_card", boom)
    for intento in (1, 2, 3):
        assert resumen_prioridad.publicar() == 0
        assert (fila()["estado"], fila()["intentos"]) == ("fallido", intento)
    # Agotados los intentos, hoy ya no se insiste aunque Slack vuelva.
    monkeypatch.setattr(slack_writer, "post_card", real)
    assert resumen_prioridad.publicar() == 0
    assert slack == []
    assert len(acciones(resumen_prioridad.ACCION_FALLIDO)) == 3
    assert db.fetch_one("select avisado_ts from dm_rastro")["avisado_ts"] is None


def test_after_a_slack_failure_the_next_pass_publishes(listo, slack, monkeypatch):
    con_tarjeta()
    real = slack_writer.post_card

    def boom(blocks, text):
        raise RuntimeError("slack caído")

    monkeypatch.setattr(slack_writer, "post_card", boom)
    assert resumen_prioridad.publicar() == 0
    monkeypatch.setattr(slack_writer, "post_card", real)
    assert resumen_prioridad.publicar() == 1
    assert (fila()["estado"], fila()["intentos"]) == ("publicado", 2)
    assert len(slack) == 1


# ---------------------------------------------------------------------------------
# Los frenos
# ---------------------------------------------------------------------------------


def test_with_the_switch_off_it_does_nothing(listo, slack):
    ajuste("resumen_prioridad_activo", False)
    con_tarjeta()
    assert resumen_prioridad.activo() is False
    assert resumen_prioridad.publicar() == 0
    assert slack == [] and fila() is None


def test_the_kill_switch_stops_it_and_leaves_one_trace_a_day(listo, slack):
    ajuste("kill_switch", True)
    con_tarjeta()
    assert resumen_prioridad.publicar() == 0
    assert resumen_prioridad.publicar() == 0
    assert slack == [] and fila() is None
    assert len(acciones(resumen_prioridad.ACCION_PARADA)) == 1
    # Al reactivar, sale: el día no se había gastado.
    ajuste("kill_switch", False)
    assert resumen_prioridad.publicar() == 1


def test_without_a_bot_token_nothing_is_published_and_the_day_is_kept(listo, slack, monkeypatch):
    con_tarjeta()
    monkeypatch.delenv("HANDOFF_SLACK_BOT_TOKEN")
    assert resumen_prioridad.publicar() == 0
    assert fila() is None
    monkeypatch.setenv("HANDOFF_SLACK_BOT_TOKEN", "xoxb-test")
    assert resumen_prioridad.publicar() == 1


def test_an_unexpected_failure_never_escapes(listo, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("config caída")

    monkeypatch.setattr(resumen_prioridad, "activo", boom)
    assert resumen_prioridad.publicar() == 0


def test_one_broken_source_does_not_take_the_others_with_it(listo, slack, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("seguimiento roto")

    monkeypatch.setattr(seguimiento, "casos", boom)
    con_tarjeta()
    assert resumen_prioridad.publicar() == 1
    assert "Ada Ruiz" in slack[0]["json"]


def test_it_costs_nothing_and_never_asks_a_model(listo, slack, monkeypatch):
    from handoff_agent import llm

    def nunca(*a, **k):
        raise AssertionError("el resumen no llama al modelo")

    monkeypatch.setattr(llm, "complete", nunca)
    con_tarjeta()
    assert resumen_prioridad.publicar() == 1
    assert db.fetch_one("select count(*) as n from llm_calls")["n"] == 0
    assert db.fetch_one("select count(*) as n from cost_events")["n"] == 0


def test_the_brief_keeps_its_levels_even_with_the_alert_labels_off(listo, slack):
    """`prioridad_activa` apaga la marca de cada aviso; el resumen ES la prioridad."""
    ajuste("prioridad_activa", False)
    con_tarjeta()
    resumen_prioridad.publicar()
    assert prioridad.marca(prioridad.AHORA) in slack[0]["json"]


# ---------------------------------------------------------------------------------
# Lo que el panel puede ver y tocar
# ---------------------------------------------------------------------------------


@pytest.fixture
def equipo(conn, restore_config):
    db.execute("insert into panel_users (email) values (%s)", (ALLOWED,))


def test_the_table_is_internal_bookkeeping_nobody_reads_from_the_panel(equipo):
    db.execute("insert into resumenes_prioridad (dia, estado) values (current_date, 'vacio')")
    for quien in (ALLOWED, STRANGER, None):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            as_user(quien, "select * from resumenes_prioridad")
    assert db.fetch_one(
        "select relrowsecurity from pg_class where relname = 'resumenes_prioridad'"
    )["relrowsecurity"]


@pytest.mark.parametrize("clave", ["prioridad_activa", "resumen_prioridad_activo"])
def test_the_switches_can_be_turned_off_and_on_from_settings(equipo, clave):
    as_user(ALLOWED, "update config set value = 'false'::jsonb where key = %s", (clave,))
    assert valor(clave) is False
    as_user(ALLOWED, "update config set value = 'true'::jsonb where key = %s", (clave,))
    assert valor(clave) is True


@pytest.mark.parametrize("clave", ["prioridad_activa", "resumen_prioridad_activo"])
@pytest.mark.parametrize("malo", ["1", '"yes"', "null"])
def test_the_switches_only_take_a_boolean(equipo, clave, malo):
    with pytest.raises(psycopg.errors.Error):
        as_user(ALLOWED, "update config set value = %s::jsonb where key = %s", (malo, clave))
    assert valor(clave) is True


@pytest.mark.parametrize(
    ("clave", "bueno"),
    [
        ("resumen_prioridad_hora", "0"),
        ("resumen_prioridad_hora", "23"),
        ("resumen_prioridad_max_por_nivel", "20"),
        ("resumen_prioridad_dias", "60"),
    ],
)
def test_the_numbers_can_be_changed_from_settings(equipo, clave, bueno):
    as_user(ALLOWED, "update config set value = %s::jsonb where key = %s", (bueno, clave))
    assert valor(clave) == int(bueno)


@pytest.mark.parametrize(
    ("clave", "malo"),
    [
        ("resumen_prioridad_hora", "24"),
        ("resumen_prioridad_hora", "-1"),
        ("resumen_prioridad_hora", "8.5"),
        ("resumen_prioridad_hora", '"8"'),
        ("resumen_prioridad_hora", "true"),
        ("resumen_prioridad_max_por_nivel", "21"),
        ("resumen_prioridad_max_por_nivel", "{}"),
        ("resumen_prioridad_dias", "61"),
        ("resumen_prioridad_dias", "null"),
    ],
)
def test_the_numbers_only_take_whole_values_in_range(equipo, clave, malo):
    antes = valor(clave)
    with pytest.raises(psycopg.errors.Error):
        as_user(ALLOWED, "update config set value = %s::jsonb where key = %s", (malo, clave))
    assert valor(clave) == antes


def test_a_stranger_cannot_touch_any_of_it(equipo):
    for clave in CLAVES:
        antes = valor(clave)
        as_user(STRANGER, "update config set value = '1'::jsonb where key = %s", (clave,))
        as_user(STRANGER, "update config set value = 'false'::jsonb where key = %s", (clave,))
        assert valor(clave) == antes


def test_the_new_policy_does_not_widen_the_allowlist(equipo):
    for clave in ("kill_switch", "monthly_budget_usd", "zona_horaria"):
        antes = valor(clave)
        as_user(ALLOWED, "update config set value = '5'::jsonb where key = %s", (clave,))
        assert valor(clave) == antes
