from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from handoff_agent import db, ledger
from handoff_agent.delivery import slack_writer, sms
from tests.test_deliver import a_person

NY = ZoneInfo("America/New_York")
MIDDAY = datetime(2026, 9, 16, 12, 0, tzinfo=NY)
NIGHT = datetime(2026, 9, 16, 23, 30, tzinfo=NY)


@pytest.fixture(autouse=True)
def twilio_configured(monkeypatch):
    """conftest borra las TWILIO_*; aquí se ponen valores falsos para que
    maybe_send llegue a _send, que cada test sustituye."""
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "not-a-real-token")
    monkeypatch.setenv("TWILIO_FROM", "+15550000000")
    monkeypatch.setenv("TWILIO_TO", "+15551111111")


def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(sms, "_send", lambda to, body: calls.append((to, body)) or "SM123")
    return calls


def a_slack_card(prospect_id: str, channel: str = "CHANDOFF", ts: str = "1700000000.000100"):
    db.execute(
        "insert into deliveries (prospect_id, kind, band, dossier_version, channel_id, "
        "message_ts) values (%s, 'slack', 'alta', 1, %s, %s)",
        (prospect_id, channel, ts),
    )


def test_a_high_band_person_gets_one_sms(conn, monkeypatch):
    calls = sent(monkeypatch)
    person = a_person()
    assert sms.maybe_send(person["id"], "alta", now=MIDDAY) is True
    assert len(calls) == 1
    assert "Ada Ruiz" in calls[0][1]
    row = db.fetch_one(
        "select kind, external_id, dossier_version from deliveries where kind = 'sms'"
    )
    assert row["external_id"] == "SM123"
    # Corrección 2: una fila 'sms' puede no traer versión de dossier -- solo
    # las de 'slack' la exigen.
    assert row["dossier_version"] is None


def test_medium_and_low_never_send_sms(conn, monkeypatch):
    calls = sent(monkeypatch)
    person = a_person(score=2)
    assert sms.maybe_send(person["id"], "media", now=MIDDAY) is False
    assert calls == []


def test_nothing_is_sent_at_night(conn, monkeypatch):
    calls = sent(monkeypatch)
    person = a_person()
    assert sms.maybe_send(person["id"], "alta", now=NIGHT) is False
    assert calls == []


def four_people() -> list[str]:
    """a_person() siempre usa U1; se renombra antes de crear la siguiente
    para que sean cuatro personas distintas."""
    ids = []
    for i in range(4):
        person = a_person()
        db.execute(
            "update prospects set slack_user_id = %s where id = %s", (f"U-{i}", person["id"])
        )
        ids.append(person["id"])
    return ids


def test_the_daily_cap_holds_across_different_people(conn, monkeypatch):
    calls = sent(monkeypatch)
    ids = four_people()
    assert len(set(ids)) == 4
    results = [sms.maybe_send(pid, "alta", now=MIDDAY) for pid in ids]
    assert results == [True, True, True, False]
    assert len(calls) == 3


def test_the_daily_cap_resets_at_new_york_midnight_not_utc(conn, monkeypatch):
    """20:30 en Nueva York ya es el día siguiente en UTC. Los tres SMS de esa
    tarde siguen contando para ese día; a las 07:00 del día siguiente hay cupo."""
    calls = sent(monkeypatch)
    ids = four_people()
    evening = datetime(2026, 9, 16, 20, 30, tzinfo=NY)
    for pid in ids[:3]:
        db.execute(
            "insert into deliveries (prospect_id, kind, band, created_at) "
            "values (%s, 'sms', 'alta', %s)",
            (pid, evening),
        )
    assert sms.maybe_send(ids[3], "alta", now=datetime(2026, 9, 16, 20, 45, tzinfo=NY)) is False
    assert sms.maybe_send(ids[3], "alta", now=datetime(2026, 9, 17, 7, 0, tzinfo=NY)) is True
    assert len(calls) == 1


def test_quiet_hours_edges(conn, monkeypatch):
    calls = sent(monkeypatch)
    person = a_person()
    assert (
        sms.maybe_send(person["id"], "alta", now=datetime(2026, 9, 16, 21, 0, tzinfo=NY)) is False
    )
    assert (
        sms.maybe_send(person["id"], "alta", now=datetime(2026, 9, 16, 6, 59, tzinfo=NY)) is False
    )
    assert sms.maybe_send(person["id"], "alta", now=datetime(2026, 9, 16, 7, 0, tzinfo=NY)) is True
    assert len(calls) == 1


def test_nothing_is_sent_while_twilio_is_not_configured(conn, monkeypatch):
    calls = sent(monkeypatch)
    monkeypatch.delenv("TWILIO_TO")
    person = a_person()
    assert sms.maybe_send(person["id"], "alta", now=MIDDAY) is False
    assert calls == []
    # No es un fallo: no queda sms_fallido por cada señal alta.
    assert db.fetch_one("select count(*) as n from agent_actions")["n"] == 0


def test_a_discarded_person_never_gets_an_sms(conn, monkeypatch):
    calls = sent(monkeypatch)
    person = a_person(state="descartado")
    assert sms.maybe_send(person["id"], "alta", now=MIDDAY) is False
    assert calls == []


