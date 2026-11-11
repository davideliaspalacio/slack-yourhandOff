"""Cliente de HubSpot: con interruptor, tope diario, freno y reintentos.

Nunca llama a HubSpot de verdad (respx) ni duerme de verdad. Lo que se comprueba aquí
no es que el cliente funcione, sino que no pueda hacer daño: ni duplicar a una persona,
ni borrar las opciones de `source` que usa su equipo comercial, ni dejar la llave en el
ledger, ni reventar el bucle del worker con una excepción que nadie espera.
"""

import ast
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx

from handoff_agent import db, guards
from handoff_agent.cli import main as cli_main
from handoff_agent.tools import hubspot

BASE = "https://api.hubapi.com"
BUSCAR_EMPRESAS = f"{BASE}/crm/v3/objects/companies/search"
EMPRESAS = f"{BASE}/crm/v3/objects/companies"
BUSCAR_CONTACTOS = f"{BASE}/crm/v3/objects/contacts/search"
CONTACTOS = f"{BASE}/crm/v3/objects/contacts"
NOTAS = f"{BASE}/crm/v3/objects/notes"
SOURCE = f"{BASE}/crm/v3/properties/contacts/source"

TOKEN = "pat-na1-secret-service-key"

# Sus cinco opciones de `source`, tal como las devuelve su portal. Ninguna dice
# "Founders Club": por eso hay que añadirla, y por eso no se puede perder ninguna.
SOURCE_SUYO = {
    "name": "source",
    "label": "Source",
    "type": "enumeration",
    "fieldType": "select",
    "groupName": "contactinformation",
    "options": [
        {"label": "Event", "value": "Event", "displayOrder": 0, "hidden": False},
        {"label": "Referral", "value": "Referral", "displayOrder": 1, "hidden": False},
        {
            "label": "Outbound Campaign",
            "value": "Outbound Campaign",
            "displayOrder": 2,
            "hidden": False,
        },
        {"label": "AG Connections", "value": "AG Connections", "displayOrder": 3, "hidden": False},
        {"label": "Organic", "value": "Organic", "displayOrder": 4, "hidden": False},
    ],
    # Campo de solo lectura que HubSpot devuelve y que no se le puede reenviar.
    "createdAt": "2024-01-01T00:00:00Z",
}


@pytest.fixture
def env(monkeypatch, restore_config):
    monkeypatch.delenv("HUBSPOT_TOKEN", raising=False)
    guardar_secreto("hubspot_token", TOKEN)
    set_config("hubspot_activo", True)
    sleeps = []
    alerts = []
    monkeypatch.setattr(hubspot, "_dormir", sleeps.append)
    monkeypatch.setattr(hubspot, "_rechazos_seguidos", 0)
    monkeypatch.setattr(hubspot.ops_alerts, "alert", lambda kind, msg: alerts.append((kind, msg)))
    return {"sleeps": sleeps, "alerts": alerts}


def set_config(key, value):
    db.execute(
        "insert into config (key, value) values (%s, %s::jsonb) "
        "on conflict (key) do update set value = excluded.value",
        (key, json.dumps(value)),
    )


def guardar_secreto(key, value):
    db.execute(
        "insert into secretos (key, value) values (%s, %s) "
        "on conflict (key) do update set value = excluded.value",
        (key, value),
    )


def activo():
    return db.fetch_one("select value from config where key = 'hubspot_activo'")["value"]


def actions(action=None):
    if action:
        return db.fetch_all(
            "select action, payload, result from agent_actions where action = %s", (action,)
        )
    return db.fetch_all("select action, payload, result from agent_actions")


def respuesta_lista(*respuestas):
    """Una ruta que va devolviendo respuestas distintas, una por llamada."""
    restantes = list(respuestas)

    def siguiente(request):
        return restantes.pop(0) if len(restantes) > 1 else restantes[0]

    return siguiente


# --- credenciales ------------------------------------------------------------------


def test_without_a_key_nothing_is_called(conn, monkeypatch):
    monkeypatch.delenv("HUBSPOT_TOKEN", raising=False)
    with pytest.raises(hubspot.HubSpotNoConfigurado):
        hubspot.HubSpot().buscar_contacto_por_email("ana@acme.com")


