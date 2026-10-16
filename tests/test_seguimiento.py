"""El resumen diario de las conversaciones que se enfrían: los tres casos, los
umbrales, el no repetirse, los frenos y -- sobre todo -- que nunca cuenta lo
que se dijeron."""

import json
import time

import pytest

from handoff_agent import db, guards
from handoff_agent.delivery import seguimiento

SECRETO = "mándame el contrato firmado"


@pytest.fixture(autouse=True)
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

    monkeypatch.setattr(seguimiento.slack_writer, "post_card", post_card)
    return posted


def ts(horas: float) -> str:
    return f"{time.time() - horas * 3600:.6f}"


def persona(slack_user_id="USUNNY", nombre="Sunny Patel", empresa="Acme", estado="nuevo"):
    fila = db.fetch_one(
        "insert into prospects (slack_user_id, full_name, company_name, state) "
        "values (%s, %s, %s, %s) returning id",
        (slack_user_id, nombre, empresa, estado),
    )
    return fila["id"]


def traza(dm_id="DAAA", user="USUNNY", de="otro", horas=30.0, prospect_id=None, team="T0OWNER"):
    """Una conversación en el rastro. Si no se pasa `prospect_id` se crea la ficha: el
    seguimiento solo recuerda conversaciones con gente ya investigada, así que un rastro
    sin ficha no produce ningún aviso y no sirve para probar nada."""
    if prospect_id is None:
        prospect_id = db.fetch_one(
            "insert into prospects (slack_user_id, full_name, company_name) "
            "values (%s, %s, 'Acme') on conflict (slack_user_id) do update "
            "set full_name = excluded.full_name returning id",
            (user, f"Persona {user}"),
        )["id"]
    db.execute(
        "insert into dm_rastro (dm_id, team_id, slack_user_id, prospect_id, ultimo_ts, ultimo_de) "
        "values (%s, %s, %s, %s, %s, %s)",
        (dm_id, team, user, prospect_id, ts(horas), de),
    )
    return dm_id


def acciones(nombre):
    return db.fetch_all("select * from agent_actions where action = %s", (nombre,))


def avisado(dm_id="DAAA"):
    fila = db.fetch_one("select avisado_ts, ultimo_ts from dm_rastro where dm_id = %s", (dm_id,))
    return fila["avisado_ts"] == fila["ultimo_ts"]


# --- los tres casos --------------------------------------------------------------


def test_they_replied_and_he_has_not(conn):
    traza(de="otro", horas=30, prospect_id=persona())
    (caso,) = seguimiento.casos()
    assert caso.tipo == seguimiento.RESPONDER
    assert (caso.nombre, caso.empresa, caso.dias) == ("Sunny Patel", "Acme", 1)
    assert seguimiento.linea(caso) == (
        "• *Sunny Patel* — Acme · Sunny replied 1 day ago · "
        "<https://app.slack.com/client/T0OWNER/DAAA|Open in Slack>"
    )


def test_he_wrote_and_nobody_answered(conn):
    traza(de="dueno", horas=24 * 5, prospect_id=persona())
    (caso,) = seguimiento.casos()
    assert caso.tipo == seguimiento.SIN_RESPUESTA
    assert "you wrote 5 days ago, no answer yet" in seguimiento.linea(caso)


def test_marked_contacted_with_no_conversation_at_all(conn):
    persona(estado="contactado")
    db.execute("update prospects set updated_at = now() - interval '6 days'")
    (caso,) = seguimiento.casos()
    assert caso.tipo == seguimiento.SIN_CONVERSACION
    assert caso.dm_id is None
    assert "marked contacted 6 days ago, no conversation in Slack" in seguimiento.linea(caso)
    # Sin DM no hay enlace que dar.
    assert "app.slack.com" not in seguimiento.linea(caso)


def test_a_contacted_person_with_a_conversation_is_not_a_case(conn):
    pid = persona(estado="contactado")
    db.execute("update prospects set updated_at = now() - interval '6 days'")
    traza(de="otro", horas=1, prospect_id=pid)  # contestó hace una hora: aún no toca
    assert seguimiento.casos() == []


def test_the_thresholds_come_from_config(conn, restore_config):
    traza("DAAA", "USUNNY", de="otro", horas=5)
    traza("DBBB", "UOTHER", de="dueno", horas=30)
    assert seguimiento.casos() == []  # 5h < 24h y 30h < 3 días
    db.execute(
        "update config set value = '4'::jsonb where key = 'seguimiento_horas_para_responder'"
    )
    db.execute("update config set value = '1'::jsonb where key = 'seguimiento_dias_sin_respuesta'")
    assert {c.tipo for c in seguimiento.casos()} == {
        seguimiento.RESPONDER,
        seguimiento.SIN_RESPUESTA,
    }


