import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from handoff_agent import db, ledger
from handoff_agent.delivery import digest
from handoff_agent.tools import prospects
from tests.test_card import DOSSIER
from tests.test_deliver import a_person

NY = ZoneInfo("America/New_York")
TODAY = date(2026, 9, 16)
IN_WINDOW = datetime(2026, 9, 16, 12, 0, tzinfo=NY)
BEFORE_WINDOW = datetime(2026, 9, 15, 23, 0, tzinfo=NY)
AFTER_WINDOW = datetime(2026, 9, 17, 0, 0, tzinfo=NY)


@pytest.fixture(autouse=True)
def resend_configured(monkeypatch):
    """conftest borra RESEND_API_KEY/DIGEST_FROM/DIGEST_TO; cada test de
    send_daily que necesite llegar a _send_email los pone a mano."""
    monkeypatch.setenv("RESEND_API_KEY", "re_test_not_a_real_key")
    monkeypatch.setenv("DIGEST_FROM", "digest@example.com")
    monkeypatch.setenv("DIGEST_TO", "anthony@example.com")


def _set_created_at(prospect_id, when, version=None):
    if version is None:
        db.execute(
            "update dossiers set created_at = %s where prospect_id = %s", (when, prospect_id)
        )
    else:
        db.execute(
            "update dossiers set created_at = %s where prospect_id = %s and version = %s",
            (when, prospect_id, version),
        )


def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(digest, "_send_email", lambda subject, body: calls.append(subject))
    return calls


# -- build(): contenido y deduplicación --


def test_the_digest_lists_low_band_people(conn):
    person = a_person(score=1)
    _set_created_at(person["id"], IN_WINDOW)
    _, body = digest.build(TODAY)
    assert "Ada Ruiz" in body
    assert "señal baja" in body.lower()


def test_the_digest_lists_new_members_researched(conn):
    person = a_person(score=1)
    _set_created_at(person["id"], IN_WINDOW)
    db.execute(
        "insert into research_jobs (slack_user_id, reason, status) "
        "values ('U1', 'miembro_nuevo', 'hecho')"
    )
    _, body = digest.build(TODAY)
    assert "miembro nuevo" in body.lower()
    assert "Miembros nuevos investigados: 1" in body


def test_the_digest_shows_the_spend_of_the_day(conn):
    a_person()
    db.execute(
        "insert into llm_calls (stage, model, input_tokens, output_tokens, cost_usd, created_at) "
        "values ('research_sintesis', 'gpt-4.1', 100, 50, 0.0217, %s)",
        (IN_WINDOW,),
    )
    _, body = digest.build(TODAY)
    assert "0.02" in body


def test_high_band_people_are_not_repeated_in_the_digest(conn):
    """Ya recibió tarjeta (y quizás SMS): repetirlo en el email es ruido."""
    person = a_person(score=3)
    _set_created_at(person["id"], IN_WINDOW)
    _, body = digest.build(TODAY)
    assert "Ada Ruiz" not in body


def test_medium_band_is_not_a_low_signal_either(conn):
    person = a_person(score=2)
    _set_created_at(person["id"], IN_WINDOW)
    _, body = digest.build(TODAY)
    assert "Ada Ruiz" not in body
    assert "Señales de banda baja: 0" in body


def test_a_discarded_person_never_appears(conn):
    person = a_person(state="descartado", score=1)
    _set_created_at(person["id"], IN_WINDOW)
    _, body = digest.build(TODAY)
    assert "Ada Ruiz" not in body
    assert "Señales de banda baja: 0" in body


def test_a_new_member_in_high_band_is_counted_but_not_listed(conn):
    """Alta/media con tarjeta ya enviada: cuenta como nuevo miembro, pero no
    se repite con línea propia -- eso sería la misma señal dos veces."""
    person = a_person(score=3)
    _set_created_at(person["id"], IN_WINDOW)
    db.execute(
        "insert into research_jobs (slack_user_id, reason, status) "
        "values ('U1', 'miembro_nuevo', 'hecho')"
    )
    _, body = digest.build(TODAY)
    assert "Miembros nuevos investigados: 1" in body
    assert "Ada Ruiz" not in body


