"""Publicación de las alertas de señales: frenos, paradas y registro."""

import json
import time
from datetime import UTC, datetime

import pytest

from handoff_agent import db, guards, llm
from handoff_agent.delivery import deliver
from handoff_agent.signals import alertas
from handoff_agent.signals.procesar import procesar_pendientes as detector_real
from tests.slack_fakes import FakeReader
from tests.test_deliver import a_person
from tests.test_senales import FakeOpenAI, mensaje, persona


@pytest.fixture(autouse=True)
def slack(monkeypatch):
    """Bot configurado y `post_card` sustituido: lo publicado queda en la lista.
    El detector se sustituye por defecto: las señales de estos tests se insertan
    ya detectadas (el camino con detector real tiene su propio test)."""
    monkeypatch.setenv("HANDOFF_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("HANDOFF_SLACK_CHANNEL_ID", "CHANDOFF")
    posted = []

    def post_card(blocks, text):
        posted.append(
            {"blocks": blocks, "text": text, "json": json.dumps(blocks, ensure_ascii=False)}
        )
        return "CHANDOFF", f"1700000000.{len(posted):06d}"

    monkeypatch.setattr(alertas.slack_writer, "post_card", post_card)
    monkeypatch.setattr(alertas.procesar, "procesar_pendientes", list)
    return posted


def senal(
    user="U1",
    horas=0.1,
    tipo="soporte",
    cita="we're hiring a support rep",
    confianza="alta",
    channel="C1",
) -> str:
    """Un mensaje con su señal ya detectada, de hace `horas` horas. Devuelve el ts."""
    ts = f"{time.time() - horas * 3600:.6f}"
    mensaje(ts, user, cita, channel=channel)
    db.execute(
        "insert into message_signals (channel_id, ts, detectada, tipo, cita, confianza) "
        "values (%s, %s, true, %s, %s, %s)",
        (channel, ts, tipo, cita, confianza),
    )
    return ts


def estado(ts, channel="C1"):
    return db.fetch_one(
        "select alerta_estado, alerta_motivo, alerta_channel_id, alerta_ts, alerta_intentos "
        "from message_signals where channel_id = %s and ts = %s",
        (channel, ts),
    )


def acciones(nombre):
    return db.fetch_all("select * from agent_actions where action = %s", (nombre,))


def test_each_detected_signal_is_posted_and_marked_published(conn, slack):
    ts = senal()
    assert alertas.publicar_pendientes() == 1
    (alerta,) = slack
    assert "THEY ASKED · support" in alerta["json"]
    assert "we're hiring a support rep" in alerta["json"]
    assert alerta["text"].startswith("They asked: support")
    fila = estado(ts)
    assert fila["alerta_estado"] == "publicada"
    assert (fila["alerta_channel_id"], fila["alerta_ts"]) == ("CHANDOFF", "1700000000.000001")
    (accion,) = acciones("alerta_publicada")
    assert accion["payload"]["ts"] == "1700000000.000001"


def test_a_published_signal_is_never_published_twice(conn, slack):
    senal()
    assert alertas.publicar_pendientes() == 1
    assert alertas.publicar_pendientes() == 0
    assert len(slack) == 1


def test_a_known_person_gets_name_company_and_dossier_line(conn, slack):
    person = a_person(state="investigado", score=3)
    db.execute("update prospects set company_name = 'Acme' where id = %s", (person["id"],))
    senal(user="U1")
    alertas.publicar_pendientes()
    texto = slack[0]["json"]
    assert "Ada Ruiz" in texto and "Acme" in texto
    assert "Dossier: fit 3/3 · Researched" in texto
    assert str(person["id"]) in texto  # el value de los botones


def test_without_a_dossier_the_card_says_research_is_on_its_way(conn, slack):
    persona("U1", state="nuevo")
    senal(user="U1")
    alertas.publicar_pendientes()
    assert "Research is on its way" in slack[0]["json"]


def test_the_original_message_button_uses_the_readers_permalink(conn, slack):
    ts = senal()
    alertas.publicar_pendientes(FakeReader())
    assert f"https://fake.slack.com/archives/C1/p{ts.replace('.', '')}" in slack[0]["json"]
    assert "View original message" in slack[0]["json"]


def test_a_permalink_that_fails_only_loses_the_button(conn, slack):
    class Broken(FakeReader):
        def permalink(self, channel, ts):
            raise RuntimeError("slack down")

    senal()
    assert alertas.publicar_pendientes(Broken()) == 1
    assert "View original message" not in slack[0]["json"]


# -- edad máxima ------------------------------------------------------------------


def test_a_message_older_than_the_limit_is_skipped_for_good(conn, slack):
    viejo = senal(horas=25)
    nuevo = senal(user="U2", horas=1)
    assert alertas.publicar_pendientes() == 1
    assert len(slack) == 1
    fila = estado(viejo)
    assert (fila["alerta_estado"], fila["alerta_motivo"]) == ("omitida", "too_old")
    assert estado(nuevo)["alerta_estado"] == "publicada"


def test_the_first_start_does_not_flood_the_channel_with_history(conn, slack):
    for i in range(12):
        senal(user=f"U{i}", horas=30 + i)
    assert alertas.publicar_pendientes() == 0
    assert slack == []
    assert (
        db.fetch_one("select count(*) as n from message_signals where alerta_motivo = 'too_old'")[
            "n"
        ]
        == 12
    )


def test_the_age_limit_comes_from_config(conn, slack, restore_config):
    db.execute("update config set value = '2'::jsonb where key = 'alertas_edad_maxima_horas'")
    senal(user="U1", horas=3)
    senal(user="U2", horas=1)
    assert alertas.publicar_pendientes() == 1


# -- tope diario ------------------------------------------------------------------


def test_the_daily_cap_limits_the_alerts_and_the_rest_wait(conn, slack):
    tses = [senal(user=f"U{i}", horas=1 + i / 10) for i in range(7)]
    assert alertas.publicar_pendientes() == 5
    assert alertas.publicar_pendientes() == 0
    assert len(slack) == 5
    # Las dos que no cupieron siguen pendientes (no se pierden mientras no caduquen).
    pendientes = [t for t in tses if estado(t)["alerta_estado"] is None]
    assert len(pendientes) == 2


def test_yesterdays_alerts_do_not_count_toward_todays_cap(conn, slack):
    for i in range(5):
        ts = senal(user=f"V{i}", horas=40)
        db.execute(
            "update message_signals set alerta_estado = 'publicada', alerta_channel_id = 'CH', "
            "alerta_ts = '1.0', alerta_at = now() - interval '2 days' where ts = %s",
            (ts,),
        )
    senal(user="U1")
    assert alertas.publicar_pendientes() == 1


def test_the_daily_cap_comes_from_config(conn, slack, restore_config):
    db.execute("update config set value = '2'::jsonb where key = 'alertas_por_dia'")
    for i in range(4):
        senal(user=f"U{i}")
    assert alertas.publicar_pendientes() == 2


def test_when_the_cap_is_short_the_most_confident_signals_go_first(conn, slack, restore_config):
    db.execute("update config set value = '1'::jsonb where key = 'alertas_por_dia'")
    senal(user="U1", confianza="baja", horas=0.1)
    alta = senal(user="U2", confianza="alta", horas=2)
    alertas.publicar_pendientes()
    assert estado(alta)["alerta_estado"] == "publicada"


# -- una por persona ----------------------------------------------------------------


def test_one_alert_per_person_inside_the_period(conn, slack):
    primera = senal(user="U1", horas=2, cita="we need support help")
    segunda = senal(user="U1", horas=1, cita="and a bookkeeper too")
    assert alertas.publicar_pendientes() == 1
    assert len(slack) == 1
    published = [t for t in (primera, segunda) if estado(t)["alerta_estado"] == "publicada"]
    omitted = [t for t in (primera, segunda) if estado(t)["alerta_estado"] == "omitida"]
    assert len(published) == len(omitted) == 1
    assert estado(omitted[0])["alerta_motivo"] == "same_person_window"


def test_the_same_person_is_alerted_again_after_the_period(conn, slack):
    viejo = senal(user="U1", horas=30)
    db.execute(
        "update message_signals set alerta_estado = 'publicada', alerta_channel_id = 'CH', "
        "alerta_ts = '1.0', alerta_at = now() - interval '8 days' where ts = %s",
        (viejo,),
    )
    senal(user="U1", horas=1)
    assert alertas.publicar_pendientes() == 1


def test_the_same_person_inside_the_period_is_not_alerted_again(conn, slack):
    viejo = senal(user="U1", horas=30)
    db.execute(
        "update message_signals set alerta_estado = 'publicada', alerta_channel_id = 'CH', "
        "alerta_ts = '1.0', alerta_at = now() - interval '3 days' where ts = %s",
        (viejo,),
    )
    nuevo = senal(user="U1", horas=1)
    assert alertas.publicar_pendientes() == 0
    assert estado(nuevo)["alerta_motivo"] == "same_person_window"


def test_the_period_comes_from_config(conn, slack, restore_config):
    db.execute("update config set value = '1'::jsonb where key = 'alertas_dias_misma_persona'")
    viejo = senal(user="U1", horas=30)
    db.execute(
        "update message_signals set alerta_estado = 'publicada', alerta_channel_id = 'CH', "
        "alerta_ts = '1.0', alerta_at = now() - interval '3 days' where ts = %s",
        (viejo,),
    )
    senal(user="U1", horas=1)
    assert alertas.publicar_pendientes() == 1


# -- estados de la persona ------------------------------------------------------------


def test_a_discarded_person_never_gets_an_alert(conn, slack):
    persona("U1", state="descartado")
    ts = senal(user="U1")
    assert alertas.publicar_pendientes() == 0
    assert slack == []
    assert estado(ts)["alerta_motivo"] == "discarded"


def test_a_contacted_person_does_get_an_alert_with_the_buttons(conn, slack):
    a_person(state="contactado", score=3)
    senal(user="U1")
    assert alertas.publicar_pendientes() == 1
    assert "fit 3/3 · Contacted" in slack[0]["json"]
    assert '"action_id": "contactado"' in slack[0]["json"]


def test_a_person_in_test_mode_gets_no_alert(conn, slack):
    persona("U1")
    db.execute("update prospects set sin_tarjeta = true where slack_user_id = 'U1'")
    ts = senal(user="U1")
    assert alertas.publicar_pendientes() == 0
    assert estado(ts)["alerta_motivo"] == "test_mode"


def test_someone_who_is_not_a_prospect_yet_still_gets_the_alert(conn, slack):
    senal(user="UNEW")
    assert alertas.publicar_pendientes() == 1
    assert '"action_id": "contactado"' not in slack[0]["json"]
    assert "Research is on its way" in slack[0]["json"]


# -- paradas del sistema -------------------------------------------------------------


def test_the_kill_switch_stops_the_alerts_and_is_recorded(conn, slack, restore_config):
    db.execute("update config set value = 'true'::jsonb where key = 'kill_switch'")
    ts = senal()
    assert alertas.publicar_pendientes() == 0
    assert slack == []
    assert estado(ts)["alerta_estado"] is None  # sigue pendiente
    (accion,) = acciones("alertas_parada")
    assert "KillSwitchActive" in accion["result"]["motivo"]


def test_the_monthly_cap_stops_the_alerts_and_is_recorded(conn, slack, monkeypatch):
    def over():
        raise guards.MonthlyBudgetExceeded("spent $150 of $150")

    monkeypatch.setattr(alertas.guards, "check_monthly_budget", over)
    senal()
    assert alertas.publicar_pendientes() == 0
    assert slack == []
    assert len(acciones("alertas_parada")) == 1


def test_alerts_off_in_config_publishes_nothing(conn, slack, restore_config):
    db.execute("update config set value = 'false'::jsonb where key = 'senales_activas'")
    senal()
    assert alertas.publicar_pendientes() == 0
    assert slack == []


def test_without_a_bot_configured_nothing_is_attempted_or_burned(conn, slack, monkeypatch):
    """Sin dónde publicar ni siquiera se llama al detector (cada mensaje cuesta
    una llamada al LLM), y tampoco se escribe una fila de ledger por ciclo."""
    llamadas = []

    def espia():
        llamadas.append(1)
        raise AssertionError("el detector no debe llamarse sin bot configurado")

    monkeypatch.setattr(alertas.procesar, "procesar_pendientes", espia)
    monkeypatch.delenv("HANDOFF_SLACK_BOT_TOKEN")
    ts = senal()
    acciones_antes = db.fetch_one("select count(*) as n from agent_actions")["n"]
    assert alertas.publicar_pendientes() == 0
    assert alertas.publicar_pendientes() == 0
    assert llamadas == []
    assert slack == []
    assert estado(ts)["alerta_estado"] is None
    assert acciones("alerta_fallida") == []
    assert db.fetch_one("select count(*) as n from agent_actions")["n"] == acciones_antes


def test_without_a_channel_configured_the_detector_is_not_called_either(conn, slack, monkeypatch):
    def espia():
        raise AssertionError("el detector no debe llamarse sin canal configurado")

    monkeypatch.setattr(alertas.procesar, "procesar_pendientes", espia)
    monkeypatch.delenv("HANDOFF_SLACK_CHANNEL_ID")
    assert alertas.publicar_pendientes() == 0


# -- fallos --------------------------------------------------------------------------


def failing(monkeypatch):
    def boom(blocks, text):
        raise RuntimeError("slack is down")

    monkeypatch.setattr(alertas.slack_writer, "post_card", boom)


def test_a_slack_failure_never_propagates_and_the_alert_is_retried(conn, slack, monkeypatch):
    ts = senal()
    failing(monkeypatch)
    assert alertas.publicar_pendientes() == 0
    fila = estado(ts)
    assert (fila["alerta_estado"], fila["alerta_intentos"]) == (None, 1)
    (accion,) = acciones("alerta_fallida")
    assert "slack is down" in accion["payload"]["motivo"]
    # Slack vuelve: la misma señal sale en la vuelta siguiente.
    monkeypatch.setattr(
        alertas.slack_writer, "post_card", lambda blocks, text: ("CHANDOFF", "1700000000.000009")
    )
    assert alertas.publicar_pendientes() == 1
    assert estado(ts)["alerta_estado"] == "publicada"


def test_a_signal_that_keeps_failing_is_given_up_after_three_attempts(conn, slack, monkeypatch):
    ts = senal()
    failing(monkeypatch)
    for _ in range(5):
        assert alertas.publicar_pendientes() == 0
    fila = estado(ts)
    assert (fila["alerta_estado"], fila["alerta_motivo"]) == ("omitida", "publish_failed")
    assert len(acciones("alerta_fallida")) == 3


def test_a_failure_stops_the_batch_so_slack_is_not_hammered(conn, slack, monkeypatch):
    for i in range(3):
        senal(user=f"U{i}")
    calls = []

    def boom(blocks, text):
        calls.append(1)
        raise RuntimeError("down")

    monkeypatch.setattr(alertas.slack_writer, "post_card", boom)
    alertas.publicar_pendientes()
    assert calls == [1]


def test_a_detector_failure_still_publishes_what_was_already_detected(conn, slack, monkeypatch):
    def boom():
        raise RuntimeError("openai down")

    monkeypatch.setattr(alertas.procesar, "procesar_pendientes", boom)
    senal()
    assert alertas.publicar_pendientes() == 1


def test_an_unexpected_database_error_is_swallowed(conn, slack, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(alertas.db, "fetch_one", boom)
    monkeypatch.setattr(alertas.db_config.db, "fetch_one", boom)
    assert alertas.publicar_pendientes() == 0


def test_a_failure_noting_the_result_never_duplicates_the_alert(conn, slack, monkeypatch):
    """La alerta ya salió pero no se pudo anotar: queda 'reservada' y no vuelve a salir."""
    ts = senal()
    real_execute = db.execute

    def execute(sql, params=()):
        if "alerta_estado = 'publicada'" in sql:
            raise RuntimeError("db blip")
        return real_execute(sql, params)

    with monkeypatch.context() as parcial:
        parcial.setattr(alertas.db, "execute", execute)
        assert alertas.publicar_pendientes() == 1
    assert estado(ts)["alerta_estado"] == "reservada"
    assert alertas.publicar_pendientes() == 0


# -- con el detector de verdad ----------------------------------------------------------


def test_a_message_goes_from_slack_to_an_alert_through_the_real_detector(conn, slack, monkeypatch):
    monkeypatch.setattr(alertas.procesar, "procesar_pendientes", detector_real)
    posted = []
    monkeypatch.setattr(
        alertas.slack_writer, "post_card", lambda b, t: posted.append(b) or ("CHANDOFF", "1.1")
    )
    monkeypatch.setattr(llm, "_client", lambda: FakeOpenAI())
    mensaje(f"{time.time() - 60:.6f}", "U9", "we are hiring a support agent")
    mensaje(f"{time.time() - 30:.6f}", "U8", "congrats on the launch!")
    assert alertas.publicar_pendientes() == 1
    assert len(posted) == 1
    assert db.fetch_one("select count(*) as n from message_signals")["n"] == 2


# -- lo anterior sigue igual ---------------------------------------------------------------


def test_alerts_do_not_touch_deliveries_and_research_cards_still_go_out(conn, slack, monkeypatch):
    person = a_person(state="contactado", score=3)
    db.execute("update prospects set state = 'investigado' where id = %s", (person["id"],))
    senal(user="U1")
    assert alertas.publicar_pendientes() == 1
    assert db.fetch_one("select count(*) as n from deliveries")["n"] == 0

    cards = []
    monkeypatch.setattr(
        deliver.slack_writer, "post_card", lambda blocks, text: cards.append(text) or ("CH", "2.2")
    )
    assert deliver.deliver_for(person["id"], FakeReader()) == "alta"
    row = db.fetch_one("select kind, channel_id, message_ts from deliveries")
    assert (row["kind"], row["channel_id"], row["message_ts"]) == ("slack", "CH", "2.2")
    assert cards and cards[0].startswith("High signal")
    # Y la señal sigue figurando como publicada, aparte de la tarjeta.
    assert (
        db.fetch_one("select count(*) as n from message_signals where alerta_estado = 'publicada'")[
            "n"
        ]
        == 1
    )


# -- respuestas en hilos (0032) -------------------------------------------------------------


def principal_hilo(user="UP", horas=3, texto="Anyone recommend a closer agency?", channel="C1"):
    """El mensaje principal de un hilo, sin señal. Devuelve su ts (= thread_ts)."""
    ts = f"{time.time() - horas * 3600:.6f}"
    db.execute(
        "insert into slack_messages (channel_id, ts, user_id, text, thread_ts, status) "
        "values (%s, %s, %s, %s, %s, 'pendiente_scoring')",
        (channel, ts, user, texto, ts),
    )
    return ts


def respuesta_hilo(
    thread_ts, user, horas, cita="we need the same, 2 closers", confianza="alta", channel="C1"
) -> str:
    """Una respuesta en el hilo con su señal ya detectada. Devuelve su ts."""
    ts = f"{time.time() - horas * 3600:.6f}"
    db.execute(
        "insert into slack_messages (channel_id, ts, user_id, text, thread_ts, status) "
        "values (%s, %s, %s, %s, %s, 'pendiente_scoring')",
        (channel, ts, user, cita, thread_ts),
    )
    db.execute(
        "insert into message_signals (channel_id, ts, detectada, tipo, cita, confianza, thread_ts) "
        "values (%s, %s, true, 'ventas', %s, %s, %s)",
        (channel, ts, cita, confianza, thread_ts),
    )
    return ts


def test_a_thread_reply_without_high_confidence_never_alerts(conn, slack):
    hilo = principal_hilo()
    media = respuesta_hilo(hilo, "U1", 0.5, confianza="media")
    baja = respuesta_hilo(hilo, "U2", 0.4, confianza="baja")
    assert alertas.publicar_pendientes() == 0
    assert slack == []
    for ts in (media, baja):
        fila = estado(ts)
        assert (fila["alerta_estado"], fila["alerta_motivo"]) == (
            "omitida",
            "thread_low_confidence",
        )


def test_a_top_level_signal_with_medium_confidence_still_alerts(conn, slack):
    """Regresión: la exigencia de confianza alta es solo para los hilos."""
    ts = senal(confianza="media")
    assert alertas.publicar_pendientes() == 1
    assert estado(ts)["alerta_estado"] == "publicada"


def test_three_replies_in_one_thread_become_one_alert_with_three_names(conn, slack):
    hilo = principal_hilo(texto="Anyone recommend a closer agency? Need help fast")
    tses = [
        respuesta_hilo(hilo, "U1", 0.6, cita="we need the same, 2 closers"),
        respuesta_hilo(hilo, "U2", 0.5, cita="we're looking for this too"),
        respuesta_hilo(hilo, "U3", 0.4, cita="same problem, need 3 SDRs"),
    ]
    assert alertas.publicar_pendientes() == 1
    (alerta,) = slack
    texto = alerta["json"]
    assert "In a thread:" in texto
    assert "Anyone recommend a closer agency? Need help fast" in texto
    for nombre, cita in (
        ("U1", "we need the same, 2 closers"),
        ("U2", "we're looking for this too"),
        ("U3", "same problem, need 3 SDRs"),
    ):
        assert f"*{nombre}*" in texto and cita in texto
    # El orden es el del hilo: la primera que respondió va primero.
    assert texto.index("*U1*") < texto.index("*U2*") < texto.index("*U3*")
    assert alerta["text"] == "They asked in a thread: sales · U1 +2"
    # Las tres señales quedan con la misma alerta.
    filas = [estado(ts) for ts in tses]
    assert {(f["alerta_estado"], f["alerta_channel_id"], f["alerta_ts"]) for f in filas} == {
        ("publicada", "CHANDOFF", "1700000000.000001")
    }
    (accion,) = acciones("alerta_publicada")
    assert accion["payload"]["hilo"] == hilo and len(accion["payload"]["senal_ids"]) == 3


def test_the_same_person_replying_twice_is_one_entry(conn, slack):
    hilo = principal_hilo()
    primera = respuesta_hilo(hilo, "U1", 0.6, cita="we need the same")
    segunda = respuesta_hilo(hilo, "U1", 0.5, cita="actually we need 4 of them")
    otra = respuesta_hilo(hilo, "U2", 0.4, cita="we need this too")
    assert alertas.publicar_pendientes() == 1
    texto = slack[0]["json"]
    assert texto.count("*U1*") == 1
    assert "we need the same" in texto and "actually we need 4" not in texto
    assert estado(primera)["alerta_estado"] == estado(otra)["alerta_estado"] == "publicada"
    fila = estado(segunda)
    assert (fila["alerta_estado"], fila["alerta_motivo"]) == ("omitida", "thread_same_person")
    # Y en la vuelta siguiente no sale nada más de ese hilo.
    assert alertas.publicar_pendientes() == 0


def test_a_grouped_alert_counts_once_against_the_daily_cap(conn, slack, restore_config):
    db.execute("update config set value = '2'::jsonb where key = 'alertas_por_dia'")
    hilo = principal_hilo()
    for i, horas in enumerate((0.6, 0.5, 0.4)):
        respuesta_hilo(hilo, f"U{i}", horas, cita=f"we need {i + 2} closers")
    suelta = senal(user="V1", horas=1)
    espera = senal(user="V2", horas=2)
    # El hilo (tres personas) y una suelta: dos alertas, no cuatro.
    assert alertas.publicar_pendientes() == 2
    assert len(slack) == 2
    assert estado(suelta)["alerta_estado"] == "publicada"
    assert estado(espera)["alerta_estado"] is None
    assert alertas._publicadas_desde(alertas._inicio_del_dia(datetime.now(UTC))) == 2
    assert alertas.publicar_pendientes() == 0


def test_a_reserved_group_also_counts_once(conn, slack):
    """Atascada tras publicar (sin `alerta_ts`), la reserva de un grupo sigue
    contando una vez: sus filas comparten `alerta_at`."""
    hilo = principal_hilo()
    ids = [
        db.fetch_one("select id::text as id from message_signals where ts = %s", (ts,))["id"]
        for ts in (respuesta_hilo(hilo, "U1", 0.6), respuesta_hilo(hilo, "U2", 0.5))
    ]
    assert alertas._reservar_grupo(ids) == set(ids)
    assert alertas._publicadas_desde(alertas._inicio_del_dia(datetime.now(UTC))) == 1


def test_the_parent_author_is_not_alerted_again_for_their_own_reply(conn, slack, restore_config):
    # Sin ventana por persona, para que la regla del hilo sea la única que actúa.
    db.execute("update config set value = '0'::jsonb where key = 'alertas_dias_misma_persona'")
    hilo = principal_hilo(user="UP", texto="We need 2 SDRs, any agency?")
    db.execute(
        "insert into message_signals (channel_id, ts, detectada, tipo, cita, confianza, "
        "alerta_estado, alerta_channel_id, alerta_ts, alerta_at) "
        "values ('C1', %s, true, 'ventas', 'We need 2 SDRs', 'alta', 'publicada', 'CH', '1.0', "
        "now() - interval '1 hour')",
        (hilo,),
    )
    propia = respuesta_hilo(hilo, "UP", 0.5, cita="still need them!")
    ajena = respuesta_hilo(hilo, "U2", 0.4, cita="we need the same")
    assert alertas.publicar_pendientes() == 1
    assert "*UP*" not in slack[0]["json"] and "*U2*" in slack[0]["json"]
    assert estado(propia)["alerta_motivo"] == "thread_same_person"
    assert estado(ajena)["alerta_estado"] == "publicada"


def test_one_alert_per_person_per_thread_across_rounds(conn, slack, restore_config):
    db.execute("update config set value = '0'::jsonb where key = 'alertas_dias_misma_persona'")
    hilo = principal_hilo()
    respuesta_hilo(hilo, "U1", 0.6, cita="we need the same")
    assert alertas.publicar_pendientes() == 1
    otra_vez = respuesta_hilo(hilo, "U1", 0.3, cita="we still need it")
    assert alertas.publicar_pendientes() == 0
    assert estado(otra_vez)["alerta_motivo"] == "thread_same_person"


def test_the_per_person_window_still_applies_inside_a_thread(conn, slack):
    viejo = senal(user="U1", horas=30)
    db.execute(
        "update message_signals set alerta_estado = 'publicada', alerta_channel_id = 'CH', "
        "alerta_ts = '1.0', alerta_at = now() - interval '3 days' where ts = %s",
        (viejo,),
    )
    hilo = principal_hilo()
    u1 = respuesta_hilo(hilo, "U1", 0.6)
    respuesta_hilo(hilo, "U2", 0.5)
    assert alertas.publicar_pendientes() == 1
    assert "*U1*" not in slack[0]["json"] and "*U2*" in slack[0]["json"]
    assert estado(u1)["alerta_motivo"] == "same_person_window"


def test_a_discarded_or_test_mode_replier_is_left_out_of_the_group(conn, slack):
    persona("U1", state="descartado")
    persona("U2")
    db.execute("update prospects set sin_tarjeta = true where slack_user_id = 'U2'")
    hilo = principal_hilo()
    u1 = respuesta_hilo(hilo, "U1", 0.6)
    u2 = respuesta_hilo(hilo, "U2", 0.5)
    assert alertas.publicar_pendientes() == 0
    assert slack == []
    assert estado(u1)["alerta_motivo"] == "discarded"
    assert estado(u2)["alerta_motivo"] == "test_mode"


def test_at_most_five_names_and_the_rest_wait_for_the_next_round(conn, slack):
    hilo = principal_hilo()
    tses = [respuesta_hilo(hilo, f"U{i}", 0.9 - i / 10, cita=f"we need {i} reps") for i in range(7)]
    # La primera vuelta saca cinco; las dos que no cupieron siguen pendientes.
    assert alertas.publicar_pendientes() == 1
    assert [estado(ts)["alerta_estado"] for ts in tses] == ["publicada"] * 5 + [None] * 2
    # Y salen en la vuelta siguiente, en otra alerta del mismo hilo.
    assert alertas.publicar_pendientes() == 1
    primera, segunda = slack
    assert [f"*U{i}*" in primera["json"] for i in range(7)] == [True] * 5 + [False] * 2
    assert "*U5*" in segunda["json"] and "*U6*" in segunda["json"]
    assert all(estado(ts)["alerta_estado"] == "publicada" for ts in tses)


def test_each_person_gets_a_link_to_their_own_reply(conn, slack):
    hilo = principal_hilo()
    u1 = respuesta_hilo(hilo, "U1", 0.6)
    u2 = respuesta_hilo(hilo, "U2", 0.5)
    alertas.publicar_pendientes(FakeReader())
    texto = slack[0]["json"]
    for ts in (u1, u2):
        assert f"https://fake.slack.com/archives/C1/p{ts.replace('.', '')}" in texto
    assert texto.count("View reply") == 2
    # Ningún botón que repinte la tarjeta y se lleve los de los demás.
    assert '"action_id": "contactado"' not in texto


def test_a_slack_failure_releases_the_whole_group_and_it_goes_out_once(conn, slack, monkeypatch):
    hilo = principal_hilo()
    tses = [respuesta_hilo(hilo, "U1", 0.6), respuesta_hilo(hilo, "U2", 0.5)]
    failing(monkeypatch)
    assert alertas.publicar_pendientes() == 0
    for ts in tses:
        assert (estado(ts)["alerta_estado"], estado(ts)["alerta_intentos"]) == (None, 1)
    posted = []
    monkeypatch.setattr(
        alertas.slack_writer,
        "post_card",
        lambda blocks, text: posted.append(text) or ("CHANDOFF", "1700000000.000009"),
    )
    assert alertas.publicar_pendientes() == 1
    assert alertas.publicar_pendientes() == 0
    assert len(posted) == 1
    assert {estado(ts)["alerta_ts"] for ts in tses} == {"1700000000.000009"}


def test_a_failure_noting_a_group_never_duplicates_it(conn, slack, monkeypatch):
    hilo = principal_hilo()
    tses = [respuesta_hilo(hilo, "U1", 0.6), respuesta_hilo(hilo, "U2", 0.5)]
    real_execute = db.execute

    def execute(sql, params=()):
        if "alerta_estado = 'publicada'" in sql:
            raise RuntimeError("db blip")
        return real_execute(sql, params)

    with monkeypatch.context() as parcial:
        parcial.setattr(alertas.db, "execute", execute)
        assert alertas.publicar_pendientes() == 1
    assert {estado(ts)["alerta_estado"] for ts in tses} == {"reservada"}
    assert alertas.publicar_pendientes() == 0
    assert len(slack) == 1


def test_a_thread_goes_from_slack_to_one_alert_through_the_real_detector(conn, slack, monkeypatch):
    """De punta a punta: el principal, un consejo y dos "we need" en el hilo. El
    detector solo da señal a quien dice necesitarlo, y sale una sola alerta."""
    from tests.test_hilos_senales import modelo_de_hilos

    monkeypatch.setattr(alertas.procesar, "procesar_pendientes", detector_real)
    monkeypatch.setattr(llm, "_client", lambda: FakeOpenAI(modelo_de_hilos))
    hilo = principal_hilo(user="UP", texto="Anyone recommend a closer agency?")
    for user, horas, texto in (
        ("U1", 0.5, "Talk to Acme Sales, they're great"),
        ("U2", 0.4, "we need the same"),
        ("U3", 0.3, "we need closers too"),
    ):
        db.execute(
            "insert into slack_messages (channel_id, ts, user_id, text, thread_ts, status) "
            "values ('C1', %s, %s, %s, %s, 'pendiente_scoring')",
            (f"{time.time() - horas * 3600:.6f}", user, texto, hilo),
        )
    assert alertas.publicar_pendientes() == 1
    texto = slack[0]["json"]
    assert "In a thread:" in texto and "Anyone recommend a closer agency?" in texto
    assert "*U2*" in texto and "*U3*" in texto and "*U1*" not in texto
    assert (
        db.fetch_one("select count(*) as n from message_signals where thread_ts = %s", (hilo,))["n"]
        == 3
    )
