"""Escrituras del MCP remoto: estados, notas, cola, umbrales y canales, con el control
del panel.

Lo que más importa: sin identidad o con un correo fuera de `panel_users` no cambia nada,
cada escritura deja su firma (correo y sufijo, nunca el token), el tope diario de
escrituras corta a la N+1 sin apagar la lectura, y las claves de dinero y el interruptor
de emergencia son intocables.
"""

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError
from starlette.responses import JSONResponse

from handoff_agent import db, mcp_escritura, mcp_identidad, mcp_remoto
from handoff_agent.tools import prospects
from handoff_agent.untrusted import TAG
from handoff_agent.web import app as web_app
from tests.test_mcp_auth import crear, llamada, post, rpc
from tests.test_mcp_remoto import _huella
from tests.test_panel_rls import as_user

ANA = "ana@example.com"
BEA = "bea@example.com"
EXTRANO = "intruso@example.com"
SUFIJO = "ab12"

ESCRITURAS = [
    ("persona_cambiar_estado", {"id_o_slack_user_id": "U1", "estado": "contactado"}),
    ("persona_ayudar_research", {"id_o_slack_user_id": "U1", "empresa": "Acme"}),
    ("persona_reinvestigar", {"id_o_slack_user_id": "U1"}),
    ("ajustar_umbral", {"clave": "sms_por_dia", "valor": 7}),
    ("canal_agregar", {"channel_id": "C0ABC123"}),
    ("canal_quitar", {"channel_id": "C0ABC123"}),
]
# Lo que el panel ya deja tocar y el MCP no: el dinero, la emergencia, sus propios topes
# y los booleanos.
CLAVES_PROHIBIDAS = [
    "kill_switch",
    "monthly_budget_usd",
    "run_budget_usd",
    "mcp_llamadas_por_dia",
    "mcp_escrituras_por_dia",
    "exigir_evidencia",
    "senales_activas",
    "unipile_pausado",
    "clave_inventada",
    "",
]


@pytest.fixture
def equipo(conn):
    for email in (ANA, BEA):
        db.execute("insert into panel_users (email) values (%s)", (email,))


@pytest.fixture
def gente(equipo):
    ada = prospects.upsert_prospect("U1", full_name="Ada Ruiz")
    bob = prospects.upsert_prospect("U2", full_name="Bob")
    return {"ada": ada, "bob": bob}


@pytest.fixture
def como_ana():
    with mcp_identidad.como(ANA, SUFIJO):
        yield


def estado_de(slack_user_id):
    return db.fetch_one("select state from prospects where slack_user_id = %s", (slack_user_id,))[
        "state"
    ]


def auditoria():
    return db.fetch_all(
        "select * from agent_actions where action = 'mcp_escritura' order by created_at"
    )


def cola(slack_user_id="U1"):
    return db.fetch_all(
        "select status, reason from research_jobs where slack_user_id = %s", (slack_user_id,)
    )


def valor(clave):
    return db.fetch_one("select value from config where key = %s", (clave,))["value"]


def llamar(nombre, argumentos):
    return getattr(mcp_escritura, nombre)(**argumentos)


# --- 1. quién llama ---


def test_identity_is_visible_inside_the_block_and_gone_after():
    with pytest.raises(ToolError, match="no caller identity"):
        mcp_identidad.identidad()
    with mcp_identidad.como(ANA, SUFIJO):
        assert mcp_identidad.identidad() == (ANA, SUFIJO)
        assert mcp_remoto.identidad() == (ANA, SUFIJO)
    with pytest.raises(ToolError, match="no caller identity"):
        mcp_identidad.identidad()


@pytest.mark.parametrize(("nombre", "args"), ESCRITURAS)
def test_without_identity_every_write_fails_and_changes_nothing(gente, nombre, args):
    antes = _huella()
    with pytest.raises(ToolError, match="no caller identity"):
        llamar(nombre, args)
    assert _huella() == antes


@pytest.mark.parametrize(("nombre", "args"), ESCRITURAS)
async def test_without_identity_the_server_reports_an_error_not_a_change(gente, nombre, args):
    antes = _huella()
    with pytest.raises(ToolError, match="no caller identity"):
        await mcp_remoto.mcp.call_tool(nombre, args)
    assert _huella() == antes


def test_reads_do_not_need_an_identity(gente):
    assert mcp_remoto.persona_ver("U1")["persona"]["slack_user_id"] == "U1"
    assert mcp_remoto.personas_buscar()["total"] == 2


# --- 2. un correo fuera de panel_users ---


@pytest.mark.parametrize(("nombre", "args"), ESCRITURAS)
def test_an_email_outside_panel_users_cannot_write(gente, nombre, args):
    antes = _huella()
    with mcp_identidad.como(EXTRANO, SUFIJO), pytest.raises(ToolError, match="not authorized"):
        llamar(nombre, args)
    assert _huella() == antes


def test_an_unauthorized_email_learns_nothing_about_who_exists(gente):
    """Se comprueba el acceso antes de buscar a la persona: mismo error exista o no."""
    with mcp_identidad.como(EXTRANO, SUFIJO):
        for referencia in ("U1", "U404", "00000000-0000-0000-0000-000000000000"):
            with pytest.raises(ToolError, match="not authorized"):
                mcp_escritura.persona_cambiar_estado(referencia, "descartado")


def test_the_owner_losing_panel_access_stops_writes_at_once(gente):
    with mcp_identidad.como(ANA, SUFIJO):
        mcp_escritura.persona_cambiar_estado("U1", "contactado")
        db.execute("delete from panel_users where email = %s", (ANA,))
        with pytest.raises(ToolError, match="not authorized"):
            mcp_escritura.persona_cambiar_estado("U1", "cliente")
    assert estado_de("U1") == "contactado"


