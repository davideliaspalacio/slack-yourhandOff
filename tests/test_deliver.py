import json

from handoff_agent import db
from handoff_agent.delivery import deliver
from handoff_agent.tools import prospects
from tests.slack_fakes import FakeReader
from tests.test_card import DOSSIER


def a_person(state="investigado", score=3):
    person = prospects.upsert_prospect("U1", full_name="Ada Ruiz")
    db.execute("update prospects set state = %s where id = %s", (state, person["id"]))
    dossier = {**DOSSIER, "encaje_handoff": {"puntuacion": score, "razon": "r"}}
    prospects.save_dossier(
        person["id"], dossier, [{"url": "https://acme.com", "kind": "home", "title": "Acme"}]
    )
    return person


def store_message(user: str, ts: str, text: str, status: str = "nuevo") -> None:
    db.execute(
        "insert into slack_messages (channel_id, ts, user_id, text, status) "
        "values ('C1', %s, %s, %s, %s)",
        (ts, user, text, status),
    )


def test_a_high_band_person_gets_a_card(conn, monkeypatch):
    posted = []
    monkeypatch.setattr(
        deliver.slack_writer,
        "post_card",
        lambda blocks, text: posted.append((blocks, text)) or ("CHANDOFF", "1.1"),
    )
    person = a_person()
    assert deliver.deliver_for(person["id"], FakeReader()) == "alta"
    row = db.fetch_one("select kind, band, channel_id, message_ts from deliveries")
    assert (row["kind"], row["band"], row["channel_id"], row["message_ts"]) == (
        "slack",
        "alta",
        "CHANDOFF",
        "1.1",
    )
    assert posted


def test_the_same_dossier_is_never_delivered_twice(conn, monkeypatch):
    monkeypatch.setattr(deliver.slack_writer, "post_card", lambda blocks, text: ("CHANDOFF", "1.1"))
    person = a_person()
    assert deliver.deliver_for(person["id"], FakeReader()) == "alta"
    assert deliver.deliver_for(person["id"], FakeReader()) is None
    assert db.fetch_one("select count(*) as n from deliveries")["n"] == 1


def test_a_discarded_person_never_generates_an_alert(conn, monkeypatch):
    """Descartar no es solo dejar de gastar: es dejar de avisar."""
    monkeypatch.setattr(deliver.slack_writer, "post_card", lambda blocks, text: ("CHANDOFF", "1.1"))
    person = a_person(state="descartado")
    assert deliver.deliver_for(person["id"], FakeReader()) is None
    assert db.fetch_one("select count(*) as n from deliveries")["n"] == 0


def test_score_zero_is_not_delivered(conn, monkeypatch):
    monkeypatch.setattr(deliver.slack_writer, "post_card", lambda blocks, text: ("CHANDOFF", "1.1"))
    person = a_person(score=0)
    assert deliver.deliver_for(person["id"], FakeReader()) is None


def test_a_low_band_person_waits_for_the_digest(conn, monkeypatch):
    """Banda baja no interrumpe: se anota para el email diario, sin tarjeta."""
    called = []
    monkeypatch.setattr(deliver.slack_writer, "post_card", lambda blocks, text: called.append(1))
    person = a_person(score=1)
    assert deliver.deliver_for(person["id"], FakeReader()) == "baja"
    assert called == []
    assert db.fetch_one("select count(*) as n from deliveries")["n"] == 0


def test_slack_failing_does_not_lose_the_research(conn, monkeypatch):
    def boom(blocks, text):
        raise RuntimeError("slack caído")

    monkeypatch.setattr(deliver.slack_writer, "post_card", boom)
    person = a_person()
    assert deliver.deliver_for(person["id"], FakeReader()) is None
    assert (
        db.fetch_one("select count(*) as n from agent_actions where action = 'entrega_fallida'")[
            "n"
        ]
        == 1
    )


# -- Corrección 1: al momento de entregar, el mensaje ya pasó por el resolver
# (ingest/resolver.py:55-66), que lo mueve de 'nuevo' a 'pendiente_scoring'
# (o 'archivado') antes de que corra el research. Un filtro que solo mirara
# 'nuevo' nunca encontraría cita que citar en la tarjeta real.


def test_the_quote_comes_from_a_message_already_moved_to_pendiente_scoring(conn, monkeypatch):
    person = a_person()
    store_message("U1", "1700000000.000100", "Our support queue is on fire", "pendiente_scoring")

    posted = {}

    def capture(blocks, text):
        posted["blocks"] = blocks
        return ("CHANDOFF", "1.1")

    monkeypatch.setattr(deliver.slack_writer, "post_card", capture)

    assert deliver.deliver_for(person["id"], FakeReader()) == "alta"
    text = json.dumps(posted["blocks"], ensure_ascii=False, default=str)
    assert "support queue is on fire" in text


def test_an_ignored_message_is_never_quoted(conn, monkeypatch):
    posted = {}

    def capture(blocks, text):
        posted["blocks"] = blocks
        return ("CHANDOFF", "1.1")

    monkeypatch.setattr(deliver.slack_writer, "post_card", capture)
    person = a_person()
    store_message("U1", "1700000000.000100", "esto no debe citarse", "ignorado")

    assert deliver.deliver_for(person["id"], FakeReader()) == "alta"
    text = json.dumps(posted["blocks"], ensure_ascii=False, default=str)
    assert "esto no debe citarse" not in text