def test_no_duplicate_line_when_a_person_has_several_finished_research_jobs(conn):
    """El left join contra research_jobs del borrador multiplicaba la fila
    por cada tarea 'hecho' de la persona."""
    person = a_person(score=1)
    _set_created_at(person["id"], IN_WINDOW)
    db.execute(
        "insert into research_jobs (slack_user_id, reason, status) values ('U1', 'mensaje', 'hecho')"
    )
    db.execute(
        "insert into research_jobs (slack_user_id, reason, status) values ('U1', 'mensaje', 'hecho')"
    )
    _, body = digest.build(TODAY)
    assert body.count("Ada Ruiz") == 1


def test_no_duplicate_line_when_researched_twice_in_the_same_day(conn):
    """Dos dossiers de la misma persona el mismo día: solo el más reciente
    aparece, no una línea por versión."""
    person = a_person(score=1)
    _set_created_at(person["id"], IN_WINDOW - timedelta(hours=3), version=1)
    second = {
        **DOSSIER,
        "encaje_handoff": {"puntuacion": 1, "razon": "r2"},
        "resumen": "version dos",
    }
    prospects.save_dossier(person["id"], second, [])
    _set_created_at(person["id"], IN_WINDOW, version=2)

    _, body = digest.build(TODAY)
    assert body.count("Ada Ruiz") == 1
    assert "version dos" in body
    assert "contratando soporte" not in body


def test_the_window_is_half_open_in_the_configured_zone_not_utc(conn):
    person = a_person(score=1)
    start_of_day = datetime(2026, 9, 16, 0, 0, tzinfo=NY)
    _set_created_at(person["id"], start_of_day)
    _, body = digest.build(TODAY)
    assert "Ada Ruiz" in body

    _set_created_at(person["id"], AFTER_WINDOW)
    _, body = digest.build(TODAY)
    assert "Ada Ruiz" not in body

    _set_created_at(person["id"], BEFORE_WINDOW)
    _, body = digest.build(TODAY)
    assert "Ada Ruiz" not in body


# -- HTML escaping: nombre, empresa y resumen son texto de un tercero --


def test_names_and_company_are_html_escaped(conn):
    person = a_person(score=1)
    db.execute(
        "update prospects set full_name = %s, company_name = %s where id = %s",
        ("<script>alert(1)</script>", "<a href=evil>Co</a>", person["id"]),
    )
    _set_created_at(person["id"], IN_WINDOW)
    _, body = digest.build(TODAY)
    assert "<script>" not in body
    assert "&lt;script&gt;" in body
    assert "<a href=evil>" not in body
    assert "&lt;a href=evil&gt;" in body


def test_the_resumen_is_escaped_and_capped(conn):
    person = a_person(score=1)
    long_resumen = "<b>x</b> " * 100  # 900 caracteres crudos, con HTML
    db.execute(
        "update dossiers set content = jsonb_set(content, '{resumen}', to_jsonb(%s::text)) "
        "where prospect_id = %s",
        (long_resumen, person["id"]),
    )
    _set_created_at(person["id"], IN_WINDOW)
    _, body = digest.build(TODAY)
    assert "<b>" not in body
    assert "&lt;b&gt;" in body
    # Recortado tras colapsar espacios, mucho menos que las 100 repeticiones.
    assert body.count("&lt;b&gt;") < 40


# -- send_daily(): configuración, paradas del sistema, dedupe, ledger --


def test_an_empty_day_sends_nothing(conn, monkeypatch):
    calls = sent(monkeypatch)
    assert digest.send_daily(TODAY) == "nada"
    assert calls == []
    assert db.fetch_one("select count(*) as n from agent_actions")["n"] == 0