def test_a_discarded_or_test_person_never_gets_a_reminder(conn):
    pid = persona("UD", "Dis Carded", estado="descartado")
    traza("DAAA", "UD", de="otro", horas=30, prospect_id=pid)
    pid2 = persona("UT", "Test Person")
    db.execute("update prospects set sin_tarjeta = true where id = %s", (pid2,))
    traza("DBBB", "UT", de="otro", horas=30, prospect_id=pid2)
    assert seguimiento.casos() == []


def test_the_most_urgent_come_first_and_the_cap_applies(conn, restore_config):
    db.execute("update config set value = '2'::jsonb where key = 'seguimiento_max_por_resumen'")
    traza("DAAA", "UA", de="dueno", horas=24 * 20)  # muy viejo, pero él no debe nada
    traza("DBBB", "UB", de="otro", horas=30)
    traza("DCCC", "UC", de="otro", horas=24 * 4)
    lista = seguimiento.casos()
    # Primero lo que espera su respuesta, y dentro de eso lo más viejo.
    assert [(c.tipo, c.dm_id) for c in lista] == [
        (seguimiento.RESPONDER, "DCCC"),
        (seguimiento.RESPONDER, "DBBB"),
    ]


# --- lo que el aviso nunca dice --------------------------------------------------


def test_the_summary_never_quotes_a_message(conn, slack):
    pid = persona()
    traza(de="otro", horas=30, prospect_id=pid)
    # El texto del DM no está en la base (el rastro no lo guarda); esto
    # comprueba que tampoco puede colarse por ninguna otra vía.
    db.execute("update prospects set research_notes = %s where id = %s", (SECRETO, pid))
    assert seguimiento.publicar_resumen() == 1
    (resumen,) = slack
    assert SECRETO not in resumen["json"]
    assert "contrato" not in resumen["json"]
    assert resumen["text"] == "Follow-ups: 1 conversation waiting"


def test_the_rendered_summary_groups_the_three_cases(conn, slack):
    traza("DAAA", "USUNNY", de="otro", horas=30, prospect_id=persona())
    traza("DBBB", "UBOB", de="dueno", horas=24 * 4, prospect_id=persona("UBOB", "Bob Lee", "Beta"))
    persona("UCLEO", "Cleo Diaz", "Gamma", estado="contactado")
    db.execute(
        "update prospects set updated_at = now() - interval '9 days' where full_name = 'Cleo Diaz'"
    )
    assert seguimiento.publicar_resumen() == 3
    texto = slack[0]["blocks"][0]["text"]["text"]
    assert texto.splitlines() == [
        "⏰ *FOLLOW-UPS*",
        "",
        "*They replied, you haven't*",
        (
            "• *Sunny Patel* — Acme · Sunny replied 1 day ago · "
            "<https://app.slack.com/client/T0OWNER/DAAA|Open in Slack>"
        ),
        "",
        "*No answer yet*",
        (
            "• *Bob Lee* — Beta · you wrote 4 days ago, no answer yet · "
            "<https://app.slack.com/client/T0OWNER/DBBB|Open in Slack>"
        ),
        "",
        "*Marked contacted, no conversation*",
        "• *Cleo Diaz* — Gamma · marked contacted 9 days ago, no conversation in Slack",
    ]


def test_a_name_from_slack_cannot_break_the_block(conn, slack):
    pid = persona("UX", "<!channel> *Ada* & co", "Acme <https://evil|click>")
    traza(de="otro", horas=30, prospect_id=pid)
    seguimiento.publicar_resumen()
    texto = slack[0]["blocks"][0]["text"]["text"]
    assert "<!channel>" not in texto
    assert "&lt;!channel&gt;" in texto
    # El único enlace del resumen es el de la conversación.
    assert texto.count("<https://app.slack.com/client/T0OWNER/DAAA|Open in Slack>") == 1
    assert "evil" in texto and "<https://evil" not in texto


def test_without_the_workspace_code_the_line_has_no_link(conn, slack):
    traza(team=None, de="otro", horas=30, prospect_id=persona())
    seguimiento.publicar_resumen()
    assert "app.slack.com" not in slack[0]["json"]


