"""Qué atender primero (0039): el nivel de cada aviso, decidido en un solo sitio.

Lo que cuidan estos tests:

1. La tabla de reglas de `prioridad.py`, entera, y que no haya tipo de aviso sin regla.
2. Que la marca se vea igual en todos los avisos -- delante de la cabecera y delante de
   la notificación del móvil -- sin tapar lo que distingue a cada tipo.
3. Que la prioridad nunca pueda costar un aviso: apagada, o con la base rota, el aviso
   sale como salía antes.
"""

import json
import time
from pathlib import Path

import pytest

from handoff_agent import db
from handoff_agent.delivery import (
    alerta_card,
    aviso_lector,
    card,
    deliver,
    novedades,
    prioridad,
    seguimiento,
    slack_writer,
)
from handoff_agent.delivery.prioridad import AHORA, FYI, HOY
from handoff_agent.signals import alertas, eventos, miembros
from handoff_agent.web import app as web_app
from tests.slack_fakes import FakeReader
from tests.test_card import DOSSIER, MESSAGE, PERSON
from tests.test_deliver import a_person

DUENO = "UOWNER"


@pytest.fixture
def slack(monkeypatch):
    """Bot configurado y `post_card` sustituido: lo publicado queda en la lista."""
    monkeypatch.setenv("HANDOFF_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("HANDOFF_SLACK_CHANNEL_ID", "CHANDOFF")
    posted = []

    def post_card(blocks, text):
        posted.append(
            {"blocks": blocks, "text": text, "json": json.dumps(blocks, ensure_ascii=False)}
        )
        return "CHANDOFF", f"1700000000.{len(posted):06d}"

    monkeypatch.setattr(slack_writer, "post_card", post_card)
    monkeypatch.setattr(alertas.procesar, "procesar_pendientes", list)
    return posted


def cabecera(publicado):
    return publicado["blocks"][0]["text"]["text"].splitlines()[0]


def ts(horas: float) -> str:
    return f"{time.time() - horas * 3600:.6f}"


def ajuste(clave, valor):
    db.execute("update config set value = %s::jsonb where key = %s", (json.dumps(valor), clave))


def ficha(user="USUNNY", nombre="Sunny Patel", estado="nuevo", nota=None):
    pid = db.fetch_one(
        "insert into prospects (slack_user_id, full_name, company_name, state) "
        "values (%s, %s, 'Acme', %s) returning id::text as id",
        (user, nombre, estado),
    )["id"]
    if nota is not None:
        db.execute(
            "insert into dossiers (prospect_id, version, content, sources) "
            "values (%s, 1, %s, '[]'::jsonb)",
            (pid, json.dumps({"encaje_handoff": {"puntuacion": nota, "razon": "r"}})),
        )
    return pid


# ---------------------------------------------------------------------------------
# La tabla de reglas
# ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tipo", "datos", "esperado"),
    [
        (prioridad.RESEARCH, {"banda": "alta"}, AHORA),
        (prioridad.RESEARCH, {"banda": "media"}, HOY),
        (prioridad.RESEARCH, {"banda": "baja"}, FYI),
        (prioridad.PIDIERON, {"confianza": "alta"}, AHORA),
        (prioridad.PIDIERON, {"confianza": "media"}, HOY),
        (prioridad.PIDIERON, {"confianza": "baja"}, HOY),
        (prioridad.PIDIERON, {}, HOY),
        (prioridad.MENCION, {"caliente": True}, AHORA),
        (prioridad.MENCION, {}, HOY),
        (prioridad.HILO, {"caliente": True}, AHORA),
        (prioridad.HILO, {}, HOY),
        (prioridad.NO_LEIDO, {"caliente": True}, AHORA),
        (prioridad.NO_LEIDO, {}, HOY),
        (prioridad.RESPONDER, {"caliente": True}, AHORA),
        (prioridad.RESPONDER, {}, HOY),
        (prioridad.SIN_FICHA, {"caliente": True}, HOY),
        (prioridad.PRESENTACION, {}, HOY),
        (prioridad.MIEMBRO_NUEVO, {"banda": "alta"}, HOY),
        (prioridad.MIEMBRO_NUEVO, {"banda": "media"}, HOY),
        (prioridad.MIEMBRO_NUEVO, {"banda": "baja"}, FYI),
        (prioridad.MIEMBRO_NUEVO, {}, FYI),
        (prioridad.SIN_RESPUESTA, {"caliente": True}, FYI),
        (prioridad.SIN_CONVERSACION, {"caliente": True}, FYI),
        (prioridad.CREDENCIAL, {}, AHORA),
        (prioridad.NOVEDADES, {}, FYI),
        (prioridad.REPORTE_CONTRATANDO, {}, FYI),
        (prioridad.REPORTE_CONTRATANDO, {"banda": "alta", "caliente": True}, FYI),
        ("un_tipo_que_no_existe", {"banda": "alta", "caliente": True}, FYI),
    ],
)
def test_the_whole_table_of_rules(tipo, datos, esperado):
    assert prioridad.nivel(tipo, **datos) == esperado


