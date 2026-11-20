"""El reporte semanal de empresas contratando (0040).

Lo que cuidan estos tests:

1. Que liste lo que AG pidió: las empresas que solo contratan, una línea por empresa,
   lo más accionable arriba, con tope y con los dos enlaces.
2. Que sea UN mensaje a la semana: el día y la hora configurados, reservando antes, y
   nada si no hay nada que listar.
3. Que no suelte un atraso: solo su ventana de días, solo su día, sin repetir empresas.
4. Que quien sale aquí no salga además como pendiente "act now" en el resumen del día.
5. Que lo que venga de un perfil ajeno no haga daño, y que nada de esto lance.
"""

import json
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from handoff_agent import db, llm
from handoff_agent.delivery import prioridad, reporte_contratando, resumen_prioridad, slack_writer
from tests.test_panel_rls import ALLOWED, STRANGER, as_user

# Lunes 16 de noviembre de 2026, las 10 de la mañana en Nueva York (15:00 UTC).
LUNES = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
NUMERICAS = (
    "tarjeta_dias_misma_persona",
    "research_reintento_horas",
    "research_reintentos_max",
    "reporte_contratando_dia",
    "reporte_contratando_hora",
    "reporte_contratando_dias",
    "reporte_contratando_max",
)
INTERRUPTORES = ("vacantes_solo_reporte", "reporte_contratando_activo")


