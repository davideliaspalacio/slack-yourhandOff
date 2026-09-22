import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from handoff_agent import db
from handoff_agent.ingest import queue
from handoff_agent.web import app as web_app
from tests.test_deliver import a_person

SECRET = "s3cr3t"
CHANNEL = "CHANDOFF"

DEFAULT_BLOCKS = [
    {"type": "section", "text": {"type": "mrkdwn", "text": "hola"}},
    {"type": "actions", "elements": [{"type": "button", "action_id": "contactado"}]},
]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("HANDOFF_SLACK_CHANNEL_ID", CHANNEL)
    monkeypatch.setattr(web_app.slack_writer, "update_card", lambda *a, **k: None)
    return TestClient(web_app.app)


def _sign(body: bytes, ts: str) -> str:
    base = b"v0:" + ts.encode() + b":" + body
    return "v0=" + hmac.new(SECRET.encode(), base, hashlib.sha256).hexdigest()


def _post(client, body: bytes, ts: str | None = None, signature: str | None = None):
    ts = ts if ts is not None else str(int(time.time()))
    signature = signature if signature is not None else _sign(body, ts)
    return client.post(
        "/slack/acciones",
        content=body,
        headers={
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": signature,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )


def press(
    client,
    action,
    value,
    channel=CHANNEL,
    user=None,
    blocks=DEFAULT_BLOCKS,
    include_message=True,
):
    payload = {
        "type": "block_actions",
        "user": user if user is not None else {"username": "anthony"},
        "channel": {"id": channel},
        "actions": [{"action_id": action, "value": str(value)}],
    }
    if include_message:
        payload["message"] = {"ts": "1.1", "blocks": blocks}
    body = ("payload=" + json.dumps(payload)).encode()
    return _post(client, body)


def state_of(person_id) -> str:
    return db.fetch_one("select state from prospects where id = %s", (person_id,))["state"]


def actions_row():
    return db.fetch_one("select action, payload from agent_actions where action = 'boton_pulsado'")


# -- Comportamiento básico --


def test_contactado_moves_the_person(conn, client):
    person = a_person()
    assert press(client, "contactado", person["id"]).status_code == 200
    assert state_of(person["id"]) == "contactado"


def test_descartar_moves_the_person(conn, client):
    person = a_person()
    assert press(client, "descartar", person["id"]).status_code == 200
    assert state_of(person["id"]) == "descartado"


def test_investigar_mas_queues_a_new_job(conn, client):
    person = a_person()
    press(client, "investigar_mas", person["id"])
    row = db.fetch_one(
        "select reason, status from research_jobs where slack_user_id = %s",
        (person["slack_user_id"],),
    )
    assert (row["reason"], row["status"]) == ("manual", "pendiente")


# -- Corrección 1: "Ver original" y cualquier action_id desconocido --


def test_ver_original_is_a_noop(conn, client, monkeypatch):
    person = a_person()
    calls = []
    monkeypatch.setattr(web_app.slack_writer, "update_card", lambda *a, **k: calls.append(1))
    response = press(client, "ver_original", person["id"])
    assert response.status_code == 200
    assert not calls
    assert state_of(person["id"]) == "investigado"
    assert actions_row() is None


def test_an_unknown_action_id_is_a_noop(conn, client):
    person = a_person()
    response = press(client, "algo_que_no_existe", person["id"])
    assert response.status_code == 200
    assert state_of(person["id"]) == "investigado"
    assert actions_row() is None


# -- Corrección 2: nunca 500 con entrada inválida --


def test_a_body_without_a_payload_field_is_400(conn, client):
    body = b"nada_de_payload=1"
    assert _post(client, body).status_code == 400


def test_invalid_json_in_payload_is_400(conn, client):
    body = b"payload=esto-no-es-json"
    assert _post(client, body).status_code == 400


def test_a_json_array_payload_is_400(conn, client):
    body = b"payload=%5B1%2C2%5D"  # payload=[1,2]
    assert _post(client, body).status_code == 400


def test_no_actions_is_200_and_a_noop(conn, client):
    payload = {
        "type": "block_actions",
        "user": {"username": "x"},
        "channel": {"id": CHANNEL},
        "actions": [],
    }
    body = ("payload=" + json.dumps(payload)).encode()
    assert _post(client, body).status_code == 200
    assert actions_row() is None


def test_a_non_uuid_value_is_200_and_a_noop(conn, client):
    response = press(client, "contactado", "no-soy-un-uuid")
    assert response.status_code == 200
    assert actions_row() is None


def test_an_unknown_prospect_is_200_and_a_noop(conn, client):
    response = press(client, "contactado", "00000000-0000-0000-0000-000000000000")
    assert response.status_code == 200
    assert actions_row() is None


# -- Corrección 3: firma sobre bytes crudos --


def test_an_unsigned_request_is_rejected(conn, client):
    ts = str(int(time.time()))
    response = _post(client, b"payload=%7B%7D", ts=ts, signature="v0=bad")
    assert response.status_code == 401


def test_missing_signing_secret_fails_closed(conn, monkeypatch):
    """Sin SLACK_SIGNING_SECRET, hay que rechazar aunque el resto llegara bien."""
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
    no_secret_client = TestClient(web_app.app)
    ts = str(int(time.time()))
    response = _post(no_secret_client, b"payload=%7B%7D", ts=ts, signature="v0=bad")
    assert response.status_code == 401


# -- Corrección 4: solo nuestro canal --


def test_a_foreign_channel_is_refused(conn, client, monkeypatch):
    person = a_person()
    calls = []
    monkeypatch.setattr(web_app.slack_writer, "update_card", lambda *a, **k: calls.append(1))
    response = press(client, "contactado", person["id"], channel="CFOUNDERS")
    assert response.status_code == 200
    assert not calls
    assert state_of(person["id"]) == "investigado"
    assert actions_row() is None


def test_channel_comparison_is_stripped_and_uppercased(conn, client, monkeypatch):
    monkeypatch.setenv("HANDOFF_SLACK_CHANNEL_ID", " chandoff ")
    person = a_person()
    response = press(client, "contactado", person["id"], channel="CHANDOFF")
    assert response.status_code == 200
    assert state_of(person["id"]) == "contactado"


# -- Corrección 5: el trabajo bloqueante corre fuera del bucle de eventos --


def test_processing_runs_through_the_threadpool(conn, client, monkeypatch):
    person = a_person()
    calls = []
    original = web_app.run_in_threadpool

    async def spy(func, *args, **kwargs):
        calls.append(func)
        return await original(func, *args, **kwargs)

    monkeypatch.setattr(web_app, "run_in_threadpool", spy)
    press(client, "contactado", person["id"])
    assert calls == [web_app._handle]


# -- Corrección 6: quién pulsó --


def test_every_click_is_recorded_with_who_pressed_it(conn, client):
    person = a_person()
    press(client, "contactado", person["id"], user={"username": "anthony", "id": "U123ABC"})
    row = actions_row()
    assert row["payload"] == {"accion": "contactado", "usuario": "anthony", "usuario_id": "U123ABC"}


def test_a_valid_slack_id_becomes_a_live_mention(conn, client, monkeypatch):
    person = a_person()
    seen = {}
    monkeypatch.setattr(
        web_app.slack_writer,
        "update_card",
        lambda channel, ts, blocks, text: seen.update(blocks=blocks),
    )
    press(client, "contactado", person["id"], user={"username": "anthony", "id": "U123ABC"})
    note = seen["blocks"][-1]["elements"][0]["text"]
    assert "<@U123ABC>" in note


def test_a_missing_slack_id_falls_back_to_the_username(conn, client, monkeypatch):
    person = a_person()
    seen = {}
    monkeypatch.setattr(
        web_app.slack_writer,
        "update_card",
        lambda channel, ts, blocks, text: seen.update(blocks=blocks),
    )
    press(client, "contactado", person["id"], user={"username": "anthony"})
    note = seen["blocks"][-1]["elements"][0]["text"]
    assert "<@" not in note
    assert "anthony" in note


def test_the_username_is_escaped_before_landing_in_mrkdwn(conn, client, monkeypatch):
    person = a_person()
    seen = {}
    monkeypatch.setattr(
        web_app.slack_writer,
        "update_card",
        lambda channel, ts, blocks, text: seen.update(blocks=blocks),
    )
    press(client, "contactado", person["id"], user={"username": "<!channel>"})
    note = seen["blocks"][-1]["elements"][0]["text"]
    assert "<!channel>" not in note
    assert "&lt;!channel&gt;" in note


# -- Corrección 7: gente descartada --


def test_investigar_mas_on_a_discarded_person_does_not_enqueue(conn, client):
    person = a_person(state="descartado")
    press(client, "investigar_mas", person["id"])
    assert db.fetch_one("select 1 from research_jobs") is None


def test_contactado_on_a_discarded_person_is_allowed(conn, client):
    person = a_person(state="descartado")
    response = press(client, "contactado", person["id"])
    assert response.status_code == 200
    assert state_of(person["id"]) == "contactado"


def test_descartar_is_idempotent(conn, client):
    person = a_person(state="descartado")
    response = press(client, "descartar", person["id"])
    assert response.status_code == 200
    assert state_of(person["id"]) == "descartado"


# -- Corrección 9: encolar cuando ya hay un job abierto no es un error --


def test_investigar_mas_when_already_queued_does_not_error(conn, client):
    person = a_person()
    queue.enqueue(person["slack_user_id"], "manual")
    response = press(client, "investigar_mas", person["id"])
    assert response.status_code == 200
    rows = db.fetch_all(
        "select status from research_jobs where slack_user_id = %s", (person["slack_user_id"],)
    )
    assert len(rows) == 1


# -- Corrección 11: repintado de la tarjeta --


def test_the_repaint_drops_the_actions_block_and_appends_context(conn, client, monkeypatch):
    person = a_person()
    seen = {}
    monkeypatch.setattr(
        web_app.slack_writer,
        "update_card",
        lambda channel, ts, blocks, text: seen.update(blocks=blocks, channel=channel, ts=ts),
    )
    press(client, "contactado", person["id"])
    assert not any(b.get("type") == "actions" for b in seen["blocks"])
    assert seen["blocks"][0] == DEFAULT_BLOCKS[0]
    assert seen["blocks"][-1]["type"] == "context"
    assert seen["channel"] == CHANNEL
    assert seen["ts"] == "1.1"


def test_a_missing_message_skips_the_repaint(conn, client, monkeypatch):
    person = a_person()
    calls = []
    monkeypatch.setattr(web_app.slack_writer, "update_card", lambda *a, **k: calls.append(1))
    response = press(client, "contactado", person["id"], include_message=False)
    assert response.status_code == 200
    assert not calls
    assert state_of(person["id"]) == "contactado"


def test_a_repaint_failure_does_not_fail_the_request(conn, client, monkeypatch):
    person = a_person()

    def boom(*a, **k):
        raise RuntimeError("slack caído")

    monkeypatch.setattr(web_app.slack_writer, "update_card", boom)
    response = press(client, "contactado", person["id"])
    assert response.status_code == 200
    assert state_of(person["id"]) == "contactado"


# -- Requirement: every note appended to the card after a click is in English --


def _note_of(client, monkeypatch, action, person, **kwargs):
    seen = {}
    monkeypatch.setattr(
        web_app.slack_writer,
        "update_card",
        lambda channel, ts, blocks, text: seen.update(
            note=blocks[-1]["elements"][0]["text"], text=text
        ),
    )
    press(client, action, person["id"], user={"username": "anthony", "id": "U123ABC"}, **kwargs)
    return seen


def test_contactado_note_is_in_english(conn, client, monkeypatch):
    person = a_person()
    seen = _note_of(client, monkeypatch, "contactado", person)
    assert seen["note"] == "✅ Marked as contacted · by <@U123ABC>"
    assert seen["text"] == seen["note"]


def test_descartar_note_is_in_english(conn, client, monkeypatch):
    person = a_person()
    seen = _note_of(client, monkeypatch, "descartar", person)
    assert seen["note"] == "🚫 Discarded: won't show up again · by <@U123ABC>"


def test_investigar_mas_note_is_in_english(conn, client, monkeypatch):
    person = a_person()
    seen = _note_of(client, monkeypatch, "investigar_mas", person)
    assert seen["note"] == "🔁 Queued for new research · by <@U123ABC>"


def test_investigar_mas_when_already_queued_note_is_in_english(conn, client, monkeypatch):
    person = a_person()
    queue.enqueue(person["slack_user_id"], "manual")
    seen = _note_of(client, monkeypatch, "investigar_mas", person)
    assert seen["note"] == "⏳ Already queued for research · by <@U123ABC>"


def test_investigar_mas_on_a_discarded_person_note_is_in_english(conn, client, monkeypatch):
    person = a_person(state="descartado")
    seen = _note_of(client, monkeypatch, "investigar_mas", person)
    assert seen["note"] == "🚫 Discarded: won't be researched again · by <@U123ABC>"


def test_no_note_contains_the_old_spanish_wording(conn, client, monkeypatch):
    person = a_person()
    seen = _note_of(client, monkeypatch, "contactado", person)
    assert " por " not in seen["note"]
    assert "Marcado" not in seen["note"]
