"""Llevar los leads al CRM: frenos, reserva, y sobre todo qué NUNCA sale de aquí.

El cliente de la API (`handoff_agent.tools.hubspot`) lo escribe otro módulo: en
estos tests se sustituye por `FakeHubSpot`, y casi todo lo que se comprueba es
exactamente qué se le pasó.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from handoff_agent import cli, db
from handoff_agent.crm import hubspot_sync
from handoff_agent.tools import prospects

# Marcadores: si alguno aparece en lo que se manda a HubSpot, es que se escapó
# algo que vive en una comunidad privada ajena.
CITA = "we're drowning in support tickets, please help"
CANAL = "C08MRLP46G6"
DM_TEXTO = "hey Anthony, can we talk pricing"
SUGERENCIA = "Hi! Happy to walk you through pricing"
PROVEEDOR = "proveedor-sin-verificar"

DOSSIER_OK = {
    "persona": {"nombre": "Ada Ruiz", "cargo": "CEO"},
    "empresa": {"nombre": "Acme", "dominio": "acme.com", "empleados_aprox": 60},
    "contratacion": {"vacantes_abiertas": 6, "roles_deslocalizables": ["support"]},
    "senales_contexto": [{"hecho": "Series A", "fuente": "https://tc.com/a"}],
    "encaje_handoff": {
        "puntuacion": 3,
        "evidencia": "vacantes",
        "razon": "Six open support roles right now",
    },
    "resumen": "CEO de Acme, 60 personas, contratando soporte.",
}


class FakeHubSpot:
    """Lo que el otro agente está escribiendo, reducido a lo que aquí se llama."""

    def __init__(self, falla: Exception | None = None) -> None:
        self.empresas: list[tuple] = []
        self.contactos: list[tuple] = []
        self.asociaciones: list[tuple] = []
        self.notas: list[dict] = []
        self.propiedades = 0
        self.falla = falla

    def asegurar_propiedades(self):
        self.propiedades += 1

    def crear_o_actualizar_empresa(self, dominio, propiedades):
        if self.falla:
            raise self.falla
        self.empresas.append((dominio, propiedades))
        return f"empresa-{len(self.empresas)}"

    def crear_o_actualizar_contacto(self, email, propiedades):
        if self.falla:
            raise self.falla
        self.contactos.append((email, propiedades))
        return f"contacto-{len(self.contactos)}"

    def asociar_contacto_con_empresa(self, contact_id, company_id):
        self.asociaciones.append((contact_id, company_id))

    def crear_nota(self, texto, contact_id=None, company_id=None):
        if self.falla:
            raise self.falla
        self.notas.append({"texto": texto, "contact_id": contact_id, "company_id": company_id})

    def enviado(self) -> str:
        """Todo lo que se le pasó, en un solo texto: para buscar filtraciones."""
        return json.dumps(
            {
                "empresas": self.empresas,
                "contactos": self.contactos,
                "asociaciones": self.asociaciones,
                "notas": self.notas,
            },
            ensure_ascii=False,
            default=str,
        )


def config(key, value) -> None:
    db.execute(
        "insert into config (key, value) values (%s, %s::jsonb) "
        "on conflict (key) do update set value = excluded.value",
        (key, json.dumps(value)),
    )


@pytest.fixture
def hubspot(conn, restore_config, monkeypatch):
    """Interruptor encendido, marca puesta hace una hora y cliente sustituido."""
    monkeypatch.setenv("PANEL_URL", "https://panel.handoff.test")
    config("hubspot_activo", True)
    config("hubspot_desde", (datetime.now(UTC) - timedelta(hours=1)).isoformat())
    cliente = FakeHubSpot()
    monkeypatch.setattr(hubspot_sync, "_cliente", lambda: cliente)
    return cliente


def persona(
    slack="U1",
    state="contactado",
    email="ada@acme.com",
    dominio="acme.com",
    dossier=DOSSIER_OK,
    nombre="Ada Ruiz",
    override_dominio=None,
) -> dict:
    fila = prospects.upsert_prospect(slack, full_name=nombre)
    db.execute(
        "update prospects set state = %s, email = %s, company_domain = %s, "
        "company_name = 'Acme', company_domain_override = %s where id = %s",
        (state, email, dominio, override_dominio, fila["id"]),
    )
    if dossier is not None:
        prospects.save_dossier(fila["id"], dossier, [])
    return db.fetch_one("select * from prospects where id = %s", (fila["id"],))


def estado(prospect_id) -> dict:
    return db.fetch_one(
        "select hubspot_estado, hubspot_motivo, hubspot_intentos, hubspot_contact_id, "
        "hubspot_company_id, hubspot_at from prospects where id = %s",
        (prospect_id,),
    )


def envejecer(prospect_id, horas: float) -> None:
    db.execute(
        "update prospects set updated_at = now() - make_interval(hours => %s) where id = %s",
        (horas, prospect_id),
    )


# --- el interruptor y quién entra --------------------------------------------


def test_with_the_switch_off_nothing_is_written(hubspot):
    p = persona()
    config("hubspot_activo", False)
    assert hubspot_sync.sincronizar_pendientes() == 0
    assert hubspot.enviado() == FakeHubSpot().enviado()
    assert estado(p["id"])["hubspot_estado"] is None


@pytest.mark.parametrize("state", ["contactado", "cliente"])
def test_only_contacted_people_and_clients_reach_the_crm(hubspot, state):
    p = persona(state=state)
    assert hubspot_sync.sincronizar_pendientes() == 1
    assert estado(p["id"])["hubspot_estado"] == "sincronizada"


@pytest.mark.parametrize("state", ["nuevo", "investigado", "incompleto", "descartado"])
def test_nobody_else_reaches_the_crm(hubspot, state):
    """Una tarjeta es "mira esto"; una ficha en el CRM es un compromiso."""
    p = persona(state=state)
    assert hubspot_sync.sincronizar_pendientes() == 0
    assert hubspot.notas == []
    assert estado(p["id"])["hubspot_estado"] is None


def test_nothing_older_than_the_mark_is_synced(hubspot):
    viejo = persona(slack="U1")
    envejecer(viejo["id"], horas=48)
    nuevo = persona(slack="U2", email="bo@acme.com")
    assert hubspot_sync.sincronizar_pendientes() == 1
    assert estado(viejo["id"])["hubspot_estado"] is None
    assert estado(nuevo["id"])["hubspot_estado"] == "sincronizada"


def test_the_first_pass_stamps_the_mark_and_syncs_no_history(hubspot):
    """El freno que no se negocia: una funcionalidad nueva ya inundó un canal
    una vez procesando meses de golpe. El CRM, además, no se deshace."""
    config("hubspot_desde", "")
    p = persona()
    envejecer(p["id"], horas=1)
    assert hubspot_sync.sincronizar_pendientes() == 0
    assert hubspot.notas == []
    assert estado(p["id"])["hubspot_estado"] is None
    marca = hubspot_sync.marca_desde()
    assert marca is not None and datetime.now(UTC) - marca < timedelta(minutes=5)
    # Y la marca no se vuelve a mover en la pasada siguiente.
    hubspot_sync.sincronizar_pendientes()
    assert hubspot_sync.marca_desde() == marca


def test_someone_contacted_after_the_first_pass_does_sync(hubspot):
    config("hubspot_desde", "")
    assert hubspot_sync.sincronizar_pendientes() == 0
    p = persona()
    assert hubspot_sync.sincronizar_pendientes() == 1
    assert estado(p["id"])["hubspot_estado"] == "sincronizada"


def test_the_daily_cap_stops_the_pass(hubspot):
    config("hubspot_por_dia", 2)
    for i in range(4):
        persona(slack=f"U{i}", email=f"u{i}@acme.com")
    assert hubspot_sync.sincronizar_pendientes() == 2
    assert len(hubspot.notas) == 2
    # Y en la misma jornada ya no cabe nadie más.
    assert hubspot_sync.sincronizar_pendientes() == 0
    assert len(hubspot.notas) == 2


def test_the_cap_counts_the_people_synced_today(hubspot):
    config("hubspot_por_dia", 1)
    persona(slack="U1")
    persona(slack="U2", email="bo@acme.com")
    assert hubspot_sync.sincronizar_pendientes() == 1
    assert hubspot_sync.cupo_restante() == 0


# --- la reserva ---------------------------------------------------------------


def test_a_person_is_never_synced_twice(hubspot):
    p = persona()
    assert hubspot_sync.sincronizar_pendientes() == 1
    assert hubspot_sync.sincronizar_pendientes() == 0
    assert len(hubspot.notas) == 1
    assert estado(p["id"])["hubspot_contact_id"] == "contacto-1"


def test_a_row_another_worker_already_took_is_left_alone(hubspot):
    """La reserva es un UPDATE condicionado: si no vuelve la fila, es de otro."""
    p = persona()
    filas = hubspot_sync.pendientes()
    assert len(filas) == 1
    db.execute("update prospects set hubspot_estado = 'reservada' where id = %s", (p["id"],))
    # La lista se hizo antes de que el otro worker reservara: aun así no se escribe.
    assert hubspot_sync._pasada(filas) == 0
    assert hubspot.notas == []
    assert estado(p["id"])["hubspot_estado"] == "reservada"


# --- fallos y reintentos ------------------------------------------------------


def test_a_hubspot_failure_releases_the_reservation_and_retries(hubspot):
    p = persona()
    hubspot.falla = RuntimeError("hubspot 500")
    assert hubspot_sync.sincronizar_pendientes() == 0
    fila = estado(p["id"])
    assert fila["hubspot_estado"] is None and fila["hubspot_intentos"] == 1
    hubspot.falla = None
    assert hubspot_sync.sincronizar_pendientes() == 1
    assert estado(p["id"])["hubspot_estado"] == "sincronizada"


def test_after_three_attempts_the_person_is_skipped_with_a_reason(hubspot):
    p = persona()
    hubspot.falla = RuntimeError("hubspot 500")
    for _ in range(hubspot_sync.MAX_INTENTOS):
        assert hubspot_sync.sincronizar_pendientes() == 0
    fila = estado(p["id"])
    assert fila["hubspot_estado"] == "omitida"
    assert fila["hubspot_motivo"] == hubspot_sync.MOTIVO_FALLO
    assert fila["hubspot_intentos"] == hubspot_sync.MAX_INTENTOS


def test_a_failure_stops_the_pass_instead_of_hammering_hubspot(hubspot):
    for i in range(3):
        persona(slack=f"U{i}", email=f"u{i}@acme.com")
    hubspot.falla = RuntimeError("hubspot 500")
    assert hubspot_sync.sincronizar_pendientes() == 0
    pendientes = db.fetch_all("select hubspot_intentos from prospects where hubspot_estado is null")
    # Solo la primera gastó un intento; las otras dos siguen intactas.
    assert sorted(f["hubspot_intentos"] for f in pendientes) == [0, 0, 1]


def test_a_missing_key_never_burns_an_attempt(hubspot, monkeypatch):
    """Sin llave no hay nada que reintentar: nadie puede acabar `omitida` por eso."""

    class HubSpotNoConfigurado(RuntimeError):
        pass

    p = persona()
    monkeypatch.setattr(
        hubspot_sync, "_cliente", lambda: (_ for _ in ()).throw(HubSpotNoConfigurado("sin llave"))
    )
    assert hubspot_sync.sincronizar_pendientes() == 0
    fila = estado(p["id"])
    assert fila["hubspot_estado"] is None and fila["hubspot_intentos"] == 0


def test_a_paused_integration_gives_the_reservation_back_and_stops_the_pass(hubspot):
    class HubSpotPausado(RuntimeError):
        pass

    for i in range(3):
        persona(slack=f"U{i}", email=f"u{i}@acme.com")
    hubspot.falla = HubSpotPausado("en pausa")
    assert hubspot_sync.sincronizar_pendientes() == 0
    filas = db.fetch_all("select hubspot_estado, hubspot_intentos from prospects")
    # Nadie gasta un intento, y la pasada no recorre a los demás para
    # reservarlos y devolverlos uno a uno.
    assert all(f["hubspot_estado"] is None and f["hubspot_intentos"] == 0 for f in filas)


def test_the_pass_never_raises(hubspot, monkeypatch):
    persona()
    monkeypatch.setattr(
        hubspot_sync, "pendientes", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert hubspot_sync.sincronizar_pendientes() == 0


# --- qué se escribe -----------------------------------------------------------


def test_company_contact_association_and_note(hubspot):
    p = persona()
    assert hubspot_sync.sincronizar_pendientes() == 1
    dominio, propiedades = hubspot.empresas[0]
    assert dominio == "acme.com"
    assert propiedades["name"] == "Acme"
    email, props_contacto = hubspot.contactos[0]
    assert email == "ada@acme.com"
    assert (props_contacto["firstname"], props_contacto["lastname"]) == ("Ada", "Ruiz")
    assert props_contacto[hubspot_sync.PROP_FIT] == 3
    assert props_contacto[hubspot_sync.PROP_ORIGEN] == hubspot_sync.ORIGEN
    # Su enumeración de siempre, con la opción del club.
    assert props_contacto[hubspot_sync.PROP_CONTACTO_ORIGEN] == hubspot_sync.ORIGEN
    assert hubspot.asociaciones == [("contacto-1", "empresa-1")]
    assert hubspot.notas[0]["contact_id"] == "contacto-1"
    assert hubspot.notas[0]["company_id"] == "empresa-1"
    fila = estado(p["id"])
    assert (fila["hubspot_contact_id"], fila["hubspot_company_id"]) == (
        "contacto-1",
        "empresa-1",
    )


def test_the_note_carries_the_fit_the_reason_what_they_asked_and_the_panel_link(hubspot):
    p = persona()
    db.execute(
        "insert into message_signals (prospect_id, channel_id, ts, detectada, tipo, cita, "
        "confianza) values (%s, %s, '1700000000.000100', true, 'soporte', %s, 'alta')",
        (p["id"], CANAL, CITA),
    )
    hubspot_sync.sincronizar_pendientes()
    nota = hubspot.notas[0]["texto"]
    assert "Fit 3/3" in nota
    assert "Six open support roles right now" in nota
    assert "What they asked for: support" in nota
    assert f"https://panel.handoff.test/personas/{p['id']}" in nota


def test_our_own_properties_carry_the_fit_the_reason_the_source_and_the_link(hubspot):
    p = persona()
    hubspot_sync.sincronizar_pendientes()
    _, props = hubspot.contactos[0]
    assert props[hubspot_sync.PROP_RAZON] == "Six open support roles right now"
    assert props[hubspot_sync.PROP_PANEL] == f"https://panel.handoff.test/personas/{p['id']}"
    _, props_empresa = hubspot.empresas[0]
    assert props_empresa[hubspot_sync.PROP_ORIGEN] == hubspot_sync.ORIGEN


def test_the_custom_properties_are_created_before_anything_is_written(hubspot):
    persona()
    hubspot_sync.sincronizar_pendientes()
    assert hubspot.propiedades == 1


# --- el dominio no confirmado -------------------------------------------------


def test_an_unconfirmed_domain_is_never_written(hubspot):
    """Un dominio adivinado metería este lead en la ficha de una empresa ajena."""
    dossier = {**DOSSIER_OK, "empresa_no_confirmada": {"dominio_adivinado": "handoff.ai"}}
    p = persona(dominio="handoff.ai", dossier=dossier)
    assert hubspot_sync.sincronizar_pendientes() == 1
    assert hubspot.empresas == []
    assert "handoff.ai" not in hubspot.enviado()
    # El contacto y la nota sí: el lead no se pierde, y el motivo queda escrito.
    assert hubspot.contactos and hubspot.notas[0]["company_id"] is None
    fila = estado(p["id"])
    assert fila["hubspot_company_id"] is None
    assert fila["hubspot_motivo"] == hubspot_sync.MOTIVO_SIN_EMPRESA_NO_CONFIRMADA


def test_a_website_set_by_hand_does_confirm_the_company(hubspot):
    dossier = {**DOSSIER_OK, "empresa_no_confirmada": {"dominio_adivinado": "handoff.ai"}}
    persona(dominio="handoff.ai", dossier=dossier, override_dominio="acme.io")
    assert hubspot_sync.sincronizar_pendientes() == 1
    assert hubspot.empresas[0][0] == "acme.io"
    assert "handoff.ai" not in hubspot.enviado()


def test_without_a_website_there_is_no_company(hubspot):
    p = persona(dominio=None)
    assert hubspot_sync.sincronizar_pendientes() == 1
    assert hubspot.empresas == []
    assert estado(p["id"])["hubspot_motivo"] == hubspot_sync.MOTIVO_SIN_EMPRESA_SIN_DOMINIO


# --- el correo gratuito --------------------------------------------------------


@pytest.mark.parametrize("email", ["ada@gmail.com", "ada@yahoo.com", "ada@proton.me"])
def test_a_free_mail_address_never_creates_a_contact(hubspot, email):
    """Duplicaría al contacto que el equipo ya tiene con su correo de trabajo, y
    las fusiones de HubSpot no se deshacen."""
    p = persona(email=email)
    assert hubspot_sync.sincronizar_pendientes() == 1
    assert hubspot.contactos == []
    assert email not in hubspot.enviado()
    # La empresa y la nota sí se hacen; el contacto lo decide una persona.
    assert hubspot.empresas and hubspot.notas[0]["company_id"] == "empresa-1"
    assert hubspot.notas[0]["contact_id"] is None
    fila = estado(p["id"])
    assert fila["hubspot_contact_id"] is None
    assert fila["hubspot_motivo"] == hubspot_sync.MOTIVO_SIN_CONTACTO_FREE_MAIL


def test_without_an_email_there_is_no_contact(hubspot):
    p = persona(email=None)
    assert hubspot_sync.sincronizar_pendientes() == 1
    assert hubspot.contactos == []
    assert estado(p["id"])["hubspot_motivo"] == hubspot_sync.MOTIVO_SIN_CONTACTO_SIN_CORREO


def test_with_no_company_and_no_contact_nothing_is_written(hubspot):
    p = persona(email="ada@gmail.com", dominio=None)
    assert hubspot_sync.sincronizar_pendientes() == 0
    assert hubspot.notas == []
    fila = estado(p["id"])
    assert fila["hubspot_estado"] == "omitida"
    assert fila["hubspot_motivo"] == hubspot_sync.MOTIVO_SIN_DESTINO


# --- lo que NUNCA puede salir --------------------------------------------------


def test_nothing_private_ever_reaches_the_payload(hubspot):
    """Las cuatro fuentes privadas a la vez: DMs, cita, canal y proveedor.

    HubSpot recibe la conclusión; la prueba se queda en el panel, detrás del
    enlace de la nota."""
    dossier = {
        **DOSSIER_OK,
        "datos_proveedor": {"empleados_linkedin": 60, "sede": {"ciudad": PROVEEDOR}},
    }
    p = persona(dossier=dossier)
    db.execute(
        "insert into message_signals (prospect_id, channel_id, ts, detectada, tipo, cita, "
        "confianza) values (%s, %s, '1700000000.000100', true, 'soporte', %s, 'alta')",
        (p["id"], CANAL, CITA),
    )
    db.execute(
        "insert into dm_rastro (dm_id, team_id, slack_user_id, prospect_id, ultimo_ts, "
        "ultimo_de) values ('D123ABC', 'T1ABC', 'U1', %s, '1700000000.000200', 'otro')",
        (p["id"],),
    )
    db.execute(
        "insert into dm_mensajes (dm_id, ts, de, texto) "
        "values ('D123ABC', '1700000000.000200', 'otro', %s)",
        (DM_TEXTO,),
    )
    db.execute(
        "insert into dm_sugerencias (dm_id, ultimo_ts, contexto, sugerencia) "
        "values ('D123ABC', '1700000000.000200', 'esperando precio', %s)",
        (SUGERENCIA,),
    )

    assert hubspot_sync.sincronizar_pendientes() == 1
    enviado = hubspot.enviado()
    assert CITA not in enviado
    assert CANAL not in enviado
    assert "D123ABC" not in enviado
    assert DM_TEXTO not in enviado
    assert SUGERENCIA not in enviado
    assert PROVEEDOR not in enviado
    assert "1700000000" not in enviado
    # Y sí sale la conclusión: el tipo de lo que pidió, que es etiqueta nuestra.
    assert "What they asked for: support" in hubspot.notas[0]["texto"]


def test_a_dossier_with_no_evidence_never_reaches_the_crm(hubspot):
    """Para una tarjeta basta con bajar de banda; para el CRM, no."""
    dossier = {
        **DOSSIER_OK,
        "encaje_handoff": {"puntuacion": 3, "evidencia": "ninguna", "razon": "likely needs help"},
    }
    p = persona(dossier=dossier)
    assert hubspot_sync.sincronizar_pendientes() == 0
    assert hubspot.enviado() == FakeHubSpot().enviado()
    fila = estado(p["id"])
    assert fila["hubspot_estado"] == "omitida"
    assert fila["hubspot_motivo"] == hubspot_sync.MOTIVO_SIN_EVIDENCIA


def test_an_old_dossier_without_the_evidence_field_is_not_assumed_good(hubspot):
    dossier = {**DOSSIER_OK, "encaje_handoff": {"puntuacion": 3, "razon": "r"}}
    p = persona(dossier=dossier)
    assert hubspot_sync.sincronizar_pendientes() == 0
    assert estado(p["id"])["hubspot_motivo"] == hubspot_sync.MOTIVO_SIN_EVIDENCIA


def test_a_degraded_dossier_never_reaches_the_crm(hubspot):
    p = persona(dossier={**DOSSIER_OK, "degradado": True})
    assert hubspot_sync.sincronizar_pendientes() == 0
    assert hubspot.notas == []
    assert estado(p["id"])["hubspot_motivo"] == hubspot_sync.MOTIVO_DEGRADADO


def test_without_a_dossier_there_is_nothing_to_conclude(hubspot):
    p = persona(dossier=None)
    assert hubspot_sync.sincronizar_pendientes() == 0
    assert estado(p["id"])["hubspot_motivo"] == hubspot_sync.MOTIVO_SIN_DOSSIER


def test_a_slack_link_inside_the_dossier_blocks_the_whole_person(hubspot):
    """La razón la escribe un modelo a partir de webs ajenas: si trae un enlace
    de Slack dentro, no se escribe nada de esa persona."""
    dossier = {
        **DOSSIER_OK,
        "encaje_handoff": {
            "puntuacion": 3,
            "evidencia": "vacantes",
            "razon": "Said so in https://founders.slack.com/archives/C08MRLP46G6/p1700000000",
        },
    }
    p = persona(dossier=dossier)
    assert hubspot_sync.sincronizar_pendientes() == 0
    assert hubspot.enviado() == FakeHubSpot().enviado()
    assert estado(p["id"])["hubspot_motivo"] == hubspot_sync.MOTIVO_DATO_PRIVADO


def test_a_channel_id_in_a_company_name_blocks_the_whole_person(hubspot):
    p = persona()
    db.execute("update prospects set company_name_override = %s where id = %s", (CANAL, p["id"]))
    assert hubspot_sync.sincronizar_pendientes() == 0
    assert hubspot.enviado() == FakeHubSpot().enviado()
    assert estado(p["id"])["hubspot_motivo"] == hubspot_sync.MOTIVO_DATO_PRIVADO


def test_an_ordinary_uppercase_word_is_not_mistaken_for_a_channel(hubspot):
    """Los ids de canal llevan dígitos; "COMPLIANCE" no puede frenar una ficha."""
    assert not hubspot_sync._tiene_rastro_de_slack("COMPLIANCE and CUSTOMER SUPPORT")
    assert hubspot_sync._tiene_rastro_de_slack(f"posted in {CANAL}")


# --- el backfill ----------------------------------------------------------------


def test_the_backfill_is_the_only_way_in_for_the_backlog(hubspot):
    p = persona()
    envejecer(p["id"], horas=72)
    assert hubspot_sync.sincronizar_pendientes() == 0
    assert hubspot_sync.sincronizar_backfill(limite=10) == 1
    assert estado(p["id"])["hubspot_estado"] == "sincronizada"


def test_the_backfill_respects_its_limit(hubspot):
    for i in range(4):
        p = persona(slack=f"U{i}", email=f"u{i}@acme.com")
        envejecer(p["id"], horas=72)
    assert hubspot_sync.sincronizar_backfill(limite=2) == 2
    assert len(hubspot.notas) == 2


def test_the_backfill_does_nothing_with_the_switch_off(hubspot):
    p = persona()
    envejecer(p["id"], horas=72)
    config("hubspot_activo", False)
    assert hubspot_sync.sincronizar_backfill(limite=10) == 0
    assert hubspot.notas == []


def test_the_dry_run_plan_writes_nothing(hubspot):
    buena = persona(slack="U1")
    mala = persona(slack="U2", email="bo@gmail.com", dominio=None)
    envejecer(buena["id"], horas=72)
    envejecer(mala["id"], horas=72)
    previsto = hubspot_sync.plan_backfill(limite=10)
    assert len(previsto) == 2
    planes = {str(p["id"]): (plan, motivo) for p, plan, motivo in previsto}
    assert planes[str(buena["id"])][0].email == "ada@acme.com"
    assert planes[str(mala["id"])][1] == hubspot_sync.MOTIVO_SIN_DESTINO
    assert hubspot.enviado() == FakeHubSpot().enviado()
    assert estado(buena["id"])["hubspot_estado"] is None


def test_the_cli_dry_run_prints_what_would_be_synced_and_writes_nothing(hubspot, capsys):
    buena = persona(slack="U1")
    mala = persona(slack="U2", email="bo@acme.com", dossier={**DOSSIER_OK, "degradado": True})
    envejecer(buena["id"], horas=72)
    envejecer(mala["id"], horas=72)
    assert cli.main(["hubspot-backfill", "--limit", "10", "--dry-run"]) == 0
    salida = capsys.readouterr().out
    assert "Ada Ruiz" in salida and "acme.com" in salida
    assert hubspot_sync.MOTIVO_DEGRADADO in salida
    assert "1 de 2 se escribirían en HubSpot" in salida
    assert hubspot.enviado() == FakeHubSpot().enviado()


def test_the_cli_backfill_syncs_the_backlog(hubspot, capsys):
    p = persona()
    envejecer(p["id"], horas=72)
    assert cli.main(["hubspot-backfill", "--limit", "10"]) == 0
    assert "1 persona(s) sincronizada(s)" in capsys.readouterr().out
    assert estado(p["id"])["hubspot_estado"] == "sincronizada"