def test_the_rules_are_deterministic_and_never_ask_a_model(monkeypatch):
    from handoff_agent import llm

    def nunca(*a, **k):
        raise AssertionError("la prioridad no llama al modelo")

    monkeypatch.setattr(llm, "complete", nunca)
    for tipo in prioridad.TIPOS:
        assert prioridad.nivel(tipo, banda="alta", confianza="alta", caliente=True) in (
            AHORA,
            HOY,
            FYI,
        )
        assert prioridad.nivel(tipo) == prioridad.nivel(tipo)


def test_every_follow_up_case_has_a_rule_under_its_own_name():
    """Si el seguimiento gana un tipo de caso, este test obliga a decidir su nivel."""
    assert set(seguimiento.ORDEN) <= set(prioridad.TIPOS)
    assert prioridad.MENCION == eventos.MENCION == seguimiento.MENCION
    assert prioridad.PRESENTACION == eventos.PRESENTACION
    assert prioridad.HILO == seguimiento.HILO
    assert prioridad.SIN_FICHA == seguimiento.SIN_FICHA
    assert prioridad.NO_LEIDO == seguimiento.NO_LEIDO


def test_three_levels_from_most_to_least_urgent():
    assert prioridad.NIVELES == (AHORA, HOY, FYI)
    assert [prioridad.orden(n) for n in prioridad.NIVELES] == [0, 1, 2]
    assert prioridad.orden(None) == prioridad.orden("inventado") == 3
    assert prioridad.mas_urgente([FYI, AHORA, HOY]) == AHORA
    assert prioridad.mas_urgente([FYI, None, HOY]) == HOY
    assert prioridad.mas_urgente([]) is None


# ---------------------------------------------------------------------------------
# Cómo se ve
# ---------------------------------------------------------------------------------


def test_the_label_goes_in_front_and_reads_the_same_everywhere():
    assert prioridad.cabecera("🔥 *HIGH SIGNAL*", AHORA) == "🔴 *ACT NOW* · 🔥 *HIGH SIGNAL*"
    assert prioridad.cabecera("👀 *MEDIUM SIGNAL*", HOY) == "🟡 *TODAY* · 👀 *MEDIUM SIGNAL*"
    assert prioridad.cabecera("📣 *What's new*", FYI) == "⚪ *FYI* · 📣 *What's new*"
    assert prioridad.resumen("High signal: Ada", AHORA) == "🔴 Act now · High signal: Ada"
    assert prioridad.resumen("Mention: Ada", HOY) == "🟡 Today · Mention: Ada"
    assert prioridad.resumen("What's new: x", FYI) == "⚪ FYI · What's new: x"


@pytest.mark.parametrize("sin_nivel", [None, "", "urgente", 3])
def test_without_a_known_level_nothing_is_added(sin_nivel):
    assert prioridad.cabecera("🔥 *HIGH SIGNAL*", sin_nivel) == "🔥 *HIGH SIGNAL*"
    assert prioridad.resumen("High signal: Ada", sin_nivel) == "High signal: Ada"
    assert prioridad.marca(sin_nivel) == ""