# --- no repetirse ----------------------------------------------------------------


def test_a_pending_conversation_is_in_every_days_summary(conn, slack):
    """El resumen es la foto del día, no un aviso: un pendiente sigue saliendo mañana.

    Antes de 0029 salía una sola vez. Cambió a propósito: desde que existen los avisos
    inmediatos, que son los que no se repiten hasta pasadas unas horas, el resumen tiene
    el otro trabajo -- decir todo lo que está esperando ahora mismo. Si descontara lo ya
    avisado, con recordatorios cada pocas horas saldría vacío todos los días."""
    traza(de="otro", horas=30, prospect_id=persona())
    assert seguimiento.publicar_resumen() == 1
    assert avisado()
    # Mañana, con la conversación igual de parada, vuelve a salir.
    db.execute("delete from agent_actions where action = %s", (seguimiento.ACCION_RESUMEN,))
    assert seguimiento.publicar_resumen() == 1
    assert len(slack) == 2


def test_a_conversation_that_got_an_answer_leaves_the_summary(conn, slack):
    """Lo que deja de estar parado deja de salir: es la otra cara de la foto del día."""
    traza(de="otro", horas=30, prospect_id=persona())
    assert seguimiento.publicar_resumen() == 1
    db.execute("delete from agent_actions where action = %s", (seguimiento.ACCION_RESUMEN,))
    # Anthony contesta: ahora el último mensaje es suyo y es reciente.
    db.execute("update dm_rastro set ultimo_ts = %s, ultimo_de = 'dueno'", (ts(1),))
    assert seguimiento.publicar_resumen() == 0
    assert len(slack) == 1


def test_the_contacted_case_comes_back_only_if_the_person_changes(conn, slack):
    persona(estado="contactado")
    db.execute("update prospects set updated_at = now() - interval '6 days'")
    assert seguimiento.publicar_resumen() == 1
    assert len(acciones(seguimiento.ACCION_AVISADO)) == 1
    db.execute("delete from agent_actions where action = %s", (seguimiento.ACCION_RESUMEN,))
    assert seguimiento.publicar_resumen() == 0
    # Alguien la volvió a marcar contactada DESPUÉS de aquel aviso: el caso
    # está vivo otra vez en cuanto vuelven a pasar los días de espera.
    db.execute(
        "update agent_actions set created_at = now() - interval '9 days' where action = %s",
        (seguimiento.ACCION_AVISADO,),
    )
    db.execute("update prospects set updated_at = now() - interval '7 days'")
    assert seguimiento.publicar_resumen() == 1


def test_only_one_summary_a_day(conn, slack):
    traza("DAAA", "UA", de="otro", horas=30)
    assert seguimiento.publicar_resumen() == 1
    traza("DBBB", "UB", de="otro", horas=30)
    assert seguimiento.publicar_resumen() == 0
    assert len(slack) == 1


def test_with_nothing_to_say_it_does_not_post_and_keeps_the_day_free(conn, slack):
    assert seguimiento.publicar_resumen() == 0
    assert slack == []
    assert acciones(seguimiento.ACCION_RESUMEN) == []
    traza(de="otro", horas=30)
    assert seguimiento.publicar_resumen() == 1


# --- frenos ----------------------------------------------------------------------


def test_with_the_switch_off_it_does_nothing(conn, slack, restore_config):
    db.execute("update config set value = 'false'::jsonb where key = 'seguimiento_activo'")
    traza(de="otro", horas=30)
    assert seguimiento.publicar_resumen() == 0
    assert slack == []
    assert acciones(seguimiento.ACCION_RESUMEN) == []


def test_the_kill_switch_stops_it_and_leaves_a_trace(conn, slack, restore_config):
    db.execute(
        "insert into config (key, value) values ('kill_switch', 'true'::jsonb) "
        "on conflict (key) do update set value = 'true'::jsonb"
    )
    traza(de="otro", horas=30)
    assert seguimiento.publicar_resumen() == 0
    assert slack == []
    (parada,) = acciones(seguimiento.ACCION_PARADA)
    assert "KillSwitchActive" in parada["payload"]["motivo"]
    assert not avisado()


def test_the_monthly_cap_stops_it_and_leaves_a_trace(conn, slack, restore_config):
    db.execute(
        "insert into config (key, value) values ('monthly_budget_usd', '1'::jsonb) "
        "on conflict (key) do update set value = '1'::jsonb"
    )
    db.execute(
        "insert into llm_calls (stage, model, input_tokens, cost_usd) values ('s', 'm', 1, 5)"
    )
    traza(de="otro", horas=30)
    assert seguimiento.publicar_resumen() == 0
    (parada,) = acciones(seguimiento.ACCION_PARADA)
    assert "MonthlyBudgetExceeded" in parada["payload"]["motivo"]


