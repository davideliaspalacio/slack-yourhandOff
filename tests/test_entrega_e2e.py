"""De un mensaje del Founders Club a una tarjeta, un SMS y un descarte real.

Recorre el sistema completo con todo lo externo falseado (Slack, OpenAI,
Twilio, Resend, Serper): un mensaje entra por el watcher, el resolver lo
encola, `research_runner.run_next_job` investiga y entrega -- publicando la
tarjeta y, por ser banda alta, un SMS -- y por último el botón "Descartar" se
pulsa a través del receptor FastAPI real, con firma verificada. Un segundo
intento de entrega para la misma persona, ya descartada, no debe publicar
nada ni mandar nada, y el resumen diario no debe mencionarla.

Se apoya en las mismas piezas que `test_ingest_e2e.py` (los tools falseados,
`ScriptedOpenAI`) y que `test_web_buttons.py` (la firma de las peticiones al
receptor).
"""

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from handoff_agent import db, llm
from handoff_agent.delivery import deliver, digest, slack_writer, sms
from handoff_agent.ingest import queue
from handoff_agent.ingest.research_runner import run_next_job
from handoff_agent.ingest.resolver import resolve_pending
from handoff_agent.ingest.watcher import watch_tick
from handoff_agent.web import app as web_app
from tests.slack_fakes import FakeReader
from tests.test_dossier import make_dossier
from tests.test_ingest_e2e import HOME, LINKEDIN, NOW, fake_tools
from tests.test_synthesize import ScriptedOpenAI
from tests.test_web_buttons import CHANNEL, SECRET, press

PATCHED_PERMALINK = "https://acme.slack.com/archives/CHANDOFF/p1700000000000100"


def dossier_json(puntuacion: int, evidencia: str | None, razon: str) -> str:
    """Lo que devolvería la síntesis. `evidencia=None` es una ficha sin el campo."""
    fit = {"puntuacion": puntuacion, "razon": razon}
    if evidencia is not None:
        fit["evidencia"] = evidencia
    return json.dumps(
        make_dossier(
            persona={"nombre": "Ada Ruiz", "cargo": "CEO", "fuente": LINKEDIN},
            empresa={**make_dossier()["empresa"], "fuentes": [HOME]},
            contratacion={
                # "vacantes" exige vacantes reales (dossier.validate_dossier).
                "vacantes_abiertas": 2 if evidencia == "vacantes" else None,
                "roles": [],
                "roles_deslocalizables": [],
                "fuentes": [HOME] if evidencia == "vacantes" else [],
            },
            senales_contexto=[{"hecho": "Contrata soporte", "fuente": HOME}],
            encaje_handoff=fit,
            busquedas_sugeridas=[],
        )
    )


def alta_dossier() -> str:
    """Un dossier con encaje 3/3 y su prueba: banda alta, tarjeta y SMS."""
    return dossier_json(3, "necesidad_declarada", "Contrata soporte ahora mismo")


def state_of(person_id) -> str:
    return db.fetch_one("select state from prospects where id = %s", (person_id,))["state"]