@pytest.mark.parametrize("nivel", prioridad.NIVELES)
def test_the_label_can_be_taken_off_again(nivel):
    assert prioridad.sin_marca(prioridad.cabecera("🔥 *HIGH SIGNAL*\nAda", nivel)) == (
        "🔥 *HIGH SIGNAL*\nAda"
    )


def test_taking_the_label_off_leaves_everything_else_alone():
    assert prioridad.sin_marca("🔥 *HIGH SIGNAL*") == "🔥 *HIGH SIGNAL*"
    # Slack puede devolver el emoji por su nombre.
    assert prioridad.sin_marca(":red_circle: *ACT NOW* · 🔥 *HIGH*") == "🔥 *HIGH*"
    # Solo al principio, y solo una marca de verdad.
    assert prioridad.sin_marca("x 🔴 *ACT NOW* · y") == "x 🔴 *ACT NOW* · y"
    assert prioridad.sin_marca("🔴 *SOMETHING ELSE* · y") == "🔴 *SOMETHING ELSE* · y"


def test_the_builders_are_unchanged_unless_a_level_is_passed():
    """Quien construye una tarjeta sin decir el nivel obtiene la de siempre."""
    sin = card.build(PERSON, DOSSIER, "alta", MESSAGE, None)
    con = card.build(PERSON, DOSSIER, "alta", MESSAGE, None, prioridad=AHORA)
    assert sin[0]["text"]["text"].splitlines()[0] == "🔥 *HIGH SIGNAL* · fit 3/3"
    assert con[0]["text"]["text"].splitlines()[0] == "🔴 *ACT NOW* · 🔥 *HIGH SIGNAL* · fit 3/3"
    assert con[1:] == sin[1:]
    assert con[0]["text"]["text"].splitlines()[1:] == sin[0]["text"]["text"].splitlines()[1:]

    senal = {"tipo": "soporte", "cita": "we need support"}
    pidieron = alerta_card.build(senal, None, None, None, prioridad=HOY)
    assert pidieron[0]["text"]["text"].splitlines()[0] == "🟡 *TODAY* · 🗣️ *THEY ASKED · support*"
    hilo = alerta_card.build_hilo("post", [{"senal": senal}], prioridad=AHORA)
    assert hilo[0]["text"]["text"].splitlines()[0] == "🔴 *ACT NOW* · 🗣️ *THEY ASKED · support*"


# ---------------------------------------------------------------------------------
# Quién es un lead caliente
# ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("estado", "nota", "caliente"),
    [
        ("nuevo", 3, True),
        ("investigado", 2, True),
        ("investigado", 1, False),
        ("nuevo", None, False),
        ("contactado", None, True),
        ("cliente", 0, True),
    ],
)
def test_a_hot_lead_has_a_good_fit_or_is_already_in_a_conversation(conn, estado, nota, caliente):
    assert prioridad.es_caliente(ficha(estado=estado, nota=nota)) is caliente


def test_a_score_with_no_evidence_is_not_a_hot_lead(conn):
    """La misma regla que decide si hay tarjeta (`bands.py`): una nota sin prueba no."""
    pid = ficha()
    db.execute(
        "insert into dossiers (prospect_id, version, content, sources) "
        "values (%s, 1, %s, '[]'::jsonb)",
        (
            pid,
            json.dumps({"encaje_handoff": {"puntuacion": 3, "razon": "r", "evidencia": "ninguna"}}),
        ),
    )
    assert prioridad.es_caliente(pid) is False


def test_nobody_and_a_broken_database_are_not_hot(conn, monkeypatch):
    assert prioridad.es_caliente(None) is False
    assert prioridad.es_caliente("00000000-0000-0000-0000-000000000000") is False

    def boom(*a, **k):
        raise RuntimeError("base caída")

    monkeypatch.setattr(prioridad.db, "fetch_one", boom)
    assert prioridad.es_caliente("00000000-0000-0000-0000-000000000000") is False


