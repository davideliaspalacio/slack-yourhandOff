import json

from handoff_agent import db, ledger
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


def test_a_newer_ignored_message_does_not_shadow_an_older_real_one(conn, monkeypatch):
    """El orden es por ts, no por status: 'ignorado' se descarta, pero no
    porque sea más nuevo que la última cita real de verdad."""
    posted = {}

    def capture(blocks, text):
        posted["blocks"] = blocks
        return ("CHANDOFF", "1.1")

    monkeypatch.setattr(deliver.slack_writer, "post_card", capture)
    person = a_person()
    store_message("U1", "1700000000.000100", "mensaje real mas viejo", "pendiente_scoring")
    store_message("U1", "1700000000.000200", "mensaje ignorado mas nuevo", "ignorado")

    assert deliver.deliver_for(person["id"], FakeReader()) == "alta"
    text = json.dumps(posted["blocks"], ensure_ascii=False, default=str)
    assert "mensaje real mas viejo" in text
    assert "mensaje ignorado mas nuevo" not in text


def test_an_archived_message_is_still_a_usable_quote(conn, monkeypatch):
    """Este combo (mensaje 'archivado' + persona que no está 'descartado') no
    ocurre hoy en la práctica: resolver.py solo marca 'archivado' cuando la
    persona ya está 'descartado', y ese estado corta la entrega antes de
    llegar aquí (ver test_a_discarded_person_never_generates_an_alert). Este
    test documenta el comportamiento de `_last_message` en sí mismo: solo
    excluye 'ignorado', así que un mensaje 'archivado' sigue sirviendo de cita
    en la tarjeta si algún día una persona vuelve de 'descartado'."""
    posted = {}

    def capture(blocks, text):
        posted["blocks"] = blocks
        return ("CHANDOFF", "1.1")

    monkeypatch.setattr(deliver.slack_writer, "post_card", capture)
    person = a_person()
    store_message("U1", "1700000000.000100", "mensaje archivado", "archivado")

    assert deliver.deliver_for(person["id"], FakeReader()) == "alta"
    text = json.dumps(posted["blocks"], ensure_ascii=False, default=str)
    assert "mensaje archivado" in text


# -- Hallazgo 1: las paradas del sistema (kill switch, tope mensual) deben
# cortar la entrega igual que cortan el research, sin perder el research ya
# hecho y sin propagar la excepción hacia el runner. config no está en
# TABLES_TO_CLEAN: cada test restaura lo que toca en un finally. --


def test_the_kill_switch_stops_delivery_without_losing_the_research(conn, monkeypatch):
    posted = []
    monkeypatch.setattr(deliver.slack_writer, "post_card", lambda blocks, text: posted.append(1))
    db.execute("update config set value = 'true'::jsonb where key = 'kill_switch'")
    try:
        person = a_person()
        assert deliver.deliver_for(person["id"], FakeReader()) is None
    finally:
        db.execute("update config set value = 'false'::jsonb where key = 'kill_switch'")

    assert posted == []
    assert db.fetch_one("select count(*) as n from deliveries")["n"] == 0
    action = db.fetch_one("select payload from agent_actions where action = 'entrega_detenida'")
    assert action is not None
    assert "KillSwitchActive" in action["payload"]["motivo"]


def test_the_monthly_cap_stops_delivery_without_losing_the_research(conn, monkeypatch):
    posted = []
    monkeypatch.setattr(deliver.slack_writer, "post_card", lambda blocks, text: posted.append(1))
    db.execute("update config set value = '1.0'::jsonb where key = 'monthly_budget_usd'")
    ledger.record_cost_event(source="test", cost_usd=2.00)
    try:
        person = a_person()
        assert deliver.deliver_for(person["id"], FakeReader()) is None
    finally:
        db.execute("update config set value = '150'::jsonb where key = 'monthly_budget_usd'")

    assert posted == []
    assert db.fetch_one("select count(*) as n from deliveries")["n"] == 0
    action = db.fetch_one("select payload from agent_actions where action = 'entrega_detenida'")
    assert action is not None
    assert "MonthlyBudgetExceeded" in action["payload"]["motivo"]