@pytest.mark.parametrize(
    "llamada_sql",
    [
        ("select public.mcp_cambiar_estado(%s, %s, 'cliente')", "p"),
        ("select public.mcp_reinvestigar(%s, %s)", "p"),
        ("select public.mcp_ayudar_research(%s, %s, 'X', null, null, null)", "p"),
        ("select public.mcp_ajustar_umbral(%s, 'sms_por_dia', 9)", None),
        ("select public.mcp_canal_agregar(%s, 'C0ABC123', null, '{}'::text[])", None),
        ("select public.mcp_canal_quitar(%s, 'C0ABC123', '{}'::text[])", None),
    ],
)
@pytest.mark.parametrize("email", [EXTRANO, "", None, "ANA@EXAMPLE.COM ", "%"])
def test_the_sql_functions_themselves_refuse_an_email_outside_panel_users(
    gente, llamada_sql, email
):
    sql, con_persona = llamada_sql
    params = (email, gente["ada"]["id"]) if con_persona else (email,)
    antes = _huella()
    with pytest.raises(psycopg.errors.RaiseException, match="not authorized"):
        db.fetch_one(sql, params)
    assert _huella() == antes


def test_the_sql_email_check_ignores_case_like_the_panel(gente):
    fila = db.fetch_one(
        "select public.mcp_cambiar_estado(%s, %s, 'cliente') as r",
        ("ANA@Example.com", gente["ada"]["id"]),
    )
    assert fila["r"]["estado"] == "cliente"