def test_the_table_wins_over_the_environment(conn, monkeypatch):
    from handoff_agent import secretos

    monkeypatch.setenv("HUBSPOT_TOKEN", "del-entorno")
    assert secretos.hubspot_token() == "del-entorno"
    guardar_secreto("hubspot_token", "del-panel")
    assert secretos.hubspot_token() == "del-panel"


@respx.mock
def test_the_key_travels_as_a_bearer_token(env):
    ruta = respx.post(BUSCAR_CONTACTOS).mock(return_value=httpx.Response(200, json={"results": []}))
    hubspot.HubSpot().buscar_contacto_por_email("ana@acme.com")
    assert ruta.calls[0].request.headers["Authorization"] == f"Bearer {TOKEN}"


# --- guardarraíles -----------------------------------------------------------------


@respx.mock
def test_with_the_switch_off_nothing_reaches_hubspot(env):
    set_config("hubspot_activo", False)
    with pytest.raises(hubspot.HubSpotPausado):
        hubspot.HubSpot().crear_o_actualizar_contacto("ana@acme.com", {"firstname": "Ana"})
    assert respx.calls.call_count == 0


@respx.mock
def test_the_kill_switch_stops_the_crm_too(env):
    set_config("kill_switch", True)
    with pytest.raises(guards.KillSwitchActive):
        hubspot.HubSpot().crear_nota("hola", contact_id="1")
    assert respx.calls.call_count == 0


@respx.mock
def test_the_daily_cap_counts_todays_calls(env):
    set_config("hubspot_por_dia", 2)
    # Lo de ayer no cuenta.
    db.execute(
        "insert into agent_actions (action, payload, created_at) "
        "values ('hubspot_llamada', '{}', %s)",
        (hubspot._ahora_utc() - timedelta(days=1),),
    )
    busqueda = respx.post(BUSCAR_CONTACTOS).mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    cliente = hubspot.HubSpot()
    cliente.buscar_contacto_por_email("ana@acme.com")
    cliente.buscar_contacto_por_email("beto@acme.com")
    with pytest.raises(hubspot.HubSpotTopeDiario):
        cliente.buscar_contacto_por_email("caro@acme.com")
    assert busqueda.call_count == 2


def test_the_day_starts_in_the_configured_timezone(env):
    """El tope es "hoy" en la zona de la operación, no en UTC (igual que seguimiento)."""
    ahora = hubspot._ahora_utc()
    assert hubspot._inicio_del_dia(ahora).tzinfo.key == "America/New_York"
    set_config("zona_horaria", "Pacific/Auckland")
    inicio = hubspot._inicio_del_dia(ahora)
    assert inicio.tzinfo.key == "Pacific/Auckland"
    assert (inicio.hour, inicio.minute, inicio.second) == (0, 0, 0)
    set_config("zona_horaria", "no/existe")
    assert hubspot._inicio_del_dia(ahora).tzinfo.key == "America/New_York"


@respx.mock
def test_the_ledger_records_the_call_but_never_the_key(env):
    respx.post(BUSCAR_CONTACTOS).mock(
        return_value=httpx.Response(200, json={"results": [{"id": "77"}]})
    )
    respx.patch(f"{CONTACTOS}/77").mock(return_value=httpx.Response(200, json={"id": "77"}))
    respx.post(NOTAS).mock(return_value=httpx.Response(201, json={"id": "900"}))
    cliente = hubspot.HubSpot()
    cliente.crear_o_actualizar_contacto("ana@acme.com", {"firstname": "Ana"})
    cliente.crear_nota("Ana lleva seis meses buscando un backend en LATAM", contact_id="77")

    registradas = actions("hubspot_llamada")
    assert [a["payload"]["metodo"] for a in registradas] == ["POST", "PATCH", "POST"]
    assert registradas[1]["result"] == {"status": 200, "intentos": 1, "id": "77"}
    volcado = json.dumps([dict(a) for a in actions()])
    assert TOKEN not in volcado
    # Tampoco el contenido: ni el correo de la persona ni el texto de la nota.
    assert "ana@acme.com" not in volcado
    assert "backend" not in volcado


