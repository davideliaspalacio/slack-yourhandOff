"""Que cada tipo de aviso se reconozca solo (0037, punto 11 del cliente), y que el
LinkedIn de la persona se vea en la tarjeta de research.

Aquí va lo que toca a la tarjeta de research, a su entrega y al SMS. La mención y la
presentación están en `test_eventos.py`; el miembro nuevo, en `test_miembros.py`.
"""

import json
from datetime import UTC, datetime

import pytest

from handoff_agent import db
from handoff_agent.delivery import alerta_card, card, deliver, sms
from handoff_agent.ingest import queue
from handoff_agent.signals import eventos, miembros
from handoff_agent.tools import prospects
from tests.slack_fakes import FakeReader
from tests.test_card import DOSSIER, MESSAGE, PERSON, blocks_text

PERFIL = "https://www.linkedin.com/in/ada-ruiz"


def cabecera(blocks):
    return blocks[0]["text"]["text"]


# ---------------------------------------------------------------------------------
# Por qué se investigó a la persona
# ---------------------------------------------------------------------------------


def test_every_queue_reason_has_its_own_label():
    """Si mañana la cola gana un motivo, este test obliga a decidir cómo se lee."""
    assert set(card.REASON_LABELS) == set(queue.REASONS)
    assert len(set(card.REASON_LABELS.values())) == len(queue.REASONS)


@pytest.mark.parametrize(
    ("motivo", "se_lee"),
    [
        ("miembro_nuevo", "new member"),
        ("mensaje", "posted in the community"),
        ("radar", "target account"),
        ("manual", "requested by the team"),
    ],
)
def test_the_card_says_why_the_person_was_researched(motivo, se_lee):
    blocks = card.build(PERSON, DOSSIER, "alta", MESSAGE, None, reason=motivo)
    # Justo debajo de la cabecera, en su propia línea de contexto.
    assert blocks[1]["type"] == "context"
    linea = blocks[1]["elements"][0]["text"]
    assert linea.startswith("Why researched: ") and se_lee in linea


@pytest.mark.parametrize("motivo", [None, "", "inventado", "<!channel>", 7])
def test_without_a_known_reason_the_card_is_the_one_it_always_was(motivo):
    """El motivo crudo nunca llega a la tarjeta: o es uno conocido, o no hay línea."""
    con = card.build(PERSON, DOSSIER, "alta", MESSAGE, None, reason=motivo)
    assert con == card.build(PERSON, DOSSIER, "alta", MESSAGE, None)
    assert "Why researched" not in blocks_text(con)


# ---------------------------------------------------------------------------------
# El LinkedIn de la persona en la tarjeta
# ---------------------------------------------------------------------------------


def test_the_card_links_the_persons_linkedin_profile():
    blocks = card.build(PERSON, DOSSIER, "alta", MESSAGE, None, linkedin_url=PERFIL)
    assert cabecera(blocks).endswith(f"\n<{PERFIL}|LinkedIn profile>")


def test_the_link_is_in_the_header_so_it_survives_the_buttons_being_removed():
    """Al pulsar "Contacted" el receptor quita los botones: el enlace tiene que seguir."""
    blocks = card.build(PERSON, DOSSIER, "alta", MESSAGE, None, linkedin_url=PERFIL)
    acciones = next(b for b in blocks if b["type"] == "actions")
    assert PERFIL not in json.dumps(acciones)


@pytest.mark.parametrize(
    "url",
    [
        None,
        "https://www.linkedin.com/company/acme",
        "https://evil.co/in/ada",
        "https://www.linkedin.com/in/ada|<!channel>",
        "http://www.linkedin.com/in/ada",
    ],
)
def test_anything_that_is_not_a_profile_is_not_linked(url):
    blocks = card.build(PERSON, DOSSIER, "alta", MESSAGE, None, linkedin_url=url)
    assert blocks == card.build(PERSON, DOSSIER, "alta", MESSAGE, None)
    assert "LinkedIn profile" not in blocks_text(blocks)


def test_the_header_stays_far_below_the_slack_limit_with_everything_at_its_worst():
    largo = "&" * 5000
    dossier = {
        "persona": {"nombre": largo, "cargo": largo},
        "empresa": {"nombre": largo, "empleados_aprox": 10**40},
        "contratacion": {"vacantes_abiertas": 10**40, "roles_deslocalizables": [largo]},
        "encaje_handoff": {"puntuacion": 10**40},
    }
    perfil = "https://www.linkedin.com/in/" + "a" * 270
    blocks = card.build(PERSON, dossier, "media", None, None, linkedin_url=perfil)
    assert perfil in cabecera(blocks)
    assert len(cabecera(blocks)) < 3000


# ---------------------------------------------------------------------------------
# La entrega: de dónde sale el perfil, y el motivo hasta la tarjeta
# ---------------------------------------------------------------------------------


@pytest.fixture
def publicadas(monkeypatch):
    posted = []
    monkeypatch.setattr(
        deliver.slack_writer,
        "post_card",
        lambda blocks, text: posted.append((json.dumps(blocks), text)) or ("CHANDOFF", "1.1"),
    )
    return posted