def test_a_slack_failure_is_recorded_and_does_not_raise(conn, slack, monkeypatch):
    def boom(blocks, text):
        raise RuntimeError("slack caído")

    monkeypatch.setattr(seguimiento.slack_writer, "post_card", boom)
    traza(de="otro", horas=30)
    assert seguimiento.publicar_resumen() == 0
    (fallo,) = acciones(seguimiento.ACCION_FALLIDO)
    assert "slack caído" in fallo["payload"]["motivo"]
    # El caso no se marca: mañana vuelve a intentarse.
    assert not avisado()


def test_without_a_bot_token_nothing_is_published_and_the_day_is_kept(conn, slack, monkeypatch):
    monkeypatch.delenv("HANDOFF_SLACK_BOT_TOKEN")
    traza(de="otro", horas=30)
    assert seguimiento.publicar_resumen() == 0
    assert acciones(seguimiento.ACCION_RESUMEN) == []


def test_an_unexpected_failure_never_escapes(conn, monkeypatch):
    monkeypatch.setattr(
        seguimiento, "casos", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert seguimiento.publicar_resumen() == 0


def test_the_guards_are_the_shared_ones(conn):
    """Si alguien cambiara los frenos por otros, esto lo nota."""
    assert seguimiento.guards is guards


# ---------------------------------------------------------------------------------
# El aviso inmediato (0029): llega en cuanto cruza y vuelve cada pocas horas.
# ---------------------------------------------------------------------------------


def avisado_at(dm_id="DAAA"):
    return db.fetch_one("select avisado_at from dm_rastro where dm_id = %s", (dm_id,))["avisado_at"]


def atrasar_aviso(horas: float, dm_id="DAAA") -> None:
    """Como si el último aviso hubiera salido hace N horas."""
    db.execute(
        "update dm_rastro set avisado_at = now() - make_interval(hours => %s) where dm_id = %s",
        (horas, dm_id),
    )


def test_an_alert_goes_out_as_soon_as_it_crosses(conn, slack):
    """No espera al resumen del día: la conversación ya pasó de su plazo y sale ya."""
    traza(prospect_id=persona())
    assert seguimiento.publicar_avisos() == 1
    assert len(slack) == 1
    assert "WAITING ON YOU" in slack[0]["json"]
    assert "Sunny Patel" in slack[0]["json"]


def test_an_alert_has_its_own_header_so_it_is_not_the_daily_summary(conn, slack):
    traza(prospect_id=persona())
    seguimiento.publicar_avisos()
    seguimiento.publicar_resumen()
    # Dos mensajes distintos, y se distinguen de un golpe en el canal.
    assert "WAITING ON YOU" in slack[0]["json"]
    assert "FOLLOW-UPS" in slack[1]["json"]


def test_a_conversation_inside_its_threshold_is_not_alerted(conn, slack):
    traza(horas=2.0, prospect_id=persona())
    assert seguimiento.publicar_avisos() == 0
    assert slack == []


def test_it_does_not_repeat_before_the_reminder_window(conn, slack):
    traza(prospect_id=persona())
    assert seguimiento.publicar_avisos() == 1
    assert seguimiento.publicar_avisos() == 0
    assert len(slack) == 1


def test_it_comes_back_after_the_reminder_window(conn, slack, restore_config):
    traza(prospect_id=persona())
    seguimiento.publicar_avisos()
    atrasar_aviso(seguimiento.RECORDAR_HORAS + 1)
    assert seguimiento.publicar_avisos() == 1
    assert len(slack) == 2


def test_the_reminder_window_comes_from_config(conn, slack, restore_config):
    db.execute("update config set value = '48'::jsonb where key = 'seguimiento_recordar_horas'")
    traza(prospect_id=persona())
    seguimiento.publicar_avisos()
    # Cuatro horas ya no bastan: ahora el recordatorio es a las 48.
    atrasar_aviso(5)
    assert seguimiento.publicar_avisos() == 0
    atrasar_aviso(49)
    assert seguimiento.publicar_avisos() == 1


def test_the_daily_cap_stops_the_flood(conn, slack, restore_config):
    db.execute("update config set value = '1'::jsonb where key = 'seguimiento_avisos_por_dia'")
    traza(dm_id="DAAA", user="U1", prospect_id=persona("U1", "Uno"))
    traza(dm_id="DBBB", user="U2", prospect_id=persona("U2", "Dos"))
    assert seguimiento.publicar_avisos() == 1
    # El cupo del día está gastado, aunque la otra conversación siga esperando.
    atrasar_aviso(seguimiento.RECORDAR_HORAS + 1, "DAAA")
    assert seguimiento.publicar_avisos() == 0


def test_the_daily_summary_is_still_the_safety_net(conn, slack, restore_config):
    """Con el cupo gastado, lo que no se avisó sigue saliendo en el resumen del día."""
    db.execute("update config set value = '0'::jsonb where key = 'seguimiento_avisos_por_dia'")
    traza(prospect_id=persona())
    assert seguimiento.publicar_avisos() == 0
    assert seguimiento.publicar_resumen() == 1
    assert "Sunny Patel" in slack[0]["json"]


def test_a_new_message_restarts_the_clock(conn, slack):
    """Si alguno de los dos escribe, la conversación entra otra vez desde cero."""
    traza(prospect_id=persona())
    seguimiento.publicar_avisos()
    db.execute("update dm_rastro set ultimo_ts = %s where dm_id = 'DAAA'", (ts(30.0),))
    atrasar_aviso(seguimiento.RECORDAR_HORAS + 1)
    assert seguimiento.publicar_avisos() == 1


def test_an_alert_never_quotes_a_message(conn, slack):
    """La garantía de siempre: el texto de un DM no está guardado y no puede salir."""
    traza(prospect_id=persona())
    seguimiento.publicar_avisos()
    assert SECRETO not in slack[0]["json"]


def test_a_discarded_person_is_never_alerted(conn, slack):
    traza(prospect_id=persona(estado="descartado"))
    assert seguimiento.publicar_avisos() == 0


def test_with_the_switch_off_no_alerts_go_out(conn, slack, restore_config):
    db.execute("update config set value = 'false'::jsonb where key = 'seguimiento_activo'")
    traza(prospect_id=persona())
    assert seguimiento.publicar_avisos() == 0
    assert slack == []


def test_the_kill_switch_stops_the_alerts(conn, slack, restore_config):
    db.execute("update config set value = 'true'::jsonb where key = 'kill_switch'")
    traza(prospect_id=persona())
    assert seguimiento.publicar_avisos() == 0
    assert acciones(seguimiento.ACCION_PARADA)


def test_a_failed_post_does_not_mark_the_case_as_alerted(conn, slack, monkeypatch):
    """Si Slack falla, la conversación no se marca: vuelve en la pasada siguiente."""
    traza(prospect_id=persona())

    def boom(blocks, text):
        raise RuntimeError("slack caído")

    monkeypatch.setattr(seguimiento.slack_writer, "post_card", boom)
    assert seguimiento.publicar_avisos() == 0
    assert avisado_at() is None
    assert acciones(seguimiento.ACCION_FALLIDO)


def test_alerting_never_raises(conn, slack, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("base caída")

    monkeypatch.setattr(seguimiento, "_casos_para_avisar", boom)
    assert seguimiento.publicar_avisos() == 0


def test_a_conversation_with_someone_we_never_researched_is_not_a_reminder(conn, slack):
    """Sin ficha no hay nombre ni empresa: la línea salía con el código de Slack
    repetido dos veces. El seguimiento es para leads, no para toda su agenda del club."""
    db.execute(
        "insert into dm_rastro (dm_id, team_id, slack_user_id, ultimo_ts, ultimo_de) "
        "values ('DZZZ', 'T0OWNER', 'UDESCONOCIDO', %s, 'otro')",
        (ts(30.0),),
    )
    assert seguimiento.casos() == []
    assert seguimiento.publicar_resumen() == 0
    assert seguimiento.publicar_avisos() == 0


def test_a_conversation_older_than_the_retention_window_is_dropped(conn, slack, restore_config):
    """Con la retención en 10 días, lo de hace tres meses ni se guarda ni se recuerda."""
    db.execute("update config set value = '10'::jsonb where key = 'seguimiento_retencion_dias'")
    traza(horas=24 * 83)
    # Sigue en la tabla hasta que la limpieza pase, pero el recordatorio es lo que
    # importa: nadie quiere que le recuerden algo de hace 83 días.
    assert seguimiento.publicar_avisos() <= 1