# --- buscar antes de escribir ------------------------------------------------------


@respx.mock
def test_an_existing_company_is_updated_never_created_again(env):
    respx.post(BUSCAR_EMPRESAS).mock(
        return_value=httpx.Response(200, json={"results": [{"id": "500"}]})
    )
    patch = respx.patch(f"{EMPRESAS}/500").mock(
        return_value=httpx.Response(200, json={"id": "500"})
    )
    crear = respx.post(EMPRESAS).mock(return_value=httpx.Response(201, json={"id": "999"}))
    assert hubspot.HubSpot().crear_o_actualizar_empresa("acme.com", {"name": "Acme"}) == "500"
    assert patch.call_count == 1
    assert crear.call_count == 0


@respx.mock
def test_an_existing_contact_is_updated_never_created_again(env):
    respx.post(BUSCAR_CONTACTOS).mock(
        return_value=httpx.Response(200, json={"results": [{"id": "77"}]})
    )
    patch = respx.patch(f"{CONTACTOS}/77").mock(return_value=httpx.Response(200, json={"id": "77"}))
    crear = respx.post(CONTACTOS).mock(return_value=httpx.Response(201, json={"id": "888"}))
    assert hubspot.HubSpot().crear_o_actualizar_contacto("Ana@ACME.com ", {"lastname": "Pérez"})
    assert crear.call_count == 0
    assert json.loads(patch.calls[0].request.content)["properties"]["email"] == "ana@acme.com"


@respx.mock
def test_a_new_contact_returns_the_id_from_the_create_and_is_never_searched_again(env):
    """La trampa del índice: tras crear, el buscador de HubSpot todavía no lo ve."""
    busqueda = respx.post(BUSCAR_CONTACTOS).mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    respx.post(CONTACTOS).mock(return_value=httpx.Response(201, json={"id": "888"}))
    assert hubspot.HubSpot().crear_o_actualizar_contacto("ana@acme.com", {"firstname": "Ana"}) == (
        "888"
    )
    assert busqueda.call_count == 1


@respx.mock
def test_a_company_with_nothing_new_is_not_written_at_all(env):
    respx.post(BUSCAR_EMPRESAS).mock(
        return_value=httpx.Response(200, json={"results": [{"id": "500"}]})
    )
    patch = respx.patch(f"{EMPRESAS}/500")
    assert hubspot.HubSpot().crear_o_actualizar_empresa("https://WWW.Acme.com/precios") == "500"
    assert patch.call_count == 0


@respx.mock
def test_the_domain_is_normalised_before_searching(env):
    ruta = respx.post(BUSCAR_EMPRESAS).mock(return_value=httpx.Response(200, json={"results": []}))
    respx.post(EMPRESAS).mock(return_value=httpx.Response(201, json={"id": "1"}))
    hubspot.HubSpot().crear_o_actualizar_empresa("HTTPS://www.Acme.com/jobs?x=1", {"name": "Acme"})
    enviado = json.loads(ruta.calls[0].request.content)
    assert enviado["filterGroups"][0]["filters"][0]["value"] == "acme.com"


@respx.mock
def test_empty_properties_are_dropped_so_an_update_never_erases_their_data(env):
    respx.post(BUSCAR_CONTACTOS).mock(
        return_value=httpx.Response(200, json={"results": [{"id": "77"}]})
    )
    patch = respx.patch(f"{CONTACTOS}/77").mock(return_value=httpx.Response(200, json={"id": "77"}))
    hubspot.HubSpot().crear_o_actualizar_contacto(
        "ana@acme.com", {"firstname": "Ana", "jobtitle": "   ", "phone": None}
    )
    assert json.loads(patch.calls[0].request.content)["properties"] == {
        "firstname": "Ana",
        "email": "ana@acme.com",
    }


# --- asociaciones y notas ----------------------------------------------------------


@respx.mock
def test_the_association_is_the_idempotent_v4_default(env):
    ruta = respx.put(f"{BASE}/crm/v4/objects/contacts/77/associations/default/companies/500").mock(
        return_value=httpx.Response(200, json={})
    )
    cliente = hubspot.HubSpot()
    cliente.asociar_contacto_con_empresa("77", "500")
    cliente.asociar_contacto_con_empresa("77", "500")
    assert ruta.call_count == 2