def una_persona(user="U1", fuente=None, enlaces=None):
    person = prospects.upsert_prospect(user, full_name="Ada Ruiz")
    if enlaces:
        db.execute(
            "update prospects set research_links = %s where id = %s", (enlaces, person["id"])
        )
    dossier = {**DOSSIER, "persona": {"nombre": "Ada Ruiz", "cargo": "CEO", "fuente": fuente}}
    prospects.save_dossier(
        person["id"], dossier, [{"url": "https://acme.com", "kind": "home", "title": "Acme"}]
    )
    return person


def test_the_delivered_card_carries_the_reason_and_the_profile(conn, publicadas):
    person = una_persona(fuente=PERFIL)
    assert deliver.deliver_for(person["id"], FakeReader(), reason="miembro_nuevo") == "alta"
    tarjeta = publicadas[0][0]
    assert "Why researched" in tarjeta and "new member" in tarjeta
    assert f"<{PERFIL}|LinkedIn profile>" in tarjeta


def test_a_delivery_without_a_reason_still_works(conn, publicadas):
    person = una_persona()
    assert deliver.deliver_for(person["id"], FakeReader()) == "alta"
    assert "Why researched" not in publicadas[0][0]
    assert "LinkedIn profile" not in publicadas[0][0]


def test_a_source_that_is_not_linkedin_is_not_shown_as_the_profile(conn, publicadas):
    person = una_persona(fuente="https://acme.com/team")
    deliver.deliver_for(person["id"], FakeReader())
    assert "LinkedIn profile" not in publicadas[0][0]


def test_a_profile_the_team_added_by_hand_wins_over_the_cited_one(conn, publicadas):
    a_mano = "https://www.linkedin.com/in/ada-la-buena"
    person = una_persona(fuente=PERFIL, enlaces=["https://acme.com/about", a_mano])
    deliver.deliver_for(person["id"], FakeReader())
    assert f"<{a_mano}|LinkedIn profile>" in publicadas[0][0]
    assert PERFIL not in publicadas[0][0]


def test_a_radar_person_gets_the_profile_the_radar_found(conn, publicadas):
    """El decisor de una cuenta objetivo entró por LinkedIn: su perfil ya se conoce."""
    person = una_persona(user="li:ACoAAA123")
    cuenta = db.fetch_one("insert into target_accounts (name) values ('Acme') returning id")["id"]
    senal = db.fetch_one(
        "insert into hiring_signals (account_id, title, title_key) "
        "values (%s, 'Support Lead', 'support lead') returning id",
        (cuenta,),
    )["id"]
    db.execute(
        "insert into decision_candidates (signal_id, linkedin_id, profile_url, chosen, "
        "prospect_id) values (%s, 'ACoAAA123', %s, true, %s)",
        (senal, PERFIL, person["id"]),
    )
    deliver.deliver_for(person["id"], FakeReader(), reason="radar")
    assert f"<{PERFIL}|LinkedIn profile>" in publicadas[0][0]
    assert "target account" in publicadas[0][0]


# ---------------------------------------------------------------------------------
# El SMS, en inglés
# ---------------------------------------------------------------------------------


def test_the_sms_is_in_english_and_says_what_it_is(conn, monkeypatch):
    for nombre, valor in {
        "TWILIO_ACCOUNT_SID": "ACnot-a-real-sid",
        "TWILIO_AUTH_TOKEN": "not-a-real-token",
        "TWILIO_FROM": "+15550000000",
        "TWILIO_TO": "+15551111111",
    }.items():
        monkeypatch.setenv(nombre, valor)
    enviados = []
    monkeypatch.setattr(sms, "_send", lambda to, body: enviados.append(body) or "SM123")
    person = una_persona()
    db.execute("update prospects set company_name = 'Acme' where id = %s", (person["id"],))
    mediodia = datetime(2026, 9, 9, 17, 0, tzinfo=UTC)  # 13:00 en Nueva York
    assert sms.maybe_send(person["id"], "alta", now=mediodia) is True
    assert enviados == ["High signal: Ada Ruiz (Acme)"]
    assert "Señal" not in enviados[0]


# ---------------------------------------------------------------------------------
# Ninguna cabecera se repite
# ---------------------------------------------------------------------------------


def test_no_two_kinds_of_alert_share_a_header():
    """Cinco avisos, cinco cabeceras: la primera línea ya dice de qué tipo es."""
    research = cabecera(card.build(PERSON, DOSSIER, "alta", MESSAGE, None)).splitlines()[0]
    pidieron = alerta_card.build({"tipo": "soporte", "cita": "we need support"}, None, None, None)
    cabeceras = [
        research,
        pidieron[0]["text"]["text"].splitlines()[0],
        eventos.CABECERAS[eventos.MENCION],
        eventos.CABECERAS[eventos.PRESENTACION],
        miembros.CABECERA,
    ]
    assert len(set(cabeceras)) == 5
    emojis = [c.split(" ")[0] for c in cabeceras]
    assert len(set(emojis)) == 5