def test_the_api_roles_cannot_execute_the_mcp_functions(gente):
    """Reciben el correo como argumento: si el panel las pudiera llamar, cualquiera con
    cuenta se haría pasar por alguien del equipo."""
    funciones = [
        "public._mcp_exigir_panel(text)",
        "public._ayudar_research(uuid, text, text, text[], text)",
        "public.mcp_cambiar_estado(text, uuid, text)",
        "public.mcp_ayudar_research(text, uuid, text, text, text[], text)",
        "public.mcp_reinvestigar(text, uuid)",
        "public.mcp_ajustar_umbral(text, text, numeric)",
    ]
    for funcion in funciones:
        fila = db.fetch_one(
            "select coalesce(p.proacl::text, '') as acl, p.prosecdef, p.proconfig::text as cfg "
            "from pg_proc p where p.oid = %s::regprocedure",
            (funcion,),
        )
        assert fila["prosecdef"], funcion
        assert "search_path=" in fila["cfg"], funcion
        for rol in ("anon", "authenticated", "public"):
            assert not db.fetch_one(
                "select has_function_privilege(%s, %s::regprocedure, 'execute') as ok",
                ("public" if rol == "public" else rol, funcion),
            )["ok"], (rol, funcion)
        # La lista de permisos solo nombra a roles concretos: una entrada que empieza por
        # "=" sería PUBLIC, y anon/authenticated son los roles de la API.
        entradas = fila["acl"].strip("{}").split(",")
        assert not [e for e in entradas if e.startswith("=")], funcion
        assert not [e for e in entradas if e.startswith(("anon=", "authenticated="))], funcion
    ada = str(gente["ada"]["id"])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        as_user(ANA, "select public.mcp_cambiar_estado(%s, %s, 'descartado')", (ANA, ada))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        as_user(ANA, "select public.mcp_ajustar_umbral(%s, 'sms_por_dia', 9)", (ANA,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        as_user(ANA, "select public._mcp_exigir_panel(%s)", (ANA,))
    assert estado_de("U1") == "nuevo"


# --- 3. cambiar el estado ---


def test_a_valid_state_change_returns_the_resulting_state(gente, como_ana):
    antes = db.fetch_one("select updated_at from prospects where slack_user_id = 'U1'")
    r = mcp_escritura.persona_cambiar_estado("U1", "descartado", motivo="no encaja")
    assert r["ok"] is True
    assert r["estado_anterior"] == "nuevo" and r["estado"] == "descartado"
    assert r["persona"]["slack_user_id"] == "U1" and r["persona"]["estado"] == "descartado"
    assert r["persona"]["nombre"].startswith(f"<{TAG} ")  # el nombre es de un tercero
    assert estado_de("U1") == "descartado"
    assert estado_de("U2") == "nuevo"
    despues = db.fetch_one("select updated_at from prospects where slack_user_id = 'U1'")
    assert despues["updated_at"] > antes["updated_at"]
    json.dumps(r)


def test_the_person_can_be_given_by_id_or_by_slack_user_id(gente, como_ana):
    mcp_escritura.persona_cambiar_estado(str(gente["ada"]["id"]), "contactado")
    assert estado_de("U1") == "contactado"
    mcp_escritura.persona_cambiar_estado("  U2  ", "cliente")
    assert estado_de("U2") == "cliente"


@pytest.mark.parametrize(
    "estado", ["incompleto", "borrado", "", "  ", "'; drop table prospects;--"]
)
def test_an_invalid_state_is_refused_and_changes_nothing(gente, como_ana, estado):
    antes = _huella()
    with pytest.raises(ToolError, match="invalid state"):
        mcp_escritura.persona_cambiar_estado("U1", estado)
    assert _huella() == antes


def test_the_state_comparison_ignores_case_and_spaces(gente, como_ana):
    mcp_escritura.persona_cambiar_estado("U1", " Contactado ")
    assert estado_de("U1") == "contactado"


def test_the_sql_function_rejects_an_invalid_state_by_itself(gente):
    for malo in ("incompleto", "borrado", "", None):
        with pytest.raises(psycopg.errors.RaiseException, match="invalid state"):
            db.fetch_one(
                "select public.mcp_cambiar_estado(%s, %s, %s)", (ANA, gente["ada"]["id"], malo)
            )
    assert estado_de("U1") == "nuevo"


@pytest.mark.parametrize(
    "referencia", ["U404", "00000000-0000-0000-0000-000000000000", "", "no-es-un-uuid"]
)
def test_an_unknown_person_is_a_clear_error_and_changes_nothing(gente, como_ana, referencia):
    antes = _huella()
    with pytest.raises(ToolError, match="no person found"):
        mcp_escritura.persona_cambiar_estado(referencia, "descartado")
    assert _huella() == antes


def test_the_sql_function_reports_an_unknown_person(gente):
    with pytest.raises(psycopg.errors.RaiseException, match="not found"):
        db.fetch_one(
            "select public.mcp_cambiar_estado(%s, '00000000-0000-0000-0000-000000000000', 'cliente')",
            (ANA,),
        )


def test_an_incompleto_person_can_be_moved_like_in_the_panel(gente, como_ana):
    db.execute("update prospects set state = 'incompleto' where slack_user_id = 'U1'")
    r = mcp_escritura.persona_cambiar_estado("U1", "contactado")
    assert r["estado_anterior"] == "incompleto" and estado_de("U1") == "contactado"


# --- volver a investigar ---


def test_reinvestigating_queues_a_manual_job_once(gente, como_ana):
    r = mcp_escritura.persona_reinvestigar("U1")
    assert r["resultado"] == "encolado" and r["research"] == "queued"
    assert cola() == [{"status": "pendiente", "reason": "manual"}]
    # Segunda vez: la cola ya impide dos abiertas, y se dice, no se finge.
    r = mcp_escritura.persona_reinvestigar(str(gente["ada"]["id"]))
    assert r["resultado"] == "ya_en_cola" and r["research"] == "already queued"
    assert len(cola()) == 1
    assert cola("U2") == []


def test_a_discarded_person_is_not_requeued(gente, como_ana):
    mcp_escritura.persona_cambiar_estado("U1", "descartado")
    r = mcp_escritura.persona_reinvestigar("U1")
    assert r["resultado"] == "descartado"
    assert "discarded" in r["research"]
    assert r["persona"]["estado"] == "descartado"
    assert cola() == []


def test_reinvestigating_an_unknown_person_fails(gente, como_ana):
    with pytest.raises(ToolError, match="no person found"):
        mcp_escritura.persona_reinvestigar("U404")


# --- ayudar al research ---


def test_help_the_research_saves_normalizes_and_queues(gente, como_ana):
    r = mcp_escritura.persona_ayudar_research(
        "U1",
        empresa="  Acme Corp ",
        web="https://www.Acme.com/about?x=1",
        enlaces=["https://acme.com/team", " https://acme.com/team ", "https://blog.acme.com"],
        notas="Series A, hiring SDRs",
    )
    assert r["ok"] is True and r["research"] == "queued"
    fila = db.fetch_one("select * from prospects where slack_user_id = 'U1'")
    assert fila["company_name_override"] == "Acme Corp"
    assert fila["company_domain_override"] == "acme.com"
    assert fila["research_links"] == ["https://acme.com/team", "https://blog.acme.com"]
    assert fila["research_notes"] == "Series A, hiring SDRs"
    assert cola() == [{"status": "pendiente", "reason": "manual"}]
    # Lo que devuelve es lo que quedó guardado, no lo que se pidió, y va delimitado.
    assert "acme.com" in r["guardado"]["dominio"] and "about" not in r["guardado"]["dominio"]
    assert (
        r["guardado"]["empresa"].startswith(f"<{TAG} ") and "Acme Corp" in r["guardado"]["empresa"]
    )
    assert len(r["guardado"]["enlaces"]) == 2
    json.dumps(r)


def test_what_you_leave_out_is_kept(gente, como_ana):
    mcp_escritura.persona_ayudar_research(
        "U1", empresa="Acme", web="acme.com", enlaces=["https://acme.com/a"], notas="nota 1"
    )
    mcp_escritura.persona_ayudar_research("U1", web="https://acme.org")
    fila = db.fetch_one("select * from prospects where slack_user_id = 'U1'")
    assert fila["company_domain_override"] == "acme.org"
    assert fila["company_name_override"] == "Acme"
    assert fila["research_links"] == ["https://acme.com/a"]
    assert fila["research_notes"] == "nota 1"


def test_links_are_added_without_duplicates_and_an_empty_list_clears_them(gente, como_ana):
    mcp_escritura.persona_ayudar_research("U1", enlaces=["https://a.com/1", "https://a.com/2"])
    mcp_escritura.persona_ayudar_research("U1", enlaces=["https://a.com/2", "https://a.com/3"])
    assert db.fetch_one("select research_links from prospects where slack_user_id = 'U1'")[
        "research_links"
    ] == ["https://a.com/1", "https://a.com/2", "https://a.com/3"]
    mcp_escritura.persona_ayudar_research("U1", enlaces=[])
    assert (
        db.fetch_one("select research_links from prospects where slack_user_id = 'U1'")[
            "research_links"
        ]
        is None
    )


def test_notes_replace_and_an_empty_string_clears(gente, como_ana):
    mcp_escritura.persona_ayudar_research("U1", notas="primera", empresa="Acme")
    mcp_escritura.persona_ayudar_research("U1", notas="segunda")
    fila = db.fetch_one("select * from prospects where slack_user_id = 'U1'")
    assert fila["research_notes"] == "segunda" and fila["company_name_override"] == "Acme"
    mcp_escritura.persona_ayudar_research("U1", notas="", empresa="")
    fila = db.fetch_one("select * from prospects where slack_user_id = 'U1'")
    assert fila["research_notes"] is None and fila["company_name_override"] is None


def test_nothing_to_change_is_an_error_and_queues_nothing(gente, como_ana):
    antes = _huella()
    with pytest.raises(ToolError, match="nothing to change"):
        mcp_escritura.persona_ayudar_research("U1")
    assert _huella() == antes


@pytest.mark.parametrize(
    ("campo", "valor_malo", "mensaje"),
    [
        ("web", "no es un dominio", "invalid domain"),
        ("web", "ftp//x", "invalid domain"),
        ("enlaces", ["javascript:alert(1)"], "invalid link"),
        ("enlaces", ["https://x.com/" + "a" * 500], "invalid link"),
        ("enlaces", [f"https://x.com/{i}" for i in range(11)], "too many links"),
        ("notas", "x" * 2001, "invalid notes"),
        ("empresa", "x" * 201, "invalid company name"),
    ],
)
def test_the_panel_validations_apply_and_a_rejection_changes_nothing(
    gente, como_ana, campo, valor_malo, mensaje
):
    antes = _huella()
    with pytest.raises(ToolError, match=mensaje):
        mcp_escritura.persona_ayudar_research("U1", **{campo: valor_malo})
    assert _huella() == antes


def test_a_discarded_person_gets_the_notes_but_no_research_job(gente, como_ana):
    mcp_escritura.persona_cambiar_estado("U1", "descartado")
    r = mcp_escritura.persona_ayudar_research("U1", notas="ya no nos interesa")
    assert "discarded" in r["research"]
    assert cola() == []
    assert (
        db.fetch_one("select research_notes from prospects where slack_user_id = 'U1'")[
            "research_notes"
        ]
        == "ya no nos interesa"
    )


def test_an_open_job_is_not_duplicated_by_helping_the_research(gente, como_ana):
    mcp_escritura.persona_reinvestigar("U1")
    r = mcp_escritura.persona_ayudar_research("U1", empresa="Acme")
    assert r["research"] == "already queued"
    assert len(cola()) == 1


def test_help_the_research_unknown_person(gente, como_ana):
    with pytest.raises(ToolError, match="no person found"):
        mcp_escritura.persona_ayudar_research("U404", empresa="Acme")


def test_the_panel_function_and_the_mcp_one_share_the_logic(gente):
    """Mismas validaciones: lo que el panel rechaza, el MCP también, con el mismo mensaje."""
    ada = gente["ada"]["id"]
    for params in [("x", "no es un dominio", None, None), ("x", None, ["ftp://x"], None)]:
        with pytest.raises(psycopg.errors.RaiseException) as del_panel:
            as_user(ANA, "select panel_ayudar_research(%s, %s, %s, %s, %s)", (ada, *params))
        with pytest.raises(psycopg.errors.RaiseException) as del_mcp:
            db.fetch_one(
                "select public.mcp_ayudar_research(%s, %s, %s, %s, %s, %s)", (ANA, ada, *params)
            )
        assert del_panel.value.diag.message_primary == del_mcp.value.diag.message_primary


# --- 6. umbrales ---


def numericas_que_el_panel_deja_tocar():
    """Las claves numéricas que el panel (RLS) deja actualizar a un usuario del equipo."""
    claves = []
    for fila in db.fetch_all(
        "select key from config where jsonb_typeof(value) = 'number' order by key"
    ):
        if as_user(ANA, "update config set value = value where key = %s", (fila["key"],)) == 1:
            claves.append(fila["key"])
    return claves


def test_adjustable_settings_match_what_the_panel_lets_you_touch(equipo):
    """Si alguien abre o cierra una política de config en una migración futura, este test
    obliga a decidir si el MCP la sigue o no."""
    del_panel = set(numericas_que_el_panel_deja_tocar())
    propios = {"mcp_llamadas_por_dia", "mcp_escrituras_por_dia"}
    assert propios <= del_panel
    assert set(mcp_escritura.UMBRALES) == del_panel - propios
    for peligrosa in ("kill_switch", "monthly_budget_usd", "run_budget_usd"):
        assert peligrosa not in del_panel


@pytest.mark.parametrize("clave", mcp_escritura.UMBRALES)
def test_every_allowed_setting_can_be_changed_and_reports_before_and_after(
    equipo, como_ana, restore_config, clave
):
    anterior = valor(clave)
    r = mcp_escritura.ajustar_umbral(clave, 1)
    assert r == {"ok": True, "clave": clave, "anterior": anterior, "valor": 1}
    assert valor(clave) == 1
    assert type(valor(clave)) is int  # un número JSON, no un texto ni un decimal


def test_changing_a_setting_updates_its_timestamp_and_only_that_row(
    equipo, como_ana, restore_config
):
    antes = {f["key"]: f for f in db.fetch_all("select * from config")}
    mcp_escritura.ajustar_umbral("sms_por_dia", 9)
    despues = {f["key"]: f for f in db.fetch_all("select * from config")}
    cambiadas = [k for k in despues if despues[k] != antes.get(k)]
    assert cambiadas == ["sms_por_dia"]
    assert despues["sms_por_dia"]["updated_at"] > antes["sms_por_dia"]["updated_at"]


@pytest.mark.parametrize("clave", CLAVES_PROHIBIDAS)
def test_forbidden_settings_are_refused_in_python_and_in_sql(
    equipo, como_ana, restore_config, clave
):
    antes = db.fetch_all("select * from config order by key")
    with pytest.raises(ToolError, match="cannot be changed from here"):
        mcp_escritura.ajustar_umbral(clave, 0)
    # También la función SQL, por sí sola: la lista de Python no es la única barrera.
    with pytest.raises(psycopg.errors.RaiseException, match="cannot be changed"):
        db.fetch_one("select public.mcp_ajustar_umbral(%s, %s, 0)", (ANA, clave or None))
    assert db.fetch_all("select * from config order by key") == antes
    assert valor("kill_switch") is False and valor("monthly_budget_usd") == 150
    assert not auditoria()


def test_every_config_key_outside_the_allowed_list_is_refused_by_sql(equipo, restore_config):
    for fila in db.fetch_all("select key from config order by key"):
        if fila["key"] in mcp_escritura.UMBRALES:
            continue
        with pytest.raises(psycopg.errors.RaiseException, match="cannot be changed"):
            db.fetch_one("select public.mcp_ajustar_umbral(%s, %s, 1)", (ANA, fila["key"]))


@pytest.mark.parametrize(
    ("clave", "malo"),
    [
        ("sms_por_dia", -1),
        ("sms_por_dia", 100000),
        ("banda_alta_min", 5),
        ("sms_hora_fin", 25),
        ("radar_auto_min", 11),
    ],
)
def test_out_of_range_values_are_refused_and_change_nothing(
    equipo, como_ana, restore_config, clave, malo
):
    antes = valor(clave)
    with pytest.raises(ToolError, match="invalid value"):
        mcp_escritura.ajustar_umbral(clave, malo)
    assert valor(clave) == antes


@pytest.mark.parametrize("malo", [1.5, "3.5", "tres", None])
async def test_a_fractional_or_missing_value_is_refused(equipo, restore_config, malo):
    antes = valor("sms_por_dia")
    with mcp_identidad.como(ANA, SUFIJO), pytest.raises(ToolError):
        await mcp_remoto.mcp.call_tool("ajustar_umbral", {"clave": "sms_por_dia", "valor": malo})
    assert valor("sms_por_dia") == antes


def test_the_sql_function_refuses_a_fraction_or_null(equipo, restore_config):
    antes = valor("sms_por_dia")
    with pytest.raises(psycopg.errors.RaiseException, match="invalid value"):
        db.fetch_one("select public.mcp_ajustar_umbral(%s, 'sms_por_dia', 2.5)", (ANA,))
    with pytest.raises(psycopg.errors.RaiseException, match="invalid value"):
        db.fetch_one("select public.mcp_ajustar_umbral(%s, 'sms_por_dia', null)", (ANA,))
    assert valor("sms_por_dia") == antes


def test_banda_baja_min_can_reach_4_to_silence(equipo, como_ana, restore_config):
    assert mcp_escritura.ajustar_umbral("banda_baja_min", 4)["valor"] == 4


# --- 7. auditoría ---


def test_every_write_leaves_one_signed_row_with_email_and_suffix(gente, como_ana, restore_config):
    ada = str(gente["ada"]["id"])
    mcp_escritura.persona_cambiar_estado("U1", "descartado", motivo="no encaja")
    mcp_escritura.persona_ayudar_research("U2", empresa="Beta", web="beta.io")
    mcp_escritura.persona_reinvestigar("U2")
    mcp_escritura.ajustar_umbral("sms_por_dia", 9)
    filas = auditoria()
    assert [f["payload"]["herramienta"] for f in filas] == [
        "persona_cambiar_estado",
        "persona_ayudar_research",
        "persona_reinvestigar",
        "ajustar_umbral",
    ]
    for f in filas:
        assert f["payload"]["email"] == ANA and f["payload"]["sufijo"] == SUFIJO
        assert f["action"] == "mcp_escritura"
    estado, ayuda, reinv, umbral = filas
    assert estado["payload"]["prospect_id"] == ada and str(estado["prospect_id"]) == ada
    assert set(estado["payload"]) == {"herramienta", "email", "sufijo", "prospect_id", "cambio"}
    assert estado["payload"]["cambio"] == {
        "estado": {"antes": "nuevo", "despues": "descartado"},
        "motivo": "no encaja",
    }
    assert ayuda["payload"]["cambio"]["empresa"] == {"antes": None, "despues": "Beta"}
    assert ayuda["payload"]["cambio"]["dominio"] == {"antes": None, "despues": "beta.io"}
    assert "notas" not in ayuda["payload"]["cambio"]  # no cambió
    assert ayuda["payload"]["cambio"]["encolado"] is True
    assert reinv["payload"]["cambio"] == {"resultado": "ya_en_cola"}
    assert set(umbral["payload"]) == {"herramienta", "email", "sufijo", "clave", "cambio"}
    assert umbral["payload"]["clave"] == "sms_por_dia"
    assert umbral["payload"]["cambio"] == {"antes": 3, "despues": 9}
    assert umbral["prospect_id"] is None


def test_the_audit_row_is_in_the_same_transaction_as_the_change(gente, como_ana, monkeypatch):
    """Si la auditoría no se puede escribir, el cambio tampoco: nada queda sin firma."""

    def falla(*args, **kwargs):
        raise RuntimeError("no se puede serializar")

    monkeypatch.setattr(mcp_escritura.json, "dumps", falla)
    with pytest.raises(RuntimeError):
        mcp_escritura.persona_cambiar_estado("U1", "descartado")
    monkeypatch.undo()
    assert estado_de("U1") == "nuevo"
    assert auditoria() == []


def test_a_rejected_write_leaves_no_audit_row(gente, como_ana):
    for intento in (
        lambda: mcp_escritura.persona_cambiar_estado("U1", "borrado"),
        lambda: mcp_escritura.persona_cambiar_estado("U404", "cliente"),
        lambda: mcp_escritura.persona_ayudar_research("U1", web="no es un dominio"),
        lambda: mcp_escritura.ajustar_umbral("kill_switch", 1),
    ):
        with pytest.raises(ToolError):
            intento()
    assert auditoria() == []


def test_the_audit_never_holds_the_token_end_to_end(equipo, gente, caplog):
    caplog.set_level(logging.DEBUG)
    with TestClient(web_app.app) as cliente:
        token = crear(ANA, "Claude de Ana")
        respuestas = [
            post(
                cliente,
                llamada(
                    "persona_cambiar_estado", {"id_o_slack_user_id": "U1", "estado": "cliente"}
                ),
                token,
            ),
            post(cliente, llamada("ajustar_umbral", {"clave": "kill_switch", "valor": 1}), token),
            post(
                cliente,
                llamada("persona_cambiar_estado", {"id_o_slack_user_id": "U404", "estado": "x"}),
                token,
            ),
        ]
    assert estado_de("U1") == "cliente"
    [fila] = auditoria()
    assert fila["payload"]["email"] == ANA and fila["payload"]["sufijo"] == token[-4:]
    volcado = json.dumps(db.fetch_all("select * from agent_actions"), default=str)
    assert token not in volcado
    assert token not in caplog.text
    for r in respuestas:
        assert token not in r.text
        assert token not in json.dumps(dict(r.headers))


# --- 8. tope de escrituras ---


def test_the_write_cap_is_a_setting_with_a_default_and_editable_from_the_panel(
    equipo, restore_config
):
    assert valor("mcp_escrituras_por_dia") == 100
    sql = "update config set value = %s::jsonb where key = 'mcp_escrituras_por_dia'"
    assert as_user(ANA, sql, ("20",)) == 1
    for malo in ('"mucho"', "true", "-1", "null"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            as_user(ANA, sql, (malo,))
    assert as_user(EXTRANO, sql, ("1",)) == 0
    # La política no abre otras claves.
    assert as_user(ANA, "update config set value = '1'::jsonb where key = 'kill_switch'") == 0


def test_the_n_plus_one_write_fails_and_reads_keep_working(gente, como_ana, restore_config):
    db.execute("update config set value = '3'::jsonb where key = 'mcp_escrituras_por_dia'")
    for estado in ("contactado", "cliente", "contactado"):
        mcp_escritura.persona_cambiar_estado("U1", estado)
    antes = _huella()
    with pytest.raises(ToolError, match="daily write limit reached"):
        mcp_escritura.persona_cambiar_estado("U1", "descartado")
    with pytest.raises(ToolError, match="daily write limit reached"):
        mcp_escritura.ajustar_umbral("sms_por_dia", 9)
    assert _huella() == antes
    assert estado_de("U1") == "contactado"
    assert len(auditoria()) == 3
    # La lectura sigue funcionando.
    assert mcp_remoto.persona_ver("U1")["persona"]["estado"] == "contactado"
    assert mcp_remoto.personas_buscar()["total"] == 2
    assert mcp_remoto.estado_sistema()["kill_switch"] is False


def test_the_write_cap_is_per_person_not_per_token_or_global(gente, como_ana, restore_config):
    db.execute("update config set value = '1'::jsonb where key = 'mcp_escrituras_por_dia'")
    mcp_escritura.persona_cambiar_estado("U1", "contactado")
    with pytest.raises(ToolError, match="daily write limit"):
        mcp_escritura.persona_cambiar_estado("U1", "cliente")
    # Otro token de la misma persona comparte el tope: no se burla creando más tokens.
    with (
        mcp_identidad.como(ANA, "ffff"),
        pytest.raises(ToolError, match="daily write limit"),
    ):
        mcp_escritura.persona_cambiar_estado("U1", "cliente")
    # Otra persona tiene el suyo.
    with mcp_identidad.como(BEA, "bbbb"):
        mcp_escritura.persona_cambiar_estado("U2", "contactado")


def test_a_cap_of_zero_switches_writes_off_and_a_new_day_starts_over(
    gente, como_ana, restore_config
):
    db.execute("update config set value = '0'::jsonb where key = 'mcp_escrituras_por_dia'")
    with pytest.raises(ToolError, match="daily write limit"):
        mcp_escritura.persona_cambiar_estado("U1", "contactado")
    db.execute("update config set value = '1'::jsonb where key = 'mcp_escrituras_por_dia'")
    mcp_escritura.persona_cambiar_estado("U1", "contactado")
    with pytest.raises(ToolError, match="daily write limit"):
        mcp_escritura.persona_cambiar_estado("U1", "cliente")
    db.execute("update agent_actions set created_at = created_at - interval '1 day'")  # "ayer"
    mcp_escritura.persona_cambiar_estado("U1", "cliente")
    assert estado_de("U1") == "cliente"


def test_a_malformed_cap_falls_back_to_the_default(gente, como_ana, restore_config):
    for roto in ('"mucho"', "true", "null"):
        db.execute(
            "update config set value = %s::jsonb where key = 'mcp_escrituras_por_dia'", (roto,)
        )
        mcp_escritura.persona_cambiar_estado("U1", "contactado")
    assert len(auditoria()) == 3  # por debajo del 100 por defecto


def test_the_cap_holds_under_concurrent_writes(gente, restore_config):
    # Ocho a la vez son tantos como conexiones tiene el pool: si una escritura pidiera una
    # segunda conexión mientras tiene la de su transacción, se bloquearían entre ellas.
    db.execute("update config set value = '3'::jsonb where key = 'mcp_escrituras_por_dia'")
    salida = threading.Barrier(8)

    def escribir(i):
        with mcp_identidad.como(ANA, SUFIJO):
            salida.wait()
            try:
                mcp_escritura.persona_cambiar_estado("U1", "contactado")
                return "ok"
            except ToolError as exc:
                return str(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        resultados = list(pool.map(escribir, range(8)))
    assert resultados.count("ok") == 3
    assert all("daily write limit" in r for r in resultados if r != "ok"), resultados
    assert len(auditoria()) == 3


# --- 9. de punta a punta, por HTTP ---


@pytest.fixture
def cliente():
    with TestClient(web_app.app) as c:
        yield c


def texto(r):
    return json.loads(r.json()["result"]["content"][0]["text"])


def test_a_write_through_http_uses_the_identity_of_the_token(equipo, gente, cliente):
    token = crear(ANA, "Claude de Ana")
    r = post(
        cliente,
        llamada("persona_cambiar_estado", {"id_o_slack_user_id": "U1", "estado": "descartado"}),
        token,
    )
    assert r.status_code == 200 and not r.json()["result"].get("isError")
    assert texto(r)["estado"] == "descartado"
    assert estado_de("U1") == "descartado"
    [fila] = auditoria()
    assert fila["payload"]["email"] == ANA and fila["payload"]["sufijo"] == token[-4:]
    # Otra persona, otro token: queda a nombre de ella.
    token_bea = crear(BEA, "Claude de Bea")
    post(
        cliente,
        llamada("persona_cambiar_estado", {"id_o_slack_user_id": "U2", "estado": "cliente"}),
        token_bea,
    )
    assert [f["payload"]["email"] for f in auditoria()] == [ANA, BEA]


def test_the_caller_cannot_choose_who_signs_the_change(equipo, gente, cliente):
    """Argumentos extra como `email` no llegan a ningún sitio: la firma sale del token."""
    token = crear(ANA)
    r = post(
        cliente,
        llamada(
            "persona_cambiar_estado",
            {"id_o_slack_user_id": "U1", "estado": "cliente", "email": BEA, "sufijo": "zzzz"},
        ),
        token,
    )
    assert r.status_code == 200
    # Hizo el cambio, firmado por la dueña del token, ignorando el correo que se coló.
    assert [(f["payload"]["email"], f["payload"]["sufijo"]) for f in auditoria()] == [
        (ANA, token[-4:])
    ]


def test_a_valid_token_whose_owner_left_the_panel_cannot_write_but_can_read(equipo, gente, cliente):
    token = crear(ANA)
    db.execute("delete from panel_users where email = %s", (ANA,))
    r = post(
        cliente,
        llamada("persona_cambiar_estado", {"id_o_slack_user_id": "U1", "estado": "cliente"}),
        token,
    )
    assert r.status_code == 200 and r.json()["result"]["isError"] is True
    assert "not authorized" in r.json()["result"]["content"][0]["text"]
    assert estado_de("U1") == "nuevo" and auditoria() == []
    assert post(cliente, llamada("resumen_costes"), token).status_code == 200


def test_the_http_error_for_a_cap_is_a_tool_error_not_a_429(equipo, gente, cliente, restore_config):
    db.execute("update config set value = '1'::jsonb where key = 'mcp_escrituras_por_dia'")
    token = crear(ANA)
    args = {"id_o_slack_user_id": "U1", "estado": "contactado"}
    assert (
        not post(cliente, llamada("persona_cambiar_estado", args), token)
        .json()["result"]
        .get("isError")
    )
    r = post(cliente, llamada("persona_cambiar_estado", args), token)
    assert r.status_code == 200 and r.json()["result"]["isError"] is True
    assert "daily write limit reached" in r.json()["result"]["content"][0]["text"]
    # Y la lectura sigue, con el mismo token.
    ok = post(cliente, llamada("persona_ver", {"id_o_slack_user_id": "U1"}), token)
    assert ok.status_code == 200 and not ok.json()["result"].get("isError")


def test_writes_count_against_the_general_call_cap_too(equipo, gente, cliente, restore_config):
    db.execute("update config set value = '1'::jsonb where key = 'mcp_llamadas_por_dia'")
    token = crear(ANA)
    args = {"id_o_slack_user_id": "U1", "estado": "contactado"}
    assert post(cliente, llamada("persona_cambiar_estado", args), token).status_code == 200
    assert post(cliente, llamada("persona_cambiar_estado", args), token).status_code == 429


def test_the_gate_sets_the_identity_per_request_and_removes_it_afterwards(equipo, cliente):
    vistas = []

    async def destino(scope, receive, send):
        vistas.append(mcp_identidad.identidad())
        await JSONResponse({"ok": True})(scope, receive, send)

    ana, bea = crear(ANA), crear(BEA)
    original = web_app.guardia_mcp.destino
    web_app.guardia_mcp.destino = destino
    try:
        post(cliente, rpc("tools/list"), ana)
        post(cliente, rpc("tools/list"), bea)
        post(cliente, rpc("tools/list"), ana)
        # Sin token válido, ni se llega al destino.
        assert post(cliente, rpc("tools/list"), "c" * 64).status_code == 401
        assert post(cliente, rpc("tools/list")).status_code == 401
    finally:
        web_app.guardia_mcp.destino = original
    assert vistas == [(ANA, ana[-4:]), (BEA, bea[-4:]), (ANA, ana[-4:])]
    # Nada quedó puesto en este contexto.
    with pytest.raises(ToolError, match="no caller identity"):
        mcp_identidad.identidad()


def test_the_identity_never_holds_the_token(equipo, cliente):
    capturadas = []

    async def destino(scope, receive, send):
        capturadas.append(mcp_identidad._identidad.get())
        await JSONResponse({"ok": True})(scope, receive, send)

    token = crear(ANA)
    web_app.guardia_mcp.destino = destino
    post(cliente, rpc("tools/list"), token)
    [identidad] = capturadas
    assert identidad == (ANA, token[-4:])
    assert token not in json.dumps(identidad)
    assert all(len(x) < 40 for x in identidad)


# --- 10. canales vigilados ---

TARJETAS = "C0TARJETAS"


@pytest.fixture
def canales_vigilados(equipo, restore_config, monkeypatch):
    """Dos canales vigilados, uno con nombre, y un canal de tarjetas configurado."""
    monkeypatch.setenv("HANDOFF_SLACK_CHANNEL_ID", TARJETAS)
    db.execute(
        'update config set value = \'["C0ABC123", "C0DEF456"]\'::jsonb '
        "where key = 'slack_channel_ids'"
    )
    db.execute("insert into canales (channel_id, nombre) values ('C0DEF456', 'founders-general')")


def vigilados():
    return valor("slack_channel_ids")


def test_adding_a_channel_returns_the_resulting_list_with_names(canales_vigilados, como_ana):
    r = mcp_escritura.canal_agregar("c0xyz789")
    assert r["ok"] is True and r["agregado"] == "C0XYZ789"
    assert r["canales"] == [
        {"canal": "C0ABC123", "nombre": None},
        {"canal": "C0DEF456", "nombre": "#founders-general"},
        {"canal": "C0XYZ789", "nombre": None},
    ]
    assert r["total"] == 3 and r["maximo"] == 20
    assert "next poll" in r["nota"]
    assert vigilados() == ["C0ABC123", "C0DEF456", "C0XYZ789"]
    json.dumps(r)


def test_adding_a_channel_is_audited_with_the_email_and_the_suffix(canales_vigilados, como_ana):
    mcp_escritura.canal_agregar("C0XYZ789")
    [fila] = auditoria()
    assert set(fila["payload"]) == {"herramienta", "email", "sufijo", "clave", "cambio"}
    assert fila["payload"]["herramienta"] == "canal_agregar"
    assert (fila["payload"]["email"], fila["payload"]["sufijo"]) == (ANA, SUFIJO)
    assert fila["payload"]["clave"] == "slack_channel_ids"
    assert fila["payload"]["cambio"] == {
        "canales": {
            "antes": ["C0ABC123", "C0DEF456"],
            "despues": ["C0ABC123", "C0DEF456", "C0XYZ789"],
        }
    }
    assert fila["result"] == {"canales": ["C0ABC123", "C0DEF456", "C0XYZ789"]}
    assert fila["prospect_id"] is None


def test_the_cards_channel_can_never_be_watched(canales_vigilados, como_ana):
    antes = _huella()
    for referencia in (TARJETAS, TARJETAS.lower(), f" {TARJETAS} "):
        with pytest.raises(ToolError, match="posts its cards"):
            mcp_escritura.canal_agregar(referencia)
    assert _huella() == antes
    # Y la función SQL por sí sola, con el canal de tarjetas como argumento.
    with pytest.raises(psycopg.errors.RaiseException, match="posts its cards"):
        db.fetch_one(
            "select public.mcp_canal_agregar(%s, %s, %s, '{}'::text[])", (ANA, TARJETAS, TARJETAS)
        )
    assert vigilados() == ["C0ABC123", "C0DEF456"]
    assert not auditoria()


@pytest.mark.parametrize(
    "malo", ["X0ABC123", "C0AB", "C0ABC-23", "random1", "#founders-general", "", "  "]
)
def test_an_invalid_channel_id_is_refused_and_changes_nothing(canales_vigilados, como_ana, malo):
    antes = _huella()
    with pytest.raises(ToolError, match="invalid channel id"):
        mcp_escritura.canal_agregar(malo)
    with pytest.raises(ToolError, match="invalid channel id"):
        mcp_escritura.canal_quitar(malo)
    assert _huella() == antes


def test_a_duplicate_channel_is_refused(canales_vigilados, como_ana):
    antes = _huella()
    for referencia in ("C0ABC123", "c0abc123"):
        with pytest.raises(ToolError, match="already watched"):
            mcp_escritura.canal_agregar(referencia)
    assert _huella() == antes


def test_the_twenty_first_channel_is_refused(canales_vigilados, como_ana, restore_config):
    veinte = [f"C0AAA{i:03d}" for i in range(20)]
    db.execute(
        "update config set value = %s::jsonb where key = 'slack_channel_ids'",
        (json.dumps(veinte),),
    )
    with pytest.raises(ToolError, match="too many channels"):
        mcp_escritura.canal_agregar("C0XYZ789")
    assert vigilados() == veinte
    assert not auditoria()


def test_removing_a_channel_takes_out_the_right_one_and_leaves_the_rest(
    canales_vigilados, como_ana
):
    r = mcp_escritura.canal_quitar("c0abc123")
    assert r["ok"] is True and r["quitado"] == "C0ABC123"
    assert r["canales"] == [{"canal": "C0DEF456", "nombre": "#founders-general"}]
    assert vigilados() == ["C0DEF456"]
    [fila] = auditoria()
    assert fila["payload"]["herramienta"] == "canal_quitar"
    assert fila["payload"]["cambio"]["canales"]["despues"] == ["C0DEF456"]


def test_removing_a_channel_that_is_not_watched_is_a_clear_error(canales_vigilados, como_ana):
    antes = _huella()
    with pytest.raises(ToolError, match="is not watched"):
        mcp_escritura.canal_quitar("C0XYZ789")
    assert _huella() == antes


def test_removing_a_channel_keeps_its_messages_and_signals(canales_vigilados, gente, como_ana):
    db.execute(
        "insert into slack_messages (channel_id, ts, user_id, text) "
        "values ('C0ABC123', '1.1', 'U1', 'hola')"
    )
    db.execute(
        "insert into message_signals (channel_id, ts, detectada, tipo, cita, confianza) "
        "values ('C0ABC123', '1.1', true, 'ventas', 'busco comercial', 'alta')"
    )
    mcp_escritura.canal_quitar("C0ABC123")
    assert db.fetch_one("select count(*) as n from slack_messages")["n"] == 1
    assert db.fetch_one("select count(*) as n from message_signals")["n"] == 1
    assert db.fetch_one("select count(*) as n from canales")["n"] == 1


def test_emptying_the_list_warns_that_the_environment_takes_over(
    canales_vigilados, como_ana, monkeypatch
):
    monkeypatch.setenv("SLACK_CHANNEL_IDS", "C0DEF456")
    db.execute("update config set value = '[\"C0DEF456\"]'::jsonb where key = 'slack_channel_ids'")
    r = mcp_escritura.canal_quitar("C0DEF456")
    assert r["canales"] == [] and "SLACK_CHANNEL_IDS" in r["nota"]
    assert vigilados() == []


def test_the_environment_list_is_materialised_before_adding(canales_vigilados, como_ana):
    """Con la lista de la base vacía el worker usa la del entorno: añadir no la pierde."""
    db.execute("update config set value = '[]'::jsonb where key = 'slack_channel_ids'")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("SLACK_CHANNEL_IDS", "C0DEF456, c0ghi789")
        r = mcp_escritura.canal_agregar("C0XYZ789")
    assert [c["canal"] for c in r["canales"]] == ["C0DEF456", "C0GHI789", "C0XYZ789"]
    assert vigilados() == ["C0DEF456", "C0GHI789", "C0XYZ789"]


def test_the_channel_tools_respect_the_daily_write_cap(canales_vigilados, como_ana):
    db.execute("update config set value = '2'::jsonb where key = 'mcp_escrituras_por_dia'")
    mcp_escritura.canal_agregar("C0XYZ789")
    mcp_escritura.canal_quitar("C0XYZ789")
    antes = _huella()
    with pytest.raises(ToolError, match="daily write limit reached"):
        mcp_escritura.canal_agregar("C0XYZ789")
    with pytest.raises(ToolError, match="daily write limit reached"):
        mcp_escritura.canal_quitar("C0ABC123")
    assert _huella() == antes
    assert vigilados() == ["C0ABC123", "C0DEF456"]
    # La lectura sigue: los canales se pueden consultar aunque no se puedan cambiar.
    assert [c["canal"] for c in mcp_remoto.canales_listar()["canales"]] == [
        "C0ABC123",
        "C0DEF456",
    ]


def test_the_panel_function_and_the_mcp_ones_share_the_validation(equipo, restore_config):
    """panel_guardar_canales (0016) y las mcp_canal_* validan con la misma función."""
    for malo in ("X0ABC123", "C0AB"):
        with pytest.raises(psycopg.errors.RaiseException, match="invalid channel id"):
            as_user(ANA, "select panel_guardar_canales(%s)", ([malo],))
        with pytest.raises(psycopg.errors.RaiseException, match="invalid channel id"):
            db.fetch_one("select public.mcp_canal_agregar(%s, %s, null, '{}'::text[])", (ANA, malo))
    # Y el panel sigue guardando como siempre, normalizando y sin duplicados.
    as_user(ANA, "select panel_guardar_canales(%s)", (["  c0abc123 ", "C0ABC123", "g0def456"],))
    assert vigilados() == ["C0ABC123", "G0DEF456"]
