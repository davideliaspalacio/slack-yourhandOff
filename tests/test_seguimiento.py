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


def test_a_case_already_reported_does_not_come_back(conn, slack):
    traza(de="otro", horas=30, prospect_id=persona())
    assert seguimiento.publicar_resumen() == 1
    assert avisado()
    # Mañana, con la conversación igual, ya no sale.
    db.execute("delete from agent_actions where action = %s", (seguimiento.ACCION_RESUMEN,))
    assert seguimiento.publicar_resumen() == 0
    assert len(slack) == 1


def test_a_change_in_the_conversation_brings_the_case_back(conn, slack):
    traza(de="otro", horas=30, prospect_id=persona())
    seguimiento.publicar_resumen()
    db.execute("delete from agent_actions where action = %s", (seguimiento.ACCION_RESUMEN,))
    # La otra persona insiste: el rastro cambia y el aviso se reinicia.
    db.execute("update dm_rastro set ultimo_ts = %s, avisado_ts = null", (ts(26),))
    assert seguimiento.publicar_resumen() == 1
    assert len(slack) == 2


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