# ---------------------------------------------------------------------------------
# Cada aviso, tal como sale
# ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("nota", "cabecera_esperada", "texto"),
    [
        (3, "🔴 *ACT NOW* · 🔥 *HIGH SIGNAL* · fit 3/3", "🔴 Act now · High signal: Ada Ruiz"),
        (2, "🟡 *TODAY* · 👀 *MEDIUM SIGNAL* · fit 2/3", "🟡 Today · Medium signal: Ada Ruiz"),
    ],
)
def test_the_research_card(conn, slack, nota, cabecera_esperada, texto):
    person = a_person(score=nota)
    deliver.deliver_for(person["id"], FakeReader())
    assert cabecera(slack[0]) == cabecera_esperada
    assert slack[0]["text"] == texto


@pytest.mark.parametrize(
    ("confianza", "cabecera_esperada", "texto"),
    [
        (
            "alta",
            "🔴 *ACT NOW* · 🗣️ *THEY ASKED · support*",
            "🔴 Act now · They asked: support · U1",
        ),
        ("media", "🟡 *TODAY* · 🗣️ *THEY ASKED · support*", "🟡 Today · They asked: support · U1"),
    ],
)
def test_they_asked(conn, slack, confianza, cabecera_esperada, texto):
    marca = ts(0.1)
    db.execute(
        "insert into slack_messages (channel_id, ts, user_id, text, status) "
        "values ('C1', %s, 'U1', 'we need a support rep', 'pendiente_scoring')",
        (marca,),
    )
    db.execute(
        "insert into message_signals (channel_id, ts, detectada, tipo, cita, confianza) "
        "values ('C1', %s, true, 'soporte', 'we need a support rep', %s)",
        (marca, confianza),
    )
    assert alertas.publicar_pendientes() == 1
    assert cabecera(slack[0]) == cabecera_esperada
    assert slack[0]["text"] == texto


@pytest.mark.parametrize(
    ("nota", "cabecera_esperada", "empieza"),
    [
        (3, "🔴 *ACT NOW* · 💬 *YOU WERE MENTIONED*", "🔴 Act now · Mention: Sunny Patel tagged"),
        (None, "🟡 *TODAY* · 💬 *YOU WERE MENTIONED*", "🟡 Today · Mention: Sunny Patel tagged"),
    ],
)
def test_a_mention_runs_more_when_it_comes_from_a_hot_lead(
    conn, slack, nota, cabecera_esperada, empieza
):
    ficha(nota=nota)
    db.execute(
        "insert into slack_messages (channel_id, ts, user_id, text) "
        "values ('CGEN', %s, 'USUNNY', %s)",
        (ts(0.01), f"hey <@{DUENO}> quick question"),
    )
    assert eventos.publicar_pendientes(FakeReader(owner=DUENO)) == 1
    assert cabecera(slack[0]) == cabecera_esperada
    assert slack[0]["text"].startswith(empieza)


def test_an_introduction_is_for_today(conn, slack, restore_config, monkeypatch):
    monkeypatch.setattr(eventos.saludo, "sugerir", lambda *a, **k: None)
    ajuste("canal_presentaciones", "C0INTROS")
    ficha()
    db.execute(
        "insert into slack_messages (channel_id, ts, user_id, text) "
        "values ('C0INTROS', %s, 'USUNNY', 'Hi all, Sunny here')",
        (ts(0.01),),
    )
    assert eventos.publicar_pendientes(FakeReader(owner=DUENO)) == 1
    assert cabecera(slack[0]) == "🟡 *TODAY* · 👋 *NEW INTRODUCTION · say hi*"
    assert slack[0]["text"] == "🟡 Today · New introduction: Sunny Patel — say hi"


@pytest.mark.parametrize(
    ("nota", "cabecera_esperada", "texto"),
    [
        (
            2,
            "🟡 *TODAY* · 🆕 *NEW MEMBER · welcome them*",
            "🟡 Today · New member: Sunny Patel — welcome them",
        ),
        (
            1,
            "⚪ *FYI* · 🆕 *NEW MEMBER · welcome them*",
            "⚪ FYI · New member: Sunny Patel — welcome them",
        ),
        (
            None,
            "⚪ *FYI* · 🆕 *NEW MEMBER · welcome them*",
            "⚪ FYI · New member: Sunny Patel — welcome them",
        ),
    ],
)
def test_a_new_member_is_fyi_unless_the_research_says_they_fit(
    conn, slack, nota, cabecera_esperada, texto
):
    ficha(nota=nota)
    db.execute("insert into miembros_nuevos (slack_user_id, channel_id) values ('USUNNY', 'C1')")
    assert miembros.publicar_pendientes(FakeReader(owner=DUENO)) == 1
    assert cabecera(slack[0]) == cabecera_esperada
    assert slack[0]["text"] == texto