@respx.mock
def test_a_note_carries_both_associations_and_a_timestamp(env):
    ruta = respx.post(NOTAS).mock(return_value=httpx.Response(201, json={"id": "900"}))
    assert hubspot.HubSpot().crear_nota("research", contact_id="77", company_id="500") == "900"
    enviado = json.loads(ruta.calls[0].request.content)
    assert enviado["properties"]["hs_note_body"] == "research"
    assert enviado["properties"]["hs_timestamp"]
    assert [a["to"]["id"] for a in enviado["associations"]] == ["77", "500"]
    assert [a["types"][0]["associationTypeId"] for a in enviado["associations"]] == [202, 190]


@respx.mock
def test_a_note_with_nothing_to_attach_to_is_refused_before_the_call(env):
    with pytest.raises(hubspot.HubSpotError):
        hubspot.HubSpot().crear_nota("research")
    assert respx.calls.call_count == 0


@respx.mock
def test_an_id_that_is_not_a_number_never_reaches_a_url(env):
    with pytest.raises(hubspot.HubSpotError):
        hubspot.HubSpot().asociar_contacto_con_empresa("77/../../evil", "500")
    assert respx.calls.call_count == 0


# --- reintentos y freno ------------------------------------------------------------


@respx.mock
def test_a_429_is_retried_and_honours_retry_after(env):
    respx.post(BUSCAR_CONTACTOS).mock(
        side_effect=respuesta_lista(
            httpx.Response(429, headers={"Retry-After": "7"}, json={}),
            httpx.Response(200, json={"results": [{"id": "77"}]}),
        )
    )
    assert hubspot.HubSpot().buscar_contacto_por_email("ana@acme.com") == "77"
    assert env["sleeps"] == [7.0]
    assert actions("hubspot_llamada")[0]["result"]["intentos"] == 2


@respx.mock
def test_an_absurd_retry_after_is_capped(env):
    respx.post(BUSCAR_CONTACTOS).mock(
        side_effect=respuesta_lista(
            httpx.Response(429, headers={"Retry-After": "99999"}, json={}),
            httpx.Response(200, json={"results": []}),
        )
    )
    hubspot.HubSpot().buscar_contacto_por_email("ana@acme.com")
    assert env["sleeps"] == [hubspot._ESPERA_MAX]


@respx.mock
def test_a_5xx_is_retried_with_backoff_and_then_gives_up_as_an_error(env):
    ruta = respx.post(BUSCAR_CONTACTOS).mock(return_value=httpx.Response(503, json={}))
    with pytest.raises(hubspot.HubSpotError) as caught:
        hubspot.HubSpot().buscar_contacto_por_email("ana@acme.com")
    assert not isinstance(caught.value, hubspot.HubSpotPausado)
    assert ruta.call_count == hubspot._MAX_INTENTOS
    assert env["sleeps"] == [1.0, 2.0, 4.0]
    assert activo() is True


@respx.mock
def test_a_persistent_429_is_a_rate_limit_error(env):
    respx.post(BUSCAR_CONTACTOS).mock(return_value=httpx.Response(429, json={}))
    with pytest.raises(hubspot.HubSpotRateLimit):
        hubspot.HubSpot().buscar_contacto_por_email("ana@acme.com")
    assert activo() is True


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
def test_a_single_rejection_does_not_switch_anything_off(env, status):
    respx.post(BUSCAR_CONTACTOS).mock(return_value=httpx.Response(status, json={}))
    with pytest.raises(hubspot.HubSpotError):
        hubspot.HubSpot().buscar_contacto_por_email("ana@acme.com")
    assert activo() is True
    assert env["alerts"] == []