@pytest.fixture(autouse=True)
def slack(monkeypatch):
    """Bot configurado y `post_card` sustituido: lo publicado queda en la lista."""
    monkeypatch.setenv("HANDOFF_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("HANDOFF_SLACK_CHANNEL_ID", "CHANDOFF")
    monkeypatch.setenv("PANEL_URL", "https://panel.example")
    posted = []

    def post_card(blocks, text):
        posted.append(
            {"blocks": blocks, "text": text, "json": json.dumps(blocks, ensure_ascii=False)}
        )
        return "CHANDOFF", f"1700000000.{len(posted):06d}"

    monkeypatch.setattr(slack_writer, "post_card", post_card)
    monkeypatch.setattr(
        slack_writer,
        "card_permalink",
        lambda canal, ts: f"https://handoff.slack.com/archives/{canal}/p{ts.replace('.', '')}",
    )
    return posted


def ajuste(clave, valor):
    db.execute("update config set value = %s::jsonb where key = %s", (json.dumps(valor), clave))


def valor(clave):
    return db.fetch_one("select value from config where key = %s", (clave,))["value"]


def ficha(
    user="UADA",
    nombre="Ada Ruiz",
    empresa="Acme",
    *,
    cargo="COO",
    nota=3,
    evidencia="vacantes",
    vacantes=4,
    cubiertos=("Support Lead", "Executive Assistant"),
    otros=("Plant Manager",),
    dominio=None,
    estado="investigado",
    hace_dias=1.0,
    fuente="https://www.linkedin.com/in/ada-ruiz",
    ahora=LUNES,
):
    """Una persona con su último dossier, guardado `hace_dias` antes de `ahora`."""
    pid = db.fetch_one(
        "insert into prospects (slack_user_id, full_name, company_name, company_domain, state) "
        "values (%s, %s, %s, %s, %s) returning id::text as id",
        (user, nombre, empresa, dominio, estado),
    )["id"]
    contenido = {
        "persona": {"nombre": nombre, "cargo": cargo, "fuente": fuente},
        "empresa": {"nombre": empresa, "dominio": dominio},
        "contratacion": {
            "vacantes_abiertas": vacantes,
            "roles": [*cubiertos, *otros],
            "roles_deslocalizables": list(cubiertos),
        },
        "encaje_handoff": {"puntuacion": nota, "evidencia": evidencia, "razon": "r"},
    }
    db.execute(
        "insert into dossiers (prospect_id, version, content, sources, created_at) "
        "values (%s, 1, %s, '[]'::jsonb, %s)",
        (pid, json.dumps(contenido), ahora - timedelta(days=hace_dias)),
    )
    return pid


def lineas(publicado) -> list[str]:
    texto = "\n".join(
        b["text"]["text"] for b in publicado["blocks"] if b["type"] == "section"
    ).splitlines()
    return [linea for linea in texto if linea.startswith("• ")]


@pytest.fixture
def listo(conn, restore_config):
    return conn


# ---------------------------------------------------------------------------------
# Qué dice
# ---------------------------------------------------------------------------------


def test_the_defaults_of_the_migration(conn):
    assert valor("vacantes_solo_reporte") is True
    assert valor("reporte_contratando_activo") is True
    assert valor("reporte_contratando_dia") == 1
    assert valor("reporte_contratando_hora") == 9
    assert valor("reporte_contratando_dias") == 7
    assert valor("reporte_contratando_max") == 15
    assert valor("tarjeta_dias_misma_persona") == 7
    assert valor("research_reintento_horas") == 24
    assert valor("research_reintentos_max") == 2


def test_the_report_as_he_sees_it(listo, slack):
    pid = ficha()
    assert reporte_contratando.publicar(LUNES) == 1
    publicado = slack[0]
    cabecera = publicado["blocks"][0]["text"]["text"].splitlines()
    assert cabecera[0] == "⚪ *FYI* · 🏢 *WEEKLY HIRING REPORT · companies hiring right now*"
    assert cabecera[1].startswith("1 company with open roles seen in the last 7 days.")
    assert lineas(publicado) == [
        (
            "• *Acme* — Ada Ruiz, COO · 4 open roles: Support Lead, Executive Assistant, "
            "Plant Manager, +1 more · <https://www.linkedin.com/in/ada-ruiz|LinkedIn profile> · "
            f"<https://panel.example/personas/{pid}|Open in panel>"
        )
    ]
    assert publicado["text"] == "⚪ FYI · Weekly hiring report: 1 company hiring"
    assert publicado["blocks"][-1]["type"] == "context"


def test_the_roles_handoff_covers_come_first(listo, slack):
    ficha(cubiertos=("Bookkeeper",), otros=("Welder", "Driver", "Nurse"), vacantes=4)
    reporte_contratando.publicar(LUNES)
    assert "4 open roles: Bookkeeper, Welder, Driver, +1 more" in lineas(slack[0])[0]


def test_one_role_reads_as_one_role_and_no_roles_listed_still_counts(listo, slack):
    ficha("U1", "Ada", "Acme", vacantes=1, cubiertos=("Support Lead",), otros=())
    ficha("U2", "Bob", "Bolt", vacantes=3, cubiertos=(), otros=())
    reporte_contratando.publicar(LUNES)
    acme, bolt = lineas(slack[0])
    assert "· 1 open role: Support Lead ·" in acme
    assert "· 3 open roles ·" in bolt


def test_without_a_known_profile_the_link_says_it_is_a_search(listo, slack):
    ficha(fuente="https://acme.com/team")
    reporte_contratando.publicar(LUNES)
    linea = lineas(slack[0])[0]
    assert (
        "<https://www.linkedin.com/search/results/people/?keywords=Ada+Ruiz+Acme|"
        "Find on LinkedIn (search)>" in linea
    )
    assert "LinkedIn profile" not in linea


def test_a_profile_the_team_added_by_hand_wins(listo, slack):
    pid = ficha(fuente="https://acme.com/team")
    db.execute(
        "update prospects set research_links = %s where id = %s",
        (["https://www.linkedin.com/in/ada-de-verdad"], pid),
    )
    reporte_contratando.publicar(LUNES)
    assert "<https://www.linkedin.com/in/ada-de-verdad|LinkedIn profile>" in lineas(slack[0])[0]


def test_without_a_panel_url_the_line_still_goes_out(listo, slack, monkeypatch):
    monkeypatch.delenv("PANEL_URL")
    ficha()
    assert reporte_contratando.publicar(LUNES) == 1
    assert "Open in panel" not in slack[0]["json"]


def test_a_name_from_slack_cannot_break_the_block(listo, slack):
    ficha(nombre="<!channel> Eve", empresa="Evil <@U1> & Co", cubiertos=("<!here> Agent",))
    reporte_contratando.publicar(LUNES)
    texto = slack[0]["json"]
    assert "<!channel>" not in texto and "<@U1>" not in texto and "<!here>" not in texto
    assert "&lt;!channel&gt; Eve" in texto


# ---------------------------------------------------------------------------------
# Quién entra
# ---------------------------------------------------------------------------------


@pytest.mark.parametrize("evidencia", ["necesidad_declarada", "equipo_relevante", "ninguna"])
def test_only_people_whose_only_evidence_is_open_roles_are_listed(listo, slack, evidencia):
    """Quien pidió algo tiene su tarjeta: no es de este reporte."""
    ficha(evidencia=evidencia)
    assert reporte_contratando.publicar(LUNES) == 0
    assert slack == []


def test_a_low_fit_is_not_a_company_to_nurture(listo, slack):
    ficha(nota=1)
    assert reporte_contratando.publicar(LUNES) == 0


@pytest.mark.parametrize("estado", ["contactado", "cliente", "descartado"])
def test_someone_already_handled_is_not_listed(listo, slack, estado):
    ficha(estado=estado)
    assert reporte_contratando.publicar(LUNES) == 0


def test_a_test_mode_person_and_a_radar_person_are_not_listed(listo, slack):
    pid = ficha("U1", "Ada", "Acme")
    db.execute("update prospects set sin_tarjeta = true where id = %s", (pid,))
    ficha("li:ACoAAB123", "Rosa Decisora", "Codelco")
    assert reporte_contratando.publicar(LUNES) == 0


def test_only_the_latest_dossier_counts(listo, slack):
    """Se la volvió a investigar y ahora pidió algo: ya no es "solo vacantes"."""
    pid = ficha()
    db.execute(
        "insert into dossiers (prospect_id, version, content, sources, created_at) "
        "values (%s, 2, %s, '[]'::jsonb, %s)",
        (
            pid,
            json.dumps({"encaje_handoff": {"puntuacion": 3, "evidencia": "necesidad_declarada"}}),
            LUNES - timedelta(hours=1),
        ),
    )
    assert reporte_contratando.publicar(LUNES) == 0


def test_with_the_rule_switched_off_there_is_nothing_to_report(listo, slack):
    """Apagada la regla, esas fichas vuelven a tener tarjeta: no salen dos veces."""
    ajuste("vacantes_solo_reporte", False)
    ficha()
    assert reporte_contratando.publicar(LUNES) == 0


# ---------------------------------------------------------------------------------
# Una línea por empresa, lo más accionable arriba, con tope
# ---------------------------------------------------------------------------------


def test_two_members_of_the_same_company_are_one_line(listo, slack):
    ficha("U1", "Ada Ruiz", "Acme", dominio="acme.com", nota=2)
    ficha("U2", "Bob Lee", "Acme Inc.", dominio="www.acme.com", nota=3)
    ficha("U3", "Cy Diaz", "Bolt", nota=2)
    ficha("U4", "Di Poe", "BOLT!", nota=3)
    assert reporte_contratando.publicar(LUNES) == 2
    salieron = lineas(slack[0])
    assert len(salieron) == 2
    # De cada empresa, quien queda más arriba en el orden: la nota más alta.
    assert "Bob Lee" in salieron[0] + salieron[1] and "Ada Ruiz" not in slack[0]["json"]
    assert "Di Poe" in slack[0]["json"] and "Cy Diaz" not in slack[0]["json"]
    assert slack[0]["blocks"][0]["text"]["text"].splitlines()[1].startswith("2 companies ")


def test_the_most_actionable_companies_come_first(listo, slack):
    ficha("U1", "A", "Sin Cubiertos", cubiertos=(), otros=("Welder",), nota=3)
    ficha("U2", "B", "Nota Dos", nota=2)
    ficha("U3", "C", "Un Rol", cubiertos=("Support Lead",), nota=3)
    ficha("U4", "D", "Sin Perfil", nota=3, fuente="https://x.com/d")
    ficha("U5", "E", "La Mejor", nota=3)
    reporte_contratando.publicar(LUNES)
    orden = [linea.split("*")[1] for linea in lineas(slack[0])]
    assert orden == ["La Mejor", "Sin Perfil", "Un Rol", "Nota Dos", "Sin Cubiertos"]


def test_the_list_is_capped_and_the_rest_is_counted(listo, slack):
    ajuste("reporte_contratando_max", 2)
    for n in range(5):
        ficha(f"U{n}", f"P{n}", f"Empresa {n}", hace_dias=1 + n / 10)
    assert reporte_contratando.publicar(LUNES) == 2
    assert len(lineas(slack[0])) == 2
    assert "_+3 more not shown_" in slack[0]["json"]
    assert "5 companies with open roles" in slack[0]["json"]
    assert slack[0]["text"].endswith("5 companies hiring")


def test_many_long_lines_never_go_over_the_block_limit(listo, slack):
    ajuste("reporte_contratando_max", 50)
    for n in range(40):
        ficha(
            f"U{n}",
            "N" * 300,
            f"{n} " + "E" * 300,
            cargo="C" * 300,
            cubiertos=("R" * 300, "S" * 300),
        )
    assert reporte_contratando.publicar(LUNES) == 40
    assert len(slack[0]["blocks"]) <= 50
    for bloque in slack[0]["blocks"]:
        if bloque["type"] == "section":
            assert len(bloque["text"]["text"]) <= 3000


# ---------------------------------------------------------------------------------
# Sin soltar un atraso: su ventana, su día, sin repetir
# ---------------------------------------------------------------------------------


def test_the_first_report_only_looks_at_its_window(listo, slack):
    """Aplicar la migración no vacía un atraso: lo de hace un mes no sale."""
    ficha("U1", "Ada", "Reciente", hace_dias=6)
    ficha("U2", "Bob", "Vieja", hace_dias=8)
    ficha("U3", "Cy", "Viejísima", hace_dias=40)
    assert reporte_contratando.publicar(LUNES) == 1
    assert "Reciente" in slack[0]["json"] and "Viej" not in slack[0]["json"]


def test_the_window_comes_from_config(listo, slack):
    ajuste("reporte_contratando_dias", 10)
    ficha("U2", "Bob", "Vieja", hace_dias=8)
    assert reporte_contratando.publicar(LUNES) == 1
    assert "seen in the last 10 days" in slack[0]["json"]


@pytest.mark.parametrize("dias_despues", [1, 2, 3, 4, 5, 6])
def test_any_other_day_of_the_week_nothing_goes_out(listo, slack, dias_despues):
    """Desplegar o encender el interruptor a media semana no publica nada."""
    ahora = LUNES + timedelta(days=dias_despues)
    ficha(ahora=ahora)
    assert reporte_contratando.publicar(ahora) == 0
    assert slack == []
    assert db.fetch_one("select count(*) as n from reportes_contratando")["n"] == 0


def test_it_waits_for_its_hour_in_the_configured_time_zone(listo, slack):
    ficha()
    # Las 8:59 del lunes en Nueva York.
    assert reporte_contratando.publicar(datetime(2026, 11, 16, 13, 59, tzinfo=UTC)) == 0
    assert reporte_contratando.publicar(datetime(2026, 11, 16, 14, 0, tzinfo=UTC)) == 1


def test_the_day_and_the_hour_come_from_config(listo, slack):
    ajuste("reporte_contratando_dia", 5)
    ajuste("reporte_contratando_hora", 16)
    viernes = datetime(2026, 11, 20, 21, 30, tzinfo=UTC)  # 16:30 en Nueva York
    ficha(ahora=viernes)
    assert reporte_contratando.publicar(LUNES) == 0
    assert reporte_contratando.publicar(viernes - timedelta(hours=1)) == 0
    assert reporte_contratando.publicar(viernes) == 1


def test_the_day_is_the_one_in_the_time_zone_not_utc(listo, slack):
    ficha()
    # Las 02:00 UTC del martes son todavía las 21:00 del lunes en Nueva York.
    assert reporte_contratando.publicar(datetime(2026, 11, 17, 2, 0, tzinfo=UTC)) == 1


def test_only_one_report_a_week_and_another_the_next(listo, slack):
    ficha("U1", "Ada", "Acme")
    assert reporte_contratando.publicar(LUNES) == 1
    ficha("U2", "Bob", "Bolt")
    assert reporte_contratando.publicar(LUNES + timedelta(hours=3)) == 0
    siguiente = LUNES + timedelta(days=7)
    ficha("U3", "Cy", "Cobalt", ahora=siguiente)
    assert reporte_contratando.publicar(siguiente) == 1
    assert len(slack) == 2


def test_a_company_is_not_listed_again_the_week_after(listo, slack):
    """Con una ventana más larga que una semana, lo ya listado no se repite."""
    ajuste("reporte_contratando_dias", 30)
    ficha("U1", "Ada", "Acme")
    assert reporte_contratando.publicar(LUNES) == 1
    siguiente = LUNES + timedelta(days=7)
    ficha("U2", "Bob", "Bolt", ahora=siguiente)
    assert reporte_contratando.publicar(siguiente) == 1
    assert "Bolt" in slack[1]["json"] and "Acme" not in slack[1]["json"]


def test_a_redone_dossier_does_not_bring_the_company_back(listo, slack):
    ajuste("reporte_contratando_dias", 30)
    pid = ficha("U1", "Ada", "Acme")
    reporte_contratando.publicar(LUNES)
    siguiente = LUNES + timedelta(days=7)
    db.execute(
        "insert into dossiers (prospect_id, version, content, sources, created_at) "
        "select prospect_id, 2, content, sources, %s from dossiers where prospect_id = %s",
        (siguiente - timedelta(days=1), pid),
    )
    assert reporte_contratando.publicar(siguiente) == 0


def test_what_did_not_fit_can_come_out_next_week(listo, slack):
    ajuste("reporte_contratando_dias", 30)
    ajuste("reporte_contratando_max", 1)
    ficha("U1", "Ada", "Acme", nota=3)
    ficha("U2", "Bob", "Bolt", nota=2)
    reporte_contratando.publicar(LUNES)
    assert reporte_contratando.publicar(LUNES + timedelta(days=7)) == 1
    assert "Bolt" in slack[1]["json"]


# ---------------------------------------------------------------------------------
# Publicarlo: reserva, vacío, fallos y frenos
# ---------------------------------------------------------------------------------


def test_what_went_out_is_written_down(listo, slack):
    pid = ficha()
    reporte_contratando.publicar(LUNES)
    fila = db.fetch_one("select * from reportes_contratando")
    assert str(fila["semana"]) == "2026-11-16"
    assert (fila["estado"], fila["lineas"], fila["total"]) == ("publicado", 1, 1)
    assert (fila["channel_id"], fila["message_ts"]) == ("CHANDOFF", "1700000000.000001")
    linea = db.fetch_one(
        "select prospect_id::text as pid, dossier_version from reportes_contratando_lineas"
    )
    assert linea == {"pid": pid, "dossier_version": 1}
    accion = db.fetch_one(
        "select payload from agent_actions where action = 'reporte_contratando_publicado'"
    )["payload"]
    assert accion["semana"] == "2026-11-16" and accion["lineas"] == 1


def test_the_week_is_reserved_before_publishing(listo, slack, monkeypatch):
    ficha()
    visto = []

    def post_card(blocks, text):
        visto.append(db.fetch_one("select estado from reportes_contratando")["estado"])
        return "CHANDOFF", "1.1"

    monkeypatch.setattr(slack_writer, "post_card", post_card)
    reporte_contratando.publicar(LUNES)
    assert visto == ["reservado"]


def test_a_week_another_worker_already_holds_is_not_published_again(listo, slack):
    ficha()
    db.execute(
        "insert into reportes_contratando (semana, estado) values ('2026-11-16', 'reservado')"
    )
    assert reporte_contratando.publicar(LUNES) == 0
    assert slack == []


def test_with_nothing_to_list_nothing_is_posted_and_the_week_is_closed(listo, slack):
    assert reporte_contratando.publicar(LUNES) == 0
    assert slack == []
    assert db.fetch_one("select estado from reportes_contratando")["estado"] == "vacio"
    ficha()
    assert reporte_contratando.publicar(LUNES + timedelta(hours=2)) == 0


def test_a_slack_failure_is_retried_a_few_times_and_lists_nobody(listo, slack, monkeypatch):
    ficha()

    def falla(blocks, text):
        raise RuntimeError("slack caído")

    monkeypatch.setattr(slack_writer, "post_card", falla)
    for _ in range(5):
        assert reporte_contratando.publicar(LUNES) == 0
    fila = db.fetch_one("select estado, intentos from reportes_contratando")
    assert (fila["estado"], fila["intentos"]) == ("fallido", reporte_contratando.MAX_INTENTOS)
    assert db.fetch_one("select count(*) as n from reportes_contratando_lineas")["n"] == 0
    fallos = db.fetch_one(
        "select count(*) as n from agent_actions where action = 'reporte_contratando_fallido'"
    )["n"]
    assert fallos == reporte_contratando.MAX_INTENTOS


def test_after_a_slack_failure_the_next_pass_publishes(listo, slack, monkeypatch):
    ficha()
    real = slack_writer.post_card
    monkeypatch.setattr(slack_writer, "post_card", lambda b, t: (_ for _ in ()).throw(OSError()))
    assert reporte_contratando.publicar(LUNES) == 0
    monkeypatch.setattr(slack_writer, "post_card", real)
    assert reporte_contratando.publicar(LUNES + timedelta(minutes=15)) == 1
    assert db.fetch_one("select estado from reportes_contratando")["estado"] == "publicado"


def test_with_the_switch_off_it_does_nothing(listo, slack):
    ajuste("reporte_contratando_activo", False)
    ficha()
    assert reporte_contratando.publicar(LUNES) == 0
    assert db.fetch_one("select count(*) as n from reportes_contratando")["n"] == 0


def test_the_kill_switch_stops_it_and_leaves_one_trace(listo, slack):
    ajuste("kill_switch", True)
    ficha()
    for _ in range(3):
        assert reporte_contratando.publicar(LUNES) == 0
    assert slack == []
    paradas = db.fetch_one(
        "select count(*) as n from agent_actions where action = 'reporte_contratando_parada'"
    )["n"]
    assert paradas == 1
    # Sin gastar la semana: al quitar la parada, sale.
    ajuste("kill_switch", False)
    assert reporte_contratando.publicar(LUNES) == 1


def test_without_a_bot_token_nothing_is_published_and_the_week_is_kept(listo, slack, monkeypatch):
    monkeypatch.delenv("HANDOFF_SLACK_BOT_TOKEN")
    ficha()
    assert reporte_contratando.publicar(LUNES) == 0
    assert db.fetch_one("select count(*) as n from reportes_contratando")["n"] == 0


def test_an_unexpected_failure_never_escapes(listo, monkeypatch):
    def boom(now):
        raise RuntimeError("config caída")

    monkeypatch.setattr(reporte_contratando, "_publicar", boom)
    assert reporte_contratando.publicar(LUNES) == 0


def test_one_strange_dossier_does_not_take_the_others_with_it(listo, slack):
    ficha("U1", "Ada", "Acme")
    rara = ficha("U2", "Bob", "Bolt")
    db.execute(
        "update dossiers set content = %s where prospect_id = %s",
        (
            json.dumps(
                {
                    "empresa": "no es un objeto",
                    "contratacion": {"vacantes_abiertas": "muchas", "roles": "x"},
                    "encaje_handoff": {"puntuacion": 3, "evidencia": "vacantes"},
                }
            ),
            rara,
        ),
    )
    assert reporte_contratando.publicar(LUNES) == 1
    assert "Acme" in slack[0]["json"]


def test_it_costs_nothing_and_never_asks_a_model(listo, slack, monkeypatch):
    def nunca(*args, **kwargs):
        raise AssertionError("el reporte no puede llamar al modelo")

    monkeypatch.setattr(llm, "complete", nunca)
    ficha()
    assert reporte_contratando.publicar(LUNES) == 1
    assert db.fetch_one("select count(*) as n from llm_calls")["n"] == 0
    assert db.fetch_one("select count(*) as n from cost_events")["n"] == 0


def test_with_the_priority_labels_off_the_header_is_its_own(listo, slack):
    ajuste("prioridad_activa", False)
    ficha()
    reporte_contratando.publicar(LUNES)
    assert slack[0]["blocks"][0]["text"]["text"].startswith("🏢 *WEEKLY HIRING REPORT")
    assert slack[0]["text"] == "Weekly hiring report: 1 company hiring"


def test_the_report_is_fyi_by_rule():
    assert prioridad.nivel(prioridad.REPORTE_CONTRATANDO) == prioridad.FYI
    assert prioridad.REPORTE_CONTRATANDO in prioridad.TIPOS


# ---------------------------------------------------------------------------------
# Coherencia con el resumen del día: no se sale en los dos sitios
# ---------------------------------------------------------------------------------


def tarjeta(pid, banda="alta"):
    db.execute(
        "insert into deliveries (prospect_id, kind, band, dossier_version, channel_id, "
        "message_ts) values (%s, 'slack', %s, 1, 'CHANDOFF', '1699999999.000100')",
        (pid, banda),
    )


def test_a_card_from_before_the_rule_is_no_longer_an_act_now_pending(listo, slack):
    """Antes de 0040 estas fichas tuvieron su tarjeta HIGH SIGNAL. Siguen en
    `deliveries`, pero ya no son pendientes del resumen del día: van al reporte."""
    ajuste("resumen_prioridad_hora", 0)
    solo_vacantes = ficha("U1", "Ada Ruiz", "Acme", ahora=datetime.now(UTC))
    pidio_algo = ficha(
        "U2", "Bob Lee", "Bolt", evidencia="necesidad_declarada", ahora=datetime.now(UTC)
    )
    tarjeta(solo_vacantes)
    tarjeta(pidio_algo)
    assert resumen_prioridad.publicar() == 1
    assert "Bob Lee" in slack[0]["json"] and "Ada Ruiz" not in slack[0]["json"]


def test_with_the_rule_off_the_old_card_is_pending_again(listo, slack):
    ajuste("resumen_prioridad_hora", 0)
    ajuste("vacantes_solo_reporte", False)
    tarjeta(ficha("U1", "Ada Ruiz", "Acme", ahora=datetime.now(UTC)))
    assert resumen_prioridad.publicar() == 1


def test_someone_with_only_open_roles_is_not_a_hot_lead(listo):
    pid = ficha()
    assert prioridad.es_caliente(pid) is False
    ajuste("vacantes_solo_reporte", False)
    assert prioridad.es_caliente(pid) is True


# ---------------------------------------------------------------------------------
# Lo que el panel puede ver y tocar
# ---------------------------------------------------------------------------------


@pytest.fixture
def equipo(conn, restore_config):
    db.execute("insert into panel_users (email) values (%s)", (ALLOWED,))


@pytest.mark.parametrize("tabla", ["reportes_contratando", "reportes_contratando_lineas"])
def test_the_tables_are_internal_bookkeeping_nobody_reads_from_the_panel(equipo, tabla):
    for quien in (ALLOWED, STRANGER, None):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            as_user(quien, f"select * from {tabla}")
    assert db.fetch_one("select relrowsecurity from pg_class where relname = %s", (tabla,))[
        "relrowsecurity"
    ]


@pytest.mark.parametrize("clave", INTERRUPTORES)
def test_the_switches_can_be_turned_off_and_on_from_settings(equipo, clave):
    as_user(ALLOWED, "update config set value = 'false'::jsonb where key = %s", (clave,))
    assert valor(clave) is False
    as_user(ALLOWED, "update config set value = 'true'::jsonb where key = %s", (clave,))
    assert valor(clave) is True


@pytest.mark.parametrize("clave", INTERRUPTORES)
@pytest.mark.parametrize("malo", ["1", '"yes"', "null"])
def test_the_switches_only_take_a_boolean(equipo, clave, malo):
    with pytest.raises(psycopg.errors.Error):
        as_user(ALLOWED, "update config set value = %s::jsonb where key = %s", (malo, clave))
    assert valor(clave) is True


@pytest.mark.parametrize(
    ("clave", "bueno"),
    [
        ("tarjeta_dias_misma_persona", "0"),
        ("tarjeta_dias_misma_persona", "60"),
        ("research_reintento_horas", "720"),
        ("research_reintentos_max", "0"),
        ("research_reintentos_max", "10"),
        ("reporte_contratando_dia", "1"),
        ("reporte_contratando_dia", "7"),
        ("reporte_contratando_hora", "0"),
        ("reporte_contratando_hora", "23"),
        ("reporte_contratando_dias", "60"),
        ("reporte_contratando_max", "50"),
    ],
)
def test_the_numbers_can_be_changed_from_settings(equipo, clave, bueno):
    as_user(ALLOWED, "update config set value = %s::jsonb where key = %s", (bueno, clave))
    assert valor(clave) == int(bueno)


@pytest.mark.parametrize(
    ("clave", "malo"),
    [
        ("tarjeta_dias_misma_persona", "61"),
        ("tarjeta_dias_misma_persona", "-1"),
        ("research_reintento_horas", "721"),
        ("research_reintentos_max", "11"),
        ("research_reintentos_max", "1.5"),
        ("reporte_contratando_dia", "0"),
        ("reporte_contratando_dia", "8"),
        ("reporte_contratando_dia", '"monday"'),
        ("reporte_contratando_hora", "24"),
        ("reporte_contratando_hora", "true"),
        ("reporte_contratando_dias", "0"),
        ("reporte_contratando_dias", "61"),
        ("reporte_contratando_max", "51"),
        ("reporte_contratando_max", "null"),
    ],
)
def test_the_numbers_only_take_whole_values_in_range(equipo, clave, malo):
    antes = valor(clave)
    with pytest.raises(psycopg.errors.Error):
        as_user(ALLOWED, "update config set value = %s::jsonb where key = %s", (malo, clave))
    assert valor(clave) == antes


def test_a_stranger_cannot_touch_any_of_it(equipo):
    for clave in (*NUMERICAS, *INTERRUPTORES):
        antes = valor(clave)
        as_user(STRANGER, "update config set value = '1'::jsonb where key = %s", (clave,))
        as_user(STRANGER, "update config set value = 'false'::jsonb where key = %s", (clave,))
        assert valor(clave) == antes


def test_the_new_policy_does_not_widen_the_allowlist(equipo):
    for clave in ("kill_switch", "monthly_budget_usd", "zona_horaria"):
        antes = valor(clave)
        as_user(ALLOWED, "update config set value = '5'::jsonb where key = %s", (clave,))
        assert valor(clave) == antes