def dm(dm_id, user, de="otro", horas=30.0, **ficha_kwargs):
    pid = ficha(user, f"Persona {user}", **ficha_kwargs)
    db.execute(
        "insert into dm_rastro (dm_id, team_id, slack_user_id, prospect_id, ultimo_ts, ultimo_de) "
        "values (%s, 'T0OWNER', %s, %s, %s, %s)",
        (dm_id, user, pid, ts(horas), de),
    )
    return pid


def test_waiting_on_you_takes_the_level_of_its_most_urgent_case(conn, slack):
    dm("DAAA", "UFRIO")
    assert seguimiento.publicar_avisos() == 1
    assert cabecera(slack[0]) == "🟡 *TODAY* · ⏳ *WAITING ON YOU*"
    assert slack[0]["text"] == "🟡 Today · Follow-ups: 1 conversation waiting"

    dm("DBBB", "UHOT", nota=3)
    assert seguimiento.publicar_avisos() == 1
    assert cabecera(slack[1]) == "🔴 *ACT NOW* · ⏳ *WAITING ON YOU*"
    assert slack[1]["text"] == "🔴 Act now · Follow-ups: 1 conversation waiting"


def test_follow_ups_with_nothing_waiting_on_him_is_fyi(conn, slack):
    """Él escribió y no le contestan: la pelota no está en su tejado."""
    dm("DAAA", "UBOB", de="dueno", horas=24 * 4, nota=3)
    assert seguimiento.publicar_resumen() == 1
    assert cabecera(slack[0]) == "⚪ *FYI* · ⏰ *FOLLOW-UPS*"
    assert slack[0]["text"] == "⚪ FYI · Follow-ups: 1 conversation waiting"


def test_the_expired_credential_is_act_now(conn, slack):
    assert aviso_lector.avisar_credencial_caducada() is True
    assert cabecera(slack[0]).startswith("🔴 *ACT NOW* · ⚠️ I can no longer read Slack")
    assert slack[0]["text"].startswith("🔴 Act now · ⚠️ I can no longer read Slack")


def test_whats_new_is_fyi(conn, slack):
    assert novedades.publicar_pendientes() == len(novedades.NOVEDADES)
    assert cabecera(slack[0]) == "⚪ *FYI* · 📣 *What's new in the Founders Club agent*"
    assert slack[0]["text"].startswith("⚪ FYI · What's new: ")


def test_no_two_kinds_of_alert_share_a_header_once_the_level_is_set_aside():
    """El nivel va delante en todos y lo que distingue a cada tipo sigue detrás: con la
    misma marca puesta, ninguna cabecera se repite ni comparte emoji."""
    from handoff_agent.delivery import reporte_contratando, resumen_prioridad

    propias = [
        reporte_contratando.CABECERA,
        card.build(PERSON, DOSSIER, "alta", MESSAGE, None)[0]["text"]["text"].splitlines()[0],
        alerta_card.build({"tipo": "soporte", "cita": "x"}, None, None, None)[0]["text"][
            "text"
        ].splitlines()[0],
        eventos.CABECERAS[eventos.MENCION],
        eventos.CABECERAS[eventos.PRESENTACION],
        miembros.CABECERA,
        seguimiento.CABECERA_AVISO,
        seguimiento.CABECERA,
        novedades.CABECERA,
        aviso_lector.MENSAJE,
        resumen_prioridad.CABECERA,
    ]
    con_marca = [prioridad.cabecera(c, AHORA) for c in propias]
    assert all(c.startswith("🔴 *ACT NOW* · ") for c in con_marca)
    assert [prioridad.sin_marca(c) for c in con_marca] == propias
    assert len(set(propias)) == len(propias)
    assert len({c.split(" ")[0] for c in propias}) == len(propias)
    # Y ningún tipo usa de emoji propio uno de los tres de la prioridad.
    assert not {c.split(" ")[0] for c in propias} & set(prioridad.EMOJI.values())