def test_a_message_becomes_a_card_and_an_sms_then_a_discard_stops_both(conn, monkeypatch):
    # -- Preparación: canal falso, perfil, mensaje, y todo lo externo falseado --
    reader = FakeReader()
    reader.set_members("C1", "U1")
    reader.add_profile("U1", "Ada Ruiz", title="CEO @ Acme", email="ada@acme.com")
    reader.post("C1", "U1", "Our support queue is out of control", f"{NOW - 60:.6f}")

    fake_tools(monkeypatch)
    monkeypatch.setattr(llm, "_client", lambda: ScriptedOpenAI(alta_dossier()))

    # Twilio "configurado" con valores falsos; _send se sustituye por completo.
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "not-a-real-token")
    monkeypatch.setenv("TWILIO_FROM", "+15550000000")
    monkeypatch.setenv("TWILIO_TO", "+15551111111")
    sms_calls = []
    monkeypatch.setattr(sms, "_send", lambda to, body: sms_calls.append((to, body)) or "SM123")
    # run_next_job llama a maybe_send sin `now`: se fuerza el horario permitido
    # sin depender de la hora real en la que corra la suite.
    monkeypatch.setattr(sms, "_within_hours", lambda now: True)
    monkeypatch.setattr(slack_writer, "card_permalink", lambda channel, ts: PATCHED_PERMALINK)

    posted = []

    def fake_post_card(blocks, text):
        posted.append((blocks, text))
        return ("CHANDOFF", "1.1")

    monkeypatch.setattr(slack_writer, "post_card", fake_post_card)

    # -- watcher -> resolver -> research + entrega --
    tick = watch_tick(reader, ["C1"], lookback_hours=1, now=lambda: NOW)
    assert tick.messages_new == 1
    assert resolve_pending().enqueued == 1
    assert run_next_job(reader).status == "hecho"

    person = db.fetch_one("select * from prospects where slack_user_id = 'U1'")
    assert person["state"] == "investigado"

    # La tarjeta se publicó, cita a la persona y trae el botón "ver_original".
    assert len(posted) == 1
    blocks, _summary = posted[0]
    card_text = json.dumps(blocks, ensure_ascii=False, default=str)
    assert "Ada Ruiz" in card_text
    actions = next(b for b in blocks if b["type"] == "actions")["elements"]
    link_button = next(e for e in actions if e["action_id"] == "ver_original")
    assert link_button["url"].startswith("https://fake.slack.com/archives/C1/p")

    delivery_rows = db.fetch_all(
        "select kind, band from deliveries where prospect_id = %s order by kind", (person["id"],)
    )
    assert {(r["kind"], r["band"]) for r in delivery_rows} == {("sms", "alta"), ("slack", "alta")}

    # El SMS de banda alta salió una vez, con el permalink parcheado.
    assert len(sms_calls) == 1
    assert "Ada Ruiz" in sms_calls[0][1]
    assert PATCHED_PERMALINK in sms_calls[0][1]

    cost_row = db.fetch_one(
        "select source, prospect_id from cost_events where source = 'twilio_sms'"
    )
    assert cost_row is not None
    assert str(cost_row["prospect_id"]) == str(person["id"])

    # -- el botón "Descartar" a través del receptor FastAPI real, firmado --
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("HANDOFF_SLACK_CHANNEL_ID", CHANNEL)
    monkeypatch.setattr(web_app.slack_writer, "update_card", lambda *a, **k: None)
    client = TestClient(web_app.app)

    response = press(client, "descartar", person["id"])
    assert response.status_code == 200
    assert state_of(person["id"]) == "descartado"

    # -- una segunda entrega para la misma persona, ya descartada, no publica
    # nada ni manda nada --
    assert deliver.deliver_for(person["id"], reader) is None
    assert len(posted) == 1
    assert len(sms_calls) == 1

    assert queue.enqueue(person["slack_user_id"], "manual") is True
    second = run_next_job(reader)
    assert second.status == "hecho"
    assert second.detail == "omitido"

    assert len(posted) == 1
    assert len(sms_calls) == 1
    assert db.fetch_one("select count(*) as n from deliveries")["n"] == 2

    # -- el resumen diario de hoy no menciona a la persona descartada --
    day = datetime.now(tz=digest._zone()).date()
    subject, body = digest.build(day)
    assert "Ada Ruiz" not in subject
    assert "Ada Ruiz" not in body


# --- Una nota de encaje sin pruebas no interrumpe (exigir_evidencia, 0020) ---

# La razón, palabra por palabra, de la ficha que se publicó como tarjeta 2/3 y
# el CEO rechazó: una suposición por sector.
RAZON_SUPUESTA = (
    "The company likely has needs in support, operations, or back office, "
    "though there is no direct evidence of current open roles"
)


def run_pipeline(monkeypatch, *answers: str):
    """Del mensaje a la entrega, con todo lo externo falseado y Twilio
    "configurado". Devuelve la persona, las tarjetas y los SMS que salieron, el
    resultado del job y el lector, por si el test quiere volver a investigar."""
    reader = FakeReader()
    reader.set_members("C1", "U1")
    reader.add_profile("U1", "Ada Ruiz", title="CEO @ Acme", email="ada@acme.com")
    reader.post("C1", "U1", "Our support queue is out of control", f"{NOW - 60:.6f}")

    fake_tools(monkeypatch)
    scripted = ScriptedOpenAI(*answers)
    monkeypatch.setattr(llm, "_client", lambda: scripted)

    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "not-a-real-token")
    monkeypatch.setenv("TWILIO_FROM", "+15550000000")
    monkeypatch.setenv("TWILIO_TO", "+15551111111")
    sms_calls = []
    monkeypatch.setattr(sms, "_send", lambda to, body: sms_calls.append((to, body)) or "SM123")
    monkeypatch.setattr(sms, "_within_hours", lambda now: True)
    monkeypatch.setattr(slack_writer, "card_permalink", lambda channel, ts: PATCHED_PERMALINK)
    posted = []
    monkeypatch.setattr(
        slack_writer, "post_card", lambda blocks, text: posted.append(blocks) or ("CHANDOFF", "1.1")
    )

    watch_tick(reader, ["C1"], lookback_hours=1, now=lambda: NOW)
    resolve_pending()
    outcome = run_next_job(reader)
    person = db.fetch_one("select * from prospects where slack_user_id = 'U1'")
    return person, posted, sms_calls, outcome, reader


