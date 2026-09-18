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


def alta_dossier() -> str:
    """Un dossier con encaje 3/3: banda alta, tarjeta y SMS."""
    return json.dumps(
        make_dossier(
            persona={"nombre": "Ada Ruiz", "cargo": "CEO", "fuente": LINKEDIN},
            empresa={**make_dossier()["empresa"], "fuentes": [HOME]},
            contratacion={
                "vacantes_abiertas": None,
                "roles": [],
                "roles_deslocalizables": [],
                "fuentes": [],
            },
            senales_contexto=[{"hecho": "Contrata soporte", "fuente": HOME}],
            encaje_handoff={"puntuacion": 3, "razon": "Contrata soporte ahora mismo"},
            busquedas_sugeridas=[],
        )
    )


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