# ---------------------------------------------------------------------------------
# Apagada, todo sale como antes
# ---------------------------------------------------------------------------------


def test_the_default_is_on(conn):
    assert db.fetch_one("select value from config where key = 'prioridad_activa'")["value"] is True
    assert prioridad.activa() is True
    assert prioridad.visible(AHORA) == AHORA


def test_switched_off_every_alert_looks_the_way_it_did(conn, slack, restore_config):
    ajuste("prioridad_activa", False)
    assert prioridad.visible(AHORA) is None
    person = a_person(score=3)
    deliver.deliver_for(person["id"], FakeReader())
    dm("DAAA", "UHOT", nota=3)
    seguimiento.publicar_avisos()
    aviso_lector.avisar_credencial_caducada()
    novedades.publicar_pendientes()
    assert [cabecera(p) for p in slack[:2]] == ["🔥 *HIGH SIGNAL* · fit 3/3", "⏳ *WAITING ON YOU*"]
    assert cabecera(slack[2]).startswith("⚠️ I can no longer read Slack")
    assert cabecera(slack[3]) == novedades.CABECERA
    assert slack[0]["text"] == "High signal: Ada Ruiz"
    assert slack[1]["text"] == "Follow-ups: 1 conversation waiting"
    # (El "What's new" nombra los tres niveles en su texto: ahí solo cuenta la cabecera.)
    for publicado in slack[:3]:
        for emoji in prioridad.EMOJI.values():
            assert emoji not in publicado["text"] and emoji not in publicado["json"]


def test_if_the_switch_cannot_be_read_the_alert_goes_out_without_a_label(conn, slack, monkeypatch):
    """La prioridad es presentación: nunca puede costar un aviso."""

    def boom(clave, defecto):
        if clave == "prioridad_activa":
            raise RuntimeError("config caída")
        return defecto

    monkeypatch.setattr(prioridad.db_config, "value", boom)
    assert prioridad.activa() is False
    assert aviso_lector.avisar_credencial_caducada() is True
    assert cabecera(slack[0]).startswith("⚠️ I can no longer read Slack")


# ---------------------------------------------------------------------------------
# Una tarjeta ya atendida deja de decir ACT NOW
# ---------------------------------------------------------------------------------


def test_marking_a_card_contacted_takes_its_priority_label_off():
    bloques = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "🔴 *ACT NOW* · 🔥 *HIGH*\nAda"}},
        {"type": "context", "elements": []},
    ]
    original = json.loads(json.dumps(bloques))
    web_app._quitar_prioridad(bloques)
    assert bloques[0]["text"] == {"type": "mrkdwn", "text": "🔥 *HIGH*\nAda"}
    assert bloques[1] == original[1]


@pytest.mark.parametrize(
    "raros",
    [[], [None], ["texto"], [{"type": "section"}], [{"text": "plano"}], [{"text": {"text": 7}}]],
)
def test_a_strange_payload_from_slack_is_left_alone(raros):
    copia = json.loads(json.dumps(raros))
    web_app._quitar_prioridad(raros)
    assert raros == copia


# ---------------------------------------------------------------------------------
# El panel usa las mismas etiquetas
# ---------------------------------------------------------------------------------


def test_the_panel_uses_the_same_levels_and_labels():
    """`panel/lib/prioridad.ts` no puede importar Python: aquí se vigila que no se separe."""
    ts_panel = (Path(__file__).parent.parent / "panel" / "lib" / "prioridad.ts").read_text()
    assert 'NIVELES: Nivel[] = ["ahora", "hoy", "fyi"]' in ts_panel
    for nivel in prioridad.NIVELES:
        assert f'{nivel}: "{prioridad.ETIQUETAS[nivel]}"' in ts_panel
        assert f'{nivel}: "{prioridad.EMOJI[nivel]}"' in ts_panel
        assert f'{nivel}: "{prioridad.EXPLICACION[nivel]}"' in ts_panel
