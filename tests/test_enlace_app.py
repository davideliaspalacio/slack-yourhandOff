"""El enlace "Open in app" de los avisos (0041): el que abre el mensaje en el teléfono.

Lo que cuidan estos tests:

1. Que el formato sea uno, y esté en un solo sitio: `delivery/enlace_app.py`.
2. Que el enlace web de siempre NO se pierda: el de la app va al lado, no en su lugar.
3. Que nada de esto pueda romper un aviso: sin el código del workspace, con el
   interruptor apagado o con un enlace raro, el aviso sale como antes.
4. Que el esquema `slack://` nunca vaya en un botón: si Slack lo rechazara, se
   perdería el aviso entero.
5. Que el código del workspace se pregunte a Slack una vez, no en cada aviso.
"""

import json
import time
from datetime import UTC, datetime

import pytest

from handoff_agent import db
from handoff_agent.delivery import (
    alerta_card,
    card,
    enlace_app,
    resumen_prioridad,
    seguimiento,
    seguimiento_canal,
)
from handoff_agent.signals import eventos
from tests.slack_fakes import FakeReader
from tests.test_card import DOSSIER, MESSAGE, PERSON

TEAM = "T0FOUNDERS"
MENSAJE = "https://foundersclub.slack.com/archives/C0INTROS/p1726000000000100"
RESPUESTA = (
    "https://foundersclub.slack.com/archives/C0INTROS/p1726000500000200"
    "?thread_ts=1726000000.000100&cid=C0INTROS"
)
APP_MENSAJE = f"slack://channel?team={TEAM}&id=C0INTROS&message=1726000000.000100"
APP_RESPUESTA = (
    f"slack://channel?team={TEAM}&id=C0INTROS&message=1726000500.000200&thread_ts=1726000000.000100"
)


@pytest.fixture(autouse=True)
def sin_memoria(monkeypatch):
    """`recordar` se acuerda de cuándo preguntó por última vez: cada test, de cero."""
    monkeypatch.setattr(enlace_app, "_ultimo_refresco", None)


def poner(clave, valor):
    db.execute(
        "insert into config (key, value) values (%s, %s::jsonb) "
        "on conflict (key) do update set value = excluded.value",
        (clave, json.dumps(valor)),
    )


@pytest.fixture
def workspace(restore_config):
    poner(enlace_app.TEAM_ID_KEY, TEAM)


def todo(blocks):
    return json.dumps(blocks, ensure_ascii=False)


def urls_de_botones(blocks):
    return [
        e["url"]
        for b in blocks
        if b.get("type") == "actions"
        for e in b.get("elements", [])
        if "url" in e
    ]


# ---------------------------------------------------------------------------------
# El formato
# ---------------------------------------------------------------------------------


def test_a_message_link_becomes_an_app_link_to_that_message(conn, workspace):
    assert enlace_app.de_mensaje(MENSAJE) == APP_MENSAJE


def test_a_reply_link_opens_the_reply_inside_its_thread(conn, workspace):
    assert enlace_app.de_mensaje(RESPUESTA) == APP_RESPUESTA


def test_a_direct_message_uses_the_documented_format_as_is(conn, workspace):
    """`slack://channel?team=…&id=…` es lo único que la documentación de Slack da por
    bueno; un DM no lleva nada más."""
    assert enlace_app.de_conversacion("T0DM", "D0ABC") == "slack://channel?team=T0DM&id=D0ABC"


def test_without_the_workspace_there_is_no_app_link(conn, restore_config):
    poner(enlace_app.TEAM_ID_KEY, "")
    assert enlace_app.de_mensaje(MENSAJE) is None


def test_the_switch_turns_it_off(conn, workspace):
    poner(enlace_app.ACTIVO_KEY, False)
    assert enlace_app.de_mensaje(MENSAJE) is None
    assert enlace_app.de_conversacion("T0DM", "D0ABC") is None


def test_if_the_database_does_not_answer_there_is_no_link_and_no_error(conn, monkeypatch):
    def caida(*a, **k):
        raise RuntimeError("base caída")

    monkeypatch.setattr(enlace_app.db_config, "value", caida)
    assert enlace_app.de_mensaje(MENSAJE) is None
    assert enlace_app.de_conversacion("T0DM", "D0ABC") is None