def digest_today() -> str:
    _subject, body = digest.build(datetime.now(tz=digest._zone()).date())
    return body


def stored_fit(person) -> dict:
    row = db.fetch_one(
        "select content from dossiers where prospect_id = %s order by version desc limit 1",
        (person["id"],),
    )
    return row["content"]["encaje_handoff"]


def test_a_top_score_without_evidence_gets_no_card_and_no_sms_but_reaches_the_digest(
    conn, monkeypatch
):
    person, posted, sms_calls, outcome, _ = run_pipeline(
        monkeypatch, dossier_json(3, "ninguna", "Strong profile for Handoff")
    )
    # El research se hizo y la ficha se guardó tal cual, con su 3: el candado
    # no reescribe la nota, solo decide que no interrumpe.
    assert outcome.status == "hecho"
    assert person["state"] == "investigado"
    assert stored_fit(person) == {
        "puntuacion": 3,
        "evidencia": "ninguna",
        "razon": "Strong profile for Handoff",
    }
    assert posted == []
    assert sms_calls == []
    assert db.fetch_one("select count(*) as n from deliveries")["n"] == 0
    body = digest_today()
    assert "Ada Ruiz" in body
    assert "Low-band signals: 1" in body


def test_the_real_case_a_guessed_fit_2_no_longer_publishes_a_card(restore_config, monkeypatch):
    """Regresión del caso real: encaje 2, sin vacantes, razón "likely has
    needs... no direct evidence" y evidencia "ninguna".

    Primero con el candado apagado, que es el código de antes: la misma ficha
    publica tarjeta. Luego encendido, que es como queda: no publica nada y la
    persona pasa al email diario."""
    db.execute("update config set value = 'false'::jsonb where key = 'exigir_evidencia'")
    ficha = dossier_json(2, "ninguna", RAZON_SUPUESTA)
    person, posted, sms_calls, _, reader = run_pipeline(monkeypatch, ficha, ficha)
    assert len(posted) == 1, "sin el candado esta ficha publicaba tarjeta: ese era el fallo"
    header = posted[0][0]["text"]["text"]
    assert "MEDIUM SIGNAL" in header and "fit 2/3" in header
    assert "Open roles: none found" in header
    assert "Ada Ruiz" not in digest_today()

    # La misma ficha, investigada otra vez con el candado puesto.
    db.execute("update config set value = 'true'::jsonb where key = 'exigir_evidencia'")
    assert queue.enqueue(person["slack_user_id"], "manual") is True
    assert run_next_job(reader).status == "hecho"
    assert db.fetch_one("select max(version) as v from dossiers")["v"] == 2
    assert stored_fit(person)["razon"] == RAZON_SUPUESTA
    assert len(posted) == 1, "con el candado, la ficha nueva no publica otra tarjeta"
    assert sms_calls == []
    assert db.fetch_one("select count(*) as n from deliveries")["n"] == 1
    assert "Ada Ruiz" in digest_today()


def test_the_real_case_with_the_lock_on_from_the_start_publishes_nothing(conn, monkeypatch):
    person, posted, sms_calls, _, _ = run_pipeline(
        monkeypatch, dossier_json(2, "ninguna", RAZON_SUPUESTA)
    )
    assert stored_fit(person)["puntuacion"] == 2
    assert posted == []
    assert sms_calls == []
    assert db.fetch_one("select count(*) as n from deliveries")["n"] == 0
    assert "Ada Ruiz" in digest_today()