@respx.mock
def test_repeated_rejections_switch_hubspot_off_and_alert_once(env):
    respx.post(BUSCAR_CONTACTOS).mock(return_value=httpx.Response(401, json={}))
    cliente = hubspot.HubSpot()
    for _ in range(hubspot._RECHAZOS_PARA_FRENAR):
        with pytest.raises(hubspot.HubSpotError):
            cliente.buscar_contacto_por_email("ana@acme.com")
    assert activo() is False
    assert [kind for kind, _ in env["alerts"]] == ["hubspot_apagado"]
    assert "Settings" in env["alerts"][0][1]
    # Y con el interruptor apagado ya no se llama más, ni se repite la alerta.
    with pytest.raises(hubspot.HubSpotPausado):
        cliente.buscar_contacto_por_email("beto@acme.com")
    assert len(env["alerts"]) == 1


@respx.mock
def test_a_good_answer_resets_the_rejection_streak(env):
    respx.post(BUSCAR_CONTACTOS).mock(
        side_effect=respuesta_lista(
            httpx.Response(401, json={}),
            httpx.Response(401, json={}),
            httpx.Response(200, json={"results": []}),
            httpx.Response(401, json={}),
            httpx.Response(401, json={}),
        )
    )
    cliente = hubspot.HubSpot()
    for _ in range(2):
        with pytest.raises(hubspot.HubSpotError):
            cliente.buscar_contacto_por_email("ana@acme.com")
    cliente.buscar_contacto_por_email("ana@acme.com")
    for _ in range(2):
        with pytest.raises(hubspot.HubSpotError):
            cliente.buscar_contacto_por_email("ana@acme.com")
    assert activo() is True


# --- nada se escapa ----------------------------------------------------------------


@respx.mock
def test_a_network_failure_is_a_hubspot_error_not_an_httpx_one(env):
    ruta = respx.post(BUSCAR_CONTACTOS).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(hubspot.HubSpotError):
        hubspot.HubSpot().buscar_contacto_por_email("ana@acme.com")
    assert ruta.call_count == hubspot._MAX_INTENTOS
    assert actions("hubspot_llamada")[0]["result"] == {"error": "HubSpotError"}


@respx.mock
def test_an_answer_that_is_not_json_does_not_reach_the_caller_as_a_surprise(env):
    respx.post(BUSCAR_CONTACTOS).mock(return_value=httpx.Response(200, text="<html>nope</html>"))
    assert hubspot.HubSpot().buscar_contacto_por_email("ana@acme.com") is None


@respx.mock
def test_a_create_without_an_id_is_a_typed_error(env):
    respx.post(BUSCAR_CONTACTOS).mock(return_value=httpx.Response(200, json={"results": []}))
    respx.post(CONTACTOS).mock(return_value=httpx.Response(201, json={"ok": True}))
    with pytest.raises(hubspot.HubSpotError):
        hubspot.HubSpot().crear_o_actualizar_contacto("ana@acme.com", {"firstname": "Ana"})


@respx.mock
def test_everything_this_module_raises_is_catchable_in_one_except(env):
    """El bucle del worker solo debería tener que capturar HubSpotError."""
    for excepcion in (
        hubspot.HubSpotNoConfigurado,
        hubspot.HubSpotPausado,
        hubspot.HubSpotTopeDiario,
        hubspot.HubSpotRateLimit,
    ):
        assert issubclass(excepcion, hubspot.HubSpotError)
    respx.post(BUSCAR_CONTACTOS).mock(return_value=httpx.Response(400, json={"message": "nope"}))
    try:
        hubspot.HubSpot().buscar_contacto_por_email("ana@acme.com")
    except hubspot.HubSpotError:
        pass


# --- esquema -----------------------------------------------------------------------


def _esquema_vacio():
    """Ninguna de nuestras propiedades existe todavía; la suya sí."""
    for propiedad in hubspot.PROPIEDADES_PROPIAS:
        respx.get(f"{BASE}/crm/v3/properties/{propiedad.objeto}/{propiedad.nombre}").mock(
            return_value=httpx.Response(404, json={"status": "error"})
        )
    return (
        respx.post(f"{BASE}/crm/v3/properties/companies").mock(
            return_value=httpx.Response(201, json={"name": "handoff_fit_score"})
        ),
        respx.post(f"{BASE}/crm/v3/properties/contacts").mock(
            return_value=httpx.Response(201, json={"name": "handoff_fit_score"})
        ),
    )