@pytest.mark.parametrize(
    "enlace",
    [
        None,
        "",
        "https://evil.co/archives/C0INTROS/p1726000000000100",
        "http://foundersclub.slack.com/archives/C0INTROS/p1726000000000100",
        "https://foundersclub.slack.com/archives/C0INTROS",
        "https://foundersclub.slack.com/archives/C0|X/p1726000000000100",
        "https://foundersclub.slack.com/client/T0/C0INTROS",
        "https://app.example.com/personas/11111111-1111-1111-1111-111111111111",
    ],
)
def test_anything_that_is_not_a_slack_message_link_gives_nothing(conn, workspace, enlace):
    assert enlace_app.de_mensaje(enlace) is None


@pytest.mark.parametrize("hilo", ["1726000000.000100|<!channel>", "x", "1726000000"])
def test_a_strange_thread_parameter_is_dropped_not_copied(conn, workspace, hilo):
    """El enlace lo da Slack, pero lo que entra en la URL se comprueba igual: nada que
    pueda cerrar `<url|texto>` antes de tiempo."""
    enlace = enlace_app.de_mensaje(f"{MENSAJE}?thread_ts={hilo}&cid=C0INTROS")
    assert enlace == APP_MENSAJE


@pytest.mark.parametrize("team", ["", "x", "T0|<!channel>", "t0lower", None])
def test_a_workspace_code_that_does_not_look_like_one_is_not_used(conn, restore_config, team):
    poner(enlace_app.TEAM_ID_KEY, team)
    assert enlace_app.de_mensaje(MENSAJE) is None
    assert enlace_app.de_conversacion(team, "D0ABC") is None


# ---------------------------------------------------------------------------------
# De dónde sale el código del workspace
# ---------------------------------------------------------------------------------


class LectorContado(FakeReader):
    def __init__(self):
        super().__init__()
        self.team = TEAM
        self.preguntas = 0

    def team_id(self):
        self.preguntas += 1
        return super().team_id()


def test_the_worker_notes_the_workspace_and_asks_slack_only_once(conn, restore_config):
    lector = LectorContado()
    for _ in range(5):
        enlace_app.recordar(lector)
    assert lector.preguntas == 1
    assert enlace_app.team_id() == TEAM
    assert enlace_app.de_mensaje(MENSAJE) == APP_MENSAJE


def test_it_asks_again_after_a_few_hours(conn, restore_config):
    lector = LectorContado()
    enlace_app.recordar(lector, clock=lambda: 1000.0)
    enlace_app.recordar(lector, clock=lambda: 1000.0 + enlace_app.REFRESCO_SEGUNDOS - 1)
    assert lector.preguntas == 1
    lector.team = "T0OTRO"
    enlace_app.recordar(lector, clock=lambda: 1000.0 + enlace_app.REFRESCO_SEGUNDOS + 1)
    assert lector.preguntas == 2
    assert enlace_app.team_id() == "T0OTRO"


def test_if_slack_does_not_answer_nothing_breaks_and_it_tries_again(conn, restore_config):
    poner(enlace_app.TEAM_ID_KEY, "")
    lector = LectorContado()
    lector.auth_error = RuntimeError("slack caído")
    enlace_app.recordar(lector)
    assert enlace_app.team_id() is None
    lector.auth_error = None
    enlace_app.recordar(lector)
    assert enlace_app.team_id() == TEAM


def test_a_reader_that_cannot_tell_the_workspace_does_not_break_the_loop(conn, restore_config):
    poner(enlace_app.TEAM_ID_KEY, "")
    enlace_app.recordar(object())
    lector = LectorContado()
    lector.team = ""
    enlace_app.recordar(lector)
    assert enlace_app.team_id() is None


# ---------------------------------------------------------------------------------
# Cada aviso: el enlace web sigue, y el de la app va al lado
# ---------------------------------------------------------------------------------

EN_LA_APP = f"📱 On your phone? <{APP_MENSAJE}|Open in app>"


def test_the_research_card_keeps_its_button_and_adds_the_app_link(conn, workspace):
    blocks = card.build(PERSON, DOSSIER, "alta", MESSAGE, MENSAJE)
    assert urls_de_botones(blocks)[-1] == MENSAJE
    assert blocks[-2]["type"] == "actions"
    assert blocks[-1] == {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": EN_LA_APP}],
    }