def test_with_real_evidence_everything_publishes_like_before(conn, monkeypatch):
    """Vacantes o necesidad pedida: tarjeta y SMS con 3, solo tarjeta con 2.
    Con equipo_relevante, lo mismo. Nada de esto pasa por el email diario."""
    cases = [
        (3, "vacantes", {("slack", "alta"), ("sms", "alta")}),
        (3, "necesidad_declarada", {("slack", "alta"), ("sms", "alta")}),
        (3, "equipo_relevante", {("slack", "alta"), ("sms", "alta")}),
        (2, "vacantes", {("slack", "media")}),
        (2, "necesidad_declarada", {("slack", "media")}),
        (2, "equipo_relevante", {("slack", "media")}),
    ]
    for puntuacion, evidencia, expected in cases:
        with monkeypatch.context() as patch:
            _, posted, sms_calls, _, _ = run_pipeline(
                patch, dossier_json(puntuacion, evidencia, "Hiring support right now")
            )
            rows = db.fetch_all("select kind, band from deliveries")
            assert {(r["kind"], r["band"]) for r in rows} == expected, (puntuacion, evidencia)
            assert len(posted) == 1, (puntuacion, evidencia)
            assert len(sms_calls) == (1 if puntuacion == 3 else 0), (puntuacion, evidencia)
            assert "Ada Ruiz" not in digest_today()
        # Cada caso arranca limpio: misma persona, mismo mensaje, otra ficha.
        db.execute(
            "truncate table deliveries, research_jobs, slack_messages, member_snapshots, "
            "dossiers, prospects, agent_actions, llm_calls, cost_events restart identity cascade"
        )


@pytest.mark.parametrize(
    ("puntuacion", "expected"),
    [(3, {("slack", "alta"), ("sms", "alta")}), (2, {("slack", "media")})],
)
def test_with_the_lock_off_no_evidence_delivers_exactly_like_today(
    restore_config, monkeypatch, puntuacion, expected
):
    db.execute("update config set value = 'false'::jsonb where key = 'exigir_evidencia'")
    _, posted, sms_calls, _, _ = run_pipeline(
        monkeypatch, dossier_json(puntuacion, "ninguna", RAZON_SUPUESTA)
    )
    rows = db.fetch_all("select kind, band from deliveries")
    assert {(r["kind"], r["band"]) for r in rows} == expected
    assert len(posted) == 1
    assert len(sms_calls) == (1 if puntuacion == 3 else 0)


def test_a_dossier_with_an_invented_evidence_is_rejected_and_retried(conn, monkeypatch):
    """Un valor presente pero fuera de la lista no entra: se reintenta con el
    motivo y la segunda, ya válida, es la que se guarda."""
    person, posted, _, outcome, _ = run_pipeline(
        monkeypatch,
        dossier_json(3, "sector_guess", "Hiring support right now"),
        dossier_json(3, "vacantes", "Hiring support right now"),
    )
    assert outcome.status == "hecho"
    assert stored_fit(person)["evidencia"] == "vacantes"
    assert db.fetch_one("select count(*) as n from llm_calls")["n"] == 2
    assert len(posted) == 1


def test_a_dossier_that_forgets_its_evidence_is_stored_as_ninguna_and_stays_low(conn, monkeypatch):
    """El olvido no cuesta la ficha ni un segundo intento: se guarda con
    "ninguna", sin tarjeta ni SMS (banda baja), pero en panel y digest."""
    person, posted, sms_calls, outcome, _ = run_pipeline(
        monkeypatch, dossier_json(3, None, "Hiring support right now")
    )
    assert outcome.status == "hecho"
    assert person["state"] == "investigado"
    assert stored_fit(person)["evidencia"] == "ninguna"
    assert db.fetch_one("select count(*) as n from llm_calls")["n"] == 1
    assert posted == [] and sms_calls == []
    assert db.fetch_one("select count(*) as n from deliveries where kind = 'slack'")["n"] == 0


def test_a_dossier_that_keeps_inventing_its_evidence_is_never_stored(conn, monkeypatch):
    bad = dossier_json(3, "sector_guess", "Hiring support right now")
    person, posted, sms_calls, _, _ = run_pipeline(monkeypatch, bad, bad)
    assert db.fetch_one("select count(*) as n from dossiers")["n"] == 0
    assert person["state"] == "incompleto"
    assert posted == []
    assert sms_calls == []