@respx.mock
def test_missing_properties_are_created_once(env):
    set_config("hubspot_por_dia", 100)
    empresas, contactos = _esquema_vacio()
    respx.get(SOURCE).mock(return_value=httpx.Response(200, json=SOURCE_SUYO))
    respx.patch(SOURCE).mock(return_value=httpx.Response(200, json=SOURCE_SUYO))
    resumen = hubspot.HubSpot().asegurar_propiedades()
    assert len(resumen["creadas"]) == len(hubspot.PROPIEDADES_PROPIAS)
    assert resumen["existentes"] == []
    assert empresas.call_count == 2
    assert contactos.call_count == 3


@respx.mock
def test_running_it_twice_creates_nothing_and_patches_nothing(env):
    """Idempotente: la segunda pasada solo lee."""
    set_config("hubspot_por_dia", 100)
    for propiedad in hubspot.PROPIEDADES_PROPIAS:
        respx.get(f"{BASE}/crm/v3/properties/{propiedad.objeto}/{propiedad.nombre}").mock(
            return_value=httpx.Response(200, json={"name": propiedad.nombre})
        )
    crear_empresas = respx.post(f"{BASE}/crm/v3/properties/companies")
    crear_contactos = respx.post(f"{BASE}/crm/v3/properties/contacts")
    con_la_nuestra = {
        **SOURCE_SUYO,
        "options": [*SOURCE_SUYO["options"], {"label": "Founders Club", "value": "Founders Club"}],
    }
    respx.get(SOURCE).mock(return_value=httpx.Response(200, json=con_la_nuestra))
    patch = respx.patch(SOURCE)
    resumen = hubspot.HubSpot().asegurar_propiedades()
    assert resumen["creadas"] == []
    assert len(resumen["existentes"]) == len(hubspot.PROPIEDADES_PROPIAS)
    assert resumen["source"] == "ya estaba"
    assert (crear_empresas.call_count, crear_contactos.call_count, patch.call_count) == (0, 0, 0)


@respx.mock
def test_their_source_options_survive_untouched_and_ours_goes_last(env):
    set_config("hubspot_por_dia", 100)
    _esquema_vacio()
    respx.get(SOURCE).mock(return_value=httpx.Response(200, json=SOURCE_SUYO))
    patch = respx.patch(SOURCE).mock(return_value=httpx.Response(200, json=SOURCE_SUYO))
    assert hubspot.HubSpot().asegurar_propiedades()["source"] == "añadida"

    enviado = json.loads(patch.calls[0].request.content)
    # Solo se tocan las opciones: ni la etiqueta ni el tipo de SU propiedad.
    assert set(enviado) == {"options"}
    assert [o["label"] for o in enviado["options"]] == [
        "Event",
        "Referral",
        "Outbound Campaign",
        "AG Connections",
        "Organic",
        "Founders Club",
    ]
    # Su orden original intacto, y la nuestra detrás.
    assert [o["displayOrder"] for o in enviado["options"]] == [0, 1, 2, 3, 4, 5]
    # Y nada de los campos de solo lectura que devuelve el GET.
    assert all(set(o) <= set(hubspot._CLAVES_OPCION) for o in enviado["options"])


@respx.mock
def test_an_option_that_differs_only_in_case_is_not_added_twice(env):
    set_config("hubspot_por_dia", 100)
    for propiedad in hubspot.PROPIEDADES_PROPIAS:
        respx.get(f"{BASE}/crm/v3/properties/{propiedad.objeto}/{propiedad.nombre}").mock(
            return_value=httpx.Response(200, json={"name": propiedad.nombre})
        )
    suyo = {
        **SOURCE_SUYO,
        "options": [*SOURCE_SUYO["options"], {"label": "founders club", "value": "FOUNDERS_CLUB"}],
    }
    respx.get(SOURCE).mock(return_value=httpx.Response(200, json=suyo))
    patch = respx.patch(SOURCE)
    assert hubspot.HubSpot().asegurar_propiedades()["source"] == "ya estaba"
    assert patch.call_count == 0