# -- Hallazgo 2: la reserva atómica en `deliveries` es la que decide quién
# entrega, no el read-then-insert de antes. --


def test_a_failed_post_leaves_no_reservation_so_a_later_attempt_can_deliver(conn, monkeypatch):
    def boom(blocks, text):
        raise RuntimeError("slack caído")

    monkeypatch.setattr(deliver.slack_writer, "post_card", boom)
    person = a_person()
    assert deliver.deliver_for(person["id"], FakeReader()) is None
    assert db.fetch_one("select count(*) as n from deliveries")["n"] == 0

    monkeypatch.setattr(deliver.slack_writer, "post_card", lambda blocks, text: ("CHANDOFF", "1.1"))
    assert deliver.deliver_for(person["id"], FakeReader()) == "alta"
    assert db.fetch_one("select count(*) as n from deliveries")["n"] == 1


def test_a_successful_post_whose_update_fails_still_prevents_a_second_post(
    conn, monkeypatch, caplog
):
    """Si el post a Slack sale bien pero el update que anota canal/ts falla
    (una base de datos que da un blip justo ahí), la reserva sigue viva: no
    puede publicarse una segunda tarjeta para la misma versión, y el fallo se
    registra en el log en vez de tragarse en silencio."""
    monkeypatch.setattr(deliver.slack_writer, "post_card", lambda blocks, text: ("CHANDOFF", "1.1"))
    person = a_person()

    real_execute = deliver.db.execute

    def flaky_execute(sql, params=()):
        if sql.strip().startswith("update deliveries"):
            raise RuntimeError("db caída justo aquí")
        return real_execute(sql, params)

    monkeypatch.setattr(deliver.db, "execute", flaky_execute)

    with caplog.at_level("ERROR"):
        assert deliver.deliver_for(person["id"], FakeReader()) == "alta"
    assert "no se pudo anotar la entrega" in caplog.text

    row = db.fetch_one("select channel_id, message_ts from deliveries")
    assert (row["channel_id"], row["message_ts"]) == (None, None)

    # Un segundo intento no debe volver a publicar: la reserva sigue viva.
    monkeypatch.setattr(deliver.db, "execute", real_execute)
    posted = []
    monkeypatch.setattr(deliver.slack_writer, "post_card", lambda blocks, text: posted.append(1))
    assert deliver.deliver_for(person["id"], FakeReader()) is None
    assert posted == []
    assert db.fetch_one("select count(*) as n from deliveries")["n"] == 1


def test_post_fails_and_delete_fails_records_both_actions_and_reservation_survives(
    conn, monkeypatch, caplog
):
    """Cuando el post a Slack falla Y el delete de la reserva también falla
    (un blip de base de datos justo en el manejador), la entrega no puede
    propagarse como excepción. Se registran tanto entrega_fallida como
    entrega_reserva_atascada, la reserva sigue viva (nunca se borró), y se
    registra en el log."""

    def post_fails(blocks, text):
        raise RuntimeError("slack caído")

    monkeypatch.setattr(deliver.slack_writer, "post_card", post_fails)
    person = a_person()

    real_execute = deliver.db.execute

    def flaky_execute(sql, params=()):
        if sql.strip().startswith("delete from deliveries"):
            raise RuntimeError("db caída en el delete")
        return real_execute(sql, params)

    monkeypatch.setattr(deliver.db, "execute", flaky_execute)

    with caplog.at_level("ERROR"):
        assert deliver.deliver_for(person["id"], FakeReader()) is None

    # No exception escaped
    assert "db caída en el delete" not in caplog.text or "exception" in caplog.text.lower()

    # Both actions recorded
    entrega_fallida = db.fetch_one(
        "select action from agent_actions where action = 'entrega_fallida'"
    )
    assert entrega_fallida is not None

    entrega_atascada = db.fetch_one(
        "select action, payload from agent_actions where action = 'entrega_reserva_atascada'"
    )
    assert entrega_atascada is not None

    # Reservation still alive
    reservation = db.fetch_one(
        "select count(*) as n from deliveries where prospect_id = %s", (person["id"],)
    )
    assert reservation["n"] == 1