def test_the_they_asked_alert_keeps_its_button_and_adds_the_app_link(conn, workspace):
    senal = {"tipo": "busca_contratar", "cita": "need an SDR", "slack_user_id": "U1"}
    blocks = alerta_card.build(senal, PERSON, None, MENSAJE)
    assert MENSAJE in urls_de_botones(blocks)
    assert blocks[-1]["elements"][0]["text"] == EN_LA_APP


def test_each_person_in_a_thread_alert_gets_a_link_to_their_own_reply(conn, workspace):
    senal = {"tipo": "busca_contratar", "cita": "me too", "slack_user_id": "U1"}
    otra = RESPUESTA.replace("p1726000500000200", "p1726000900000300")
    blocks = alerta_card.build_hilo(
        "Anyone hiring SDRs?",
        [
            {"senal": senal, "person": PERSON, "permalink": RESPUESTA},
            {"senal": {**senal, "slack_user_id": "U2"}, "person": None, "permalink": otra},
        ],
    )
    texto = todo(blocks)
    assert RESPUESTA in urls_de_botones(blocks) and otra in urls_de_botones(blocks)
    assert f"<{APP_RESPUESTA}|Open in app>" in texto
    assert (
        f"<slack://channel?team={TEAM}&id=C0INTROS&message=1726000900.000300"
        "&thread_ts=1726000000.000100|Open in app>"
    ) in texto


class Lector(FakeReader):
    def permalink(self, channel, ts):
        return f"https://foundersclub.slack.com/archives/{channel}/p{ts.replace('.', '')}"