@respx.mock
def test_if_they_ever_drop_their_source_property_we_do_not_invent_it(env):
    set_config("hubspot_por_dia", 100)
    for propiedad in hubspot.PROPIEDADES_PROPIAS:
        respx.get(f"{BASE}/crm/v3/properties/{propiedad.objeto}/{propiedad.nombre}").mock(
            return_value=httpx.Response(200, json={"name": propiedad.nombre})
        )
    respx.get(SOURCE).mock(return_value=httpx.Response(404, json={"status": "error"}))
    crear = respx.post(f"{BASE}/crm/v3/properties/contacts")
    assert hubspot.HubSpot().asegurar_propiedades()["source"] == "no existe en su portal"
    assert crear.call_count == 0


# --- diagnóstico y CLI -------------------------------------------------------------


def _portal_ok():
    respx.get(f"{BASE}/account-info/v3/details").mock(
        return_value=httpx.Response(200, json={"portalId": 245974710, "timeZone": "US/Eastern"})
    )
    for ruta in ("companies", "contacts", "notes"):
        respx.get(f"{BASE}/crm/v3/objects/{ruta}").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
    for ruta in ("companies", "contacts"):
        respx.get(f"{BASE}/crm/v3/properties/{ruta}").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
    respx.get(f"{BASE}/crm/v3/limits/custom-properties").mock(
        return_value=httpx.Response(
            200,
            json={
                "overallLimit": 1000,
                "byObjectType": [
                    {"objectType": "COMPANY", "usage": 9, "limit": 1000},
                    {"objectType": "CONTACT", "usage": 10, "limit": 1000},
                ],
            },
        )
    )


@respx.mock
def test_the_status_works_with_the_switch_off(env, capsys):
    set_config("hubspot_activo", False)
    _portal_ok()
    assert cli_main(["hubspot-estado"]) == 0
    salida = capsys.readouterr().out
    assert "Portal: 245974710" in salida
    assert "Read notes: OK" in salida
    assert "COMPANY: 9/1000" in salida
    assert "Sync switch (hubspot_activo): off" in salida
    # Diagnóstico: no gasta cupo del worker.
    assert actions("hubspot_llamada") == []
    assert len(actions("hubspot_estado")) == 7


@respx.mock
def test_the_status_reports_what_the_key_cannot_do_without_switching_anything_off(env, capsys):
    _portal_ok()
    respx.get(f"{BASE}/crm/v3/objects/notes").mock(return_value=httpx.Response(403, json={}))
    assert cli_main(["hubspot-estado"]) == 0
    assert "Read notes: NOT ALLOWED" in capsys.readouterr().out
    # Una comprobación manual nunca frena ni avisa: ya hay una persona mirando.
    assert activo() is True
    assert env["alerts"] == []


@respx.mock
def test_the_status_says_so_when_there_is_no_key(conn, monkeypatch, capsys):
    monkeypatch.delenv("HUBSPOT_TOKEN", raising=False)
    assert cli_main(["hubspot-estado"]) == 1
    assert "HubSpot key: missing" in capsys.readouterr().out
    assert respx.calls.call_count == 0


@respx.mock
def test_a_dead_key_is_reported_with_a_failure_code(env, capsys):
    respx.route(host="api.hubapi.com").mock(return_value=httpx.Response(401, json={}))
    assert cli_main(["hubspot-estado"]) == 1
    assert "NOT ALLOWED" in capsys.readouterr().out


# --- la regla que no se negocia ----------------------------------------------------


def test_this_module_never_touches_slack():
    """El Slack del Founders Club es ajeno y de solo lectura; el de Handoff no es
    asunto del CRM. Se comprueba sobre el fichero porque la regla importa más que
    la buena intención de quien lo edite mañana."""
    fuente = Path(hubspot.__file__).read_text(encoding="utf-8")
    importados = []
    for nodo in ast.walk(ast.parse(fuente)):
        if isinstance(nodo, ast.Import):
            importados += [alias.name for alias in nodo.names]
        elif isinstance(nodo, ast.ImportFrom):
            importados += [f"{nodo.module or ''}.{alias.name}" for alias in nodo.names]
    assert [nombre for nombre in importados if "slack" in nombre.lower()] == []
    for prohibido in ("slack.com", "slack_sdk", "slack_client", "slack_writer", "chat.postMessage"):
        assert prohibido not in fuente