def test_send_daily_does_nothing_without_resend_configured(conn, monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    calls = sent(monkeypatch)
    assert digest.send_daily(TODAY) == "sin_configurar"
    assert calls == []
    assert db.fetch_one("select count(*) as n from agent_actions")["n"] == 0


def test_send_daily_does_nothing_without_digest_to(conn, monkeypatch):
    monkeypatch.delenv("DIGEST_TO", raising=False)
    calls = sent(monkeypatch)
    assert digest.send_daily(TODAY) == "sin_configurar"
    assert calls == []


def test_a_successful_send_writes_a_cost_event_and_an_action(conn, monkeypatch):
    calls = sent(monkeypatch)
    person = a_person(score=1)
    _set_created_at(person["id"], IN_WINDOW)

    assert digest.send_daily(TODAY) == "enviado"
    assert len(calls) == 1

    row = db.fetch_one("select source, cost_usd from cost_events")
    assert row["source"] == "resend_email"
    assert float(row["cost_usd"]) == 0.0

    action = db.fetch_one("select payload from agent_actions where action = 'digest_enviado'")
    assert action["payload"]["dia"] == "2026-09-16"


def test_the_resend_price_comes_from_settings(conn, monkeypatch):
    monkeypatch.setenv("PRICE_RESEND_PER_EMAIL", "0.0025")
    sent(monkeypatch)
    person = a_person(score=1)
    _set_created_at(person["id"], IN_WINDOW)
    digest.send_daily(TODAY)
    row = db.fetch_one("select cost_usd from cost_events")
    assert float(row["cost_usd"]) == 0.0025


def test_a_failed_send_does_not_write_a_cost_event(conn, monkeypatch):
    def boom(subject, body):
        raise RuntimeError("resend caído")

    monkeypatch.setattr(digest, "_send_email", boom)
    person = a_person(score=1)
    _set_created_at(person["id"], IN_WINDOW)

    assert digest.send_daily(TODAY) == "fallido"
    assert db.fetch_one("select count(*) as n from cost_events")["n"] == 0
    action = db.fetch_one("select payload from agent_actions where action = 'digest_fallido'")
    assert action["payload"] == {"motivo": "RuntimeError"}


def test_failure_payload_never_carries_the_request_or_str_exc(conn, monkeypatch, caplog):
    def boom(subject, body):
        request = httpx.Request(
            "POST", digest.RESEND_URL, headers={"Authorization": "Bearer re_super_secreto"}
        )
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

    monkeypatch.setattr(digest, "_send_email", boom)
    person = a_person(score=1)
    _set_created_at(person["id"], IN_WINDOW)

    with caplog.at_level("ERROR"):
        assert digest.send_daily(TODAY) == "fallido"

    action = db.fetch_one("select payload from agent_actions where action = 'digest_fallido'")
    assert action["payload"] == {"motivo": "HTTPStatusError", "status": 401}
    assert "re_super_secreto" not in caplog.text
    assert "Authorization" not in caplog.text


def test_the_digest_is_never_sent_twice_for_the_same_day(conn, monkeypatch):
    calls = sent(monkeypatch)
    person = a_person(score=1)
    _set_created_at(person["id"], IN_WINDOW)

    assert digest.send_daily(TODAY) == "enviado"
    assert digest.send_daily(TODAY) == "ya_enviado"
    assert len(calls) == 1
    assert db.fetch_one("select count(*) as n from cost_events")["n"] == 1


def test_send_daily_defaults_to_yesterday_in_the_configured_zone(conn, monkeypatch):
    """El cron corre a las 9:00 de Nueva York (13:00 UTC): el resumen tiene
    que ser el de ayer, no el del día en curso que casi no tiene datos."""
    calls = sent(monkeypatch)
    cron_run = datetime(2026, 9, 16, 9, 0, tzinfo=NY)
    yesterday_noon = datetime(2026, 9, 15, 12, 0, tzinfo=NY)
    person = a_person(score=1)
    _set_created_at(person["id"], yesterday_noon)

    assert digest.send_daily(now=cron_run) == "enviado"
    assert len(calls) == 1

    action = db.fetch_one("select payload from agent_actions where action = 'digest_enviado'")
    assert action["payload"]["dia"] == "2026-09-15"


def test_the_kill_switch_stops_the_digest(conn, monkeypatch):
    calls = sent(monkeypatch)
    person = a_person(score=1)
    _set_created_at(person["id"], IN_WINDOW)
    db.execute("update config set value = 'true'::jsonb where key = 'kill_switch'")
    try:
        status = digest.send_daily(TODAY)
    finally:
        db.execute("update config set value = 'false'::jsonb where key = 'kill_switch'")

    assert status == "detenido"
    assert calls == []
    action = db.fetch_one("select payload from agent_actions where action = 'digest_detenido'")
    assert action is not None
    assert "KillSwitchActive" in action["payload"]["motivo"]


def test_the_monthly_cap_stops_the_digest(conn, monkeypatch):
    calls = sent(monkeypatch)
    person = a_person(score=1)
    _set_created_at(person["id"], IN_WINDOW)
    db.execute("update config set value = '1.0'::jsonb where key = 'monthly_budget_usd'")
    ledger.record_cost_event(source="test", cost_usd=2.00)
    try:
        status = digest.send_daily(TODAY)
    finally:
        db.execute("update config set value = '150'::jsonb where key = 'monthly_budget_usd'")

    assert status == "detenido"
    assert calls == []
    action = db.fetch_one("select payload from agent_actions where action = 'digest_detenido'")
    assert action is not None
    assert "MonthlyBudgetExceeded" in action["payload"]["motivo"]


# -- _send_email(): nunca toca Resend de verdad --


@respx.mock
def test_send_email_posts_the_right_shape_to_resend(conn, monkeypatch):
    monkeypatch.setenv("DIGEST_TO", "anthony@example.com, other@example.com")
    route = respx.post(digest.RESEND_URL).mock(return_value=httpx.Response(200, json={"id": "x"}))

    digest._send_email("Asunto de prueba", "<p>cuerpo</p>")

    request = route.calls[0].request
    assert request.url == digest.RESEND_URL
    assert request.headers["Authorization"] == "Bearer re_test_not_a_real_key"
    assert json.loads(request.content) == {
        "from": "digest@example.com",
        "to": ["anthony@example.com", "other@example.com"],
        "subject": "Asunto de prueba",
        "html": "<p>cuerpo</p>",
    }


@respx.mock
def test_send_email_raises_on_an_http_error(conn):
    respx.post(digest.RESEND_URL).mock(return_value=httpx.Response(401, json={"message": "no"}))
    with pytest.raises(httpx.HTTPStatusError):
        digest._send_email("Asunto", "<p>cuerpo</p>")


def test_a_day_with_only_high_bands_still_reports_its_spend(conn, monkeypatch):
    bodies = []
    monkeypatch.setattr(digest, "_send_email", lambda subject, body: bodies.append(body))
    person = a_person(score=3)
    _set_created_at(person["id"], IN_WINDOW)
    db.execute(
        "insert into llm_calls (stage, model, input_tokens, output_tokens, cost_usd, created_at) "
        "values ('research_sintesis', 'gpt-4.1', 100, 50, 0.0217, %s)",
        (IN_WINDOW,),
    )
    assert digest.send_daily(TODAY) == "enviado"
    assert "0.02" in bodies[0]


def test_a_retry_after_the_database_fails_post_send_does_not_email_twice(conn, monkeypatch):
    """El email sale y la base falla al anotar el coste: el reintento del cron
    encuentra la marca escrita antes del envío y no repite."""
    calls = sent(monkeypatch)
    person = a_person(score=1)
    _set_created_at(person["id"], IN_WINDOW)

    def broken(*args, **kwargs):
        raise RuntimeError("base caída")

    working = digest.ledger.record_cost_event
    monkeypatch.setattr(digest.ledger, "record_cost_event", broken)
    assert digest.send_daily(TODAY) == "enviado"
    # No monkeypatch.undo(): desharía también el DATABASE_URL de conftest.
    monkeypatch.setattr(digest.ledger, "record_cost_event", working)
    assert digest.send_daily(TODAY) == "ya_enviado"
    assert len(calls) == 1


def test_a_failed_send_can_be_retried(conn, monkeypatch):
    def boom(subject, body):
        raise RuntimeError("resend caído")

    monkeypatch.setattr(digest, "_send_email", boom)
    person = a_person(score=1)
    _set_created_at(person["id"], IN_WINDOW)
    assert digest.send_daily(TODAY) == "fallido"

    calls = sent(monkeypatch)
    assert digest.send_daily(TODAY) == "enviado"
    assert len(calls) == 1