@pytest.fixture
def publicados(monkeypatch):
    monkeypatch.setenv("HANDOFF_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("HANDOFF_SLACK_CHANNEL_ID", "CHANDOFF")
    salidas = []

    def post_card(blocks, text):
        salidas.append(blocks)
        return "CHANDOFF", f"1700000000.{len(salidas):06d}"

    monkeypatch.setattr(eventos.slack_writer, "post_card", post_card)
    monkeypatch.setattr(eventos.saludo, "sugerir", lambda *a, **k: None)
    return salidas


def _mensaje(texto, canal, ts):
    db.execute(
        "insert into slack_messages (channel_id, ts, user_id, text) values (%s, %s, 'UMIKE', %s)",
        (canal, ts, texto),
    )


def test_the_mention_alert_links_both(conn, workspace, publicados):
    lector = Lector(owner="UANTHONY")
    ts = f"{time.time() - 60:.6f}"
    _mensaje("hey <@UANTHONY> got a minute?", "C0GENERAL", ts)
    assert eventos.publicar_pendientes(lector) == 1
    web = lector.permalink("C0GENERAL", ts)
    pie = publicados[0][-1]["elements"][0]["text"]
    assert pie == (
        f"They tagged you · <{web}|Open in Slack> · "
        f"<slack://channel?team={TEAM}&id=C0GENERAL&message={ts}|Open in app> to reply"
    )


def test_the_introduction_alert_keeps_its_button_and_adds_the_app_link(conn, workspace, publicados):
    poner("canal_presentaciones", "C0INTROS")
    lector = Lector(owner="UANTHONY")
    ts = f"{time.time() - 60:.6f}"
    _mensaje("Hi all, I run a logistics startup", "C0INTROS", ts)
    assert eventos.publicar_pendientes(lector) == 1
    blocks = publicados[0]
    assert urls_de_botones(blocks) == [lector.permalink("C0INTROS", ts)]
    assert blocks[-1]["elements"][0]["text"] == (
        f"📱 On your phone? <slack://channel?team={TEAM}&id=C0INTROS&message={ts}|Open in app>"
    )


def test_a_direct_message_reminder_links_the_conversation_in_the_app(conn, workspace):
    caso = seguimiento.Caso(
        tipo=seguimiento.RESPONDER, dias=3, nombre="Ada Ruiz", dm_id="D0ABC", team_id="T0DM"
    )
    linea = seguimiento.linea(caso)
    assert linea.endswith(
        "<https://app.slack.com/client/T0DM/D0ABC|Open in Slack> · "
        "<slack://channel?team=T0DM&id=D0ABC|Open in app>"
    )


def test_a_thread_reminder_links_the_reply_in_the_app(conn, workspace):
    pendiente = seguimiento_canal.Pendiente(
        tipo=seguimiento_canal.HILO,
        channel_id="C0INTROS",
        clave_ts="1726000000.000100",
        ts="1726000500.000200",
        en_hilo=True,
    )
    caso = seguimiento.Caso(
        tipo=pendiente.tipo, dias=0, nombre="Ada Ruiz", horas=5, canal=pendiente, enlace=RESPUESTA
    )
    linea = seguimiento.linea(caso)
    assert linea.endswith(f"<{RESPUESTA}|Open in Slack> · <{APP_RESPUESTA}|Open in app>")


def test_the_daily_brief_introduction_line_links_both(conn, workspace):
    fila = {"ts": f"{time.time() - 7200:.6f}", "company_name": "Acme"}
    linea = resumen_prioridad._linea_presentacion(fila, "Ada Ruiz", MENSAJE, datetime.now(UTC))
    assert linea.endswith(f"<{MENSAJE}|Say hi in Slack> · <{APP_MENSAJE}|Open in app>")


# ---------------------------------------------------------------------------------
# Sin enlace de app, los avisos son los de antes
# ---------------------------------------------------------------------------------


def _avisos():
    senal = {"tipo": "busca_contratar", "cita": "need an SDR", "slack_user_id": "U1"}
    caso = seguimiento.Caso(
        tipo=seguimiento.RESPONDER, dias=3, nombre="Ada Ruiz", dm_id="D0ABC", team_id="T0DM"
    )
    fila = {"ts": "1726000000.000100", "company_name": "Acme"}
    ahora = datetime(2024, 9, 11, tzinfo=UTC)
    return [
        todo(card.build(PERSON, DOSSIER, "alta", MESSAGE, MENSAJE)),
        todo(alerta_card.build(senal, PERSON, None, MENSAJE)),
        todo(
            alerta_card.build_hilo(
                "post", [{"senal": senal, "person": PERSON, "permalink": RESPUESTA}]
            )
        ),
        seguimiento.linea(caso),
        resumen_prioridad._linea_presentacion(fila, "Ada Ruiz", MENSAJE, ahora),
    ]


def test_with_the_switch_off_every_alert_is_exactly_the_one_it_was(conn, workspace):
    con = _avisos()
    assert all("Open in app" in aviso for aviso in con)
    poner(enlace_app.ACTIVO_KEY, False)
    sin = _avisos()
    assert not any("slack://" in aviso or "Open in app" in aviso for aviso in sin)
    # Sin el código del workspace pasa lo mismo con todo lo que enlaza a un mensaje del
    # club. El recordatorio de un DM (el cuarto) trae su propio workspace en el rastro.
    poner(enlace_app.ACTIVO_KEY, True)
    poner(enlace_app.TEAM_ID_KEY, "")
    sin_workspace = _avisos()
    assert sin_workspace[:3] + sin_workspace[4:] == sin[:3] + sin[4:]
    assert sin_workspace[3] == con[3]


def test_the_app_scheme_never_goes_in_a_button(conn, workspace):
    """La referencia de Block Kit dice que `url` es "a URL to load in the user's
    browser" y no promete otros esquemas: un botón rechazado tumbaría el aviso entero."""
    senal = {"tipo": "busca_contratar", "cita": "need an SDR", "slack_user_id": "U1"}
    for blocks in (
        card.build(PERSON, DOSSIER, "alta", MESSAGE, MENSAJE),
        alerta_card.build(senal, PERSON, None, MENSAJE),
        alerta_card.build_hilo("p", [{"senal": senal, "person": PERSON, "permalink": RESPUESTA}]),
    ):
        assert all(url.startswith("https://") for url in urls_de_botones(blocks))


def test_the_format_lives_in_one_place():
    """Si la prueba en el teléfono pide otro formato, se cambia en `enlace_app.py` y en
    ningún otro sitio: nadie más escribe `slack://`."""
    import pathlib

    import handoff_agent

    raiz = pathlib.Path(handoff_agent.__file__).parent
    fuera = [
        str(p.relative_to(raiz))
        for p in raiz.rglob("*.py")
        if p.name != "enlace_app.py" and "slack://" in p.read_text()
    ]
    assert fuera == []