def test_a_contacted_person_never_gets_an_sms_either(conn, monkeypatch):
    calls = sent(monkeypatch)
    person = a_person(state="contactado")
    assert sms.maybe_send(person["id"], "alta", now=MIDDAY) is False
    assert calls == []


def test_a_long_third_party_name_is_trimmed(conn, monkeypatch):
    calls = sent(monkeypatch)
    person = a_person()
    db.execute(
        "update prospects set full_name = %s, company_name = %s where id = %s",
        ("N" * 500, "C" * 500, person["id"]),
    )
    assert sms.maybe_send(person["id"], "alta", now=MIDDAY) is True
    assert len(calls[0][1]) < 100


def test_every_sms_lands_in_the_cost_ledger(conn, monkeypatch):
    sent(monkeypatch)
    person = a_person()
    sms.maybe_send(person["id"], "alta", now=MIDDAY)
    row = db.fetch_one("select source, cost_usd from cost_events")
    assert row["source"] == "twilio_sms"
    # Corrección 2: el precio sale de Settings (PRICE_TWILIO_PER_SMS), no de
    # una constante en el módulo.
    assert float(row["cost_usd"]) == 0.0079


def test_the_price_comes_from_settings_not_a_hardcoded_constant(conn, monkeypatch):
    sent(monkeypatch)
    monkeypatch.setenv("PRICE_TWILIO_PER_SMS", "0.02")
    person = a_person()
    sms.maybe_send(person["id"], "alta", now=MIDDAY)
    row = db.fetch_one("select cost_usd from cost_events")
    assert float(row["cost_usd"]) == 0.02


def test_twilio_failing_never_breaks_the_pipeline(conn, monkeypatch):
    def boom(to, body):
        raise RuntimeError("twilio caído")

    monkeypatch.setattr(sms, "_send", boom)
    person = a_person()
    assert sms.maybe_send(person["id"], "alta", now=MIDDAY) is False
    assert db.fetch_one("select count(*) as n from deliveries where kind = 'sms'")["n"] == 0


# -- Corrección 1: el enlace es el permalink real que da Slack
# (chat.getPermalink), no una URL de app.slack.com armada a mano -- esa no
# abre la tarjeta. --


def test_the_sms_carries_the_real_permalink_of_the_card(conn, monkeypatch):
    calls = sent(monkeypatch)
    person = a_person()
    a_slack_card(person["id"])
    monkeypatch.setattr(
        slack_writer,
        "card_permalink",
        lambda channel, ts: "https://acme.slack.com/archives/CHANDOFF/p1700000000000100",
    )
    assert sms.maybe_send(person["id"], "alta", now=MIDDAY) is True
    assert "https://acme.slack.com/archives/CHANDOFF/p1700000000000100" in calls[0][1]


def test_the_sms_still_goes_out_without_a_link_when_the_permalink_is_unavailable(conn, monkeypatch):
    calls = sent(monkeypatch)
    person = a_person()
    a_slack_card(person["id"])
    monkeypatch.setattr(slack_writer, "card_permalink", lambda channel, ts: None)
    assert sms.maybe_send(person["id"], "alta", now=MIDDAY) is True
    assert len(calls) == 1
    assert "http" not in calls[0][1]


def test_the_sms_goes_out_without_a_link_when_there_is_no_card_yet(conn, monkeypatch):
    """No debería pasar en la práctica (el SMS solo se dispara tras entregar
    la tarjeta), pero maybe_send no debe reventar si no encuentra ninguna."""
    calls = sent(monkeypatch)
    person = a_person()
    assert sms.maybe_send(person["id"], "alta", now=MIDDAY) is True
    assert len(calls) == 1
    assert "http" not in calls[0][1]


# -- Corrección 3: las paradas del sistema (kill switch, tope mensual) cortan
# el SMS igual que cortan el research y la entrega de la tarjeta. config no
# está en TABLES_TO_CLEAN: cada test restaura lo que toca en un finally. --


def test_the_kill_switch_stops_the_sms(conn, monkeypatch):
    calls = sent(monkeypatch)
    db.execute("update config set value = 'true'::jsonb where key = 'kill_switch'")
    try:
        person = a_person()
        assert sms.maybe_send(person["id"], "alta", now=MIDDAY) is False
    finally:
        db.execute("update config set value = 'false'::jsonb where key = 'kill_switch'")

    assert calls == []
    assert db.fetch_one("select count(*) as n from deliveries where kind = 'sms'")["n"] == 0
    action = db.fetch_one("select payload from agent_actions where action = 'sms_detenido'")
    assert action is not None
    assert "KillSwitchActive" in action["payload"]["motivo"]


def test_the_monthly_cap_stops_the_sms(conn, monkeypatch):
    calls = sent(monkeypatch)
    db.execute("update config set value = '1.0'::jsonb where key = 'monthly_budget_usd'")
    ledger.record_cost_event(source="test", cost_usd=2.00)
    try:
        person = a_person()
        assert sms.maybe_send(person["id"], "alta", now=MIDDAY) is False
    finally:
        db.execute("update config set value = '150'::jsonb where key = 'monthly_budget_usd'")

    assert calls == []
    assert db.fetch_one("select count(*) as n from deliveries where kind = 'sms'")["n"] == 0
    action = db.fetch_one("select payload from agent_actions where action = 'sms_detenido'")
    assert action is not None
    assert "MonthlyBudgetExceeded" in action["payload"]["motivo"]
