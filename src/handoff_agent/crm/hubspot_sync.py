"""De una persona contactada a su ficha en el HubSpot de Handoff.

Hasta aquí una tarjeta en Slack era todo el rastro que dejaba un lead. Cuando
AG pulsa "Contacted" -- o alguien pone a la persona en `cliente` -- alguien se
ha comprometido con esa relación, y es entonces, y solo entonces, cuando se
escribe en el CRM (ver `supabase/migrations/0035_hubspot.sql`). Una tarjeta es
"mira esto"; una ficha en el CRM significa que el equipo comercial cuenta con
ella en sus informes.

Cómo se escribe, y por qué así:

- **Reservar antes de llamar**, igual que `signals/alertas.py` y
  `delivery/deliver.py`: `hubspot_estado` pasa a `reservada` con un UPDATE
  condicionado a que siguiera nula. Si no vuelve la fila, otro worker la tiene
  y aquí no se hace nada. Sin eso, dos workers (o un reintento tras un fallo al
  anotar) crearían dos veces la misma nota, y en HubSpot no hay deshacer.
- Un fallo al escribir libera la reserva y se reintenta en la vuelta siguiente,
  hasta `MAX_INTENTOS`; después la fila queda `omitida` con un motivo legible,
  que se ve en el panel y en `persona_ver`.
- Nada de esto puede propagarse: `sincronizar_pendientes` nunca lanza. El CRM
  es un extra; no puede tumbar el bucle ni la cola de research.

**El histórico no entra solo.** `hubspot_desde` nace vacío en config: la
primera pasada escribe ahí `now()` y no sincroniza nada anterior. Ya nos pasó
una vez que una funcionalidad nueva procesara meses de golpe y llenara un canal
de Slack; aquí el destino es el CRM del equipo comercial, así que el mismo
error sería peor y además irreversible. El backlog existente se sube a mano,
con `handoff hubspot-backfill --limit N`, mirando antes qué entraría con
`--dry-run`.

**Qué NO sale nunca de aquí**, por código y no por costumbre. HubSpot recibe la
conclusión; la prueba se queda en el panel, detrás del enlace de la nota:

- Nada que venga de los mensajes directos (`dm_rastro`, `dm_mensajes`,
  `dm_sugerencias`): este módulo no consulta esas tablas. Ni el texto, ni la
  sugerencia, ni los tiempos.
- Ni `message_signals.cita`: es la frase literal de alguien en una comunidad
  privada ajena. De las señales solo se lee `tipo`, que es una etiqueta nuestra
  de una lista cerrada (ver `_PEDIDOS_SQL`).
- Ni ids de canal ni permalinks: no se leen, y además `_auditar` revisa cada
  cadena que va a salir y corta la sincronización si aparece alguno.
- Ni los datos del proveedor externo (`datos_proveedor`), que la tarjeta
  etiqueta "unverified": sin fuente citable, no entran en el CRM.
- Ni un dossier `degradado` (la búsqueda no funcionó) ni una persona
  `incompleto`: lo que diga ahí no está comprobado.
- Ni nada cuyo `encaje_handoff.evidencia` no sea una de las tres pruebas reales
  (`vacantes`, `necesidad_declarada`, `equipo_relevante`). "ninguna" significa
  que el propio modelo dice que la nota no se apoya en nada; un dossier viejo
  sin el campo tampoco lo sabe. Para una tarjeta eso basta para bajar de banda
  (`delivery/bands.py`); para una escritura permanente en el CRM, no.

Las dos decisiones que evitan una fusión imposible de deshacer:

- **Dominio no confirmado** (`empresa_no_confirmada`, que pone
  `research/worker.py`): nunca se escribe ese dominio. Un dominio adivinado
  mete nuestro lead dentro del registro de una empresa desconocida. Y como el
  cliente solo sabe buscar una empresa por dominio, una empresa "solo con
  nombre" no se podría volver a encontrar ni deduplicar: se multiplicaría en
  silencio y los informes del equipo contarían cuentas que no existen. Así que
  en ese caso no se escribe empresa: va el contacto y la nota, y el motivo
  queda escrito para que una persona la ate a la cuenta correcta desde el
  panel. Un dominio puesto a mano (`company_domain_override`) sí vale: lo
  confirmó una persona.
- **Correo gratuito** (`ingest/profile_hints.FREE_MAIL_DOMAINS`): no se crea
  contacto. El equipo ya suele tener a esa persona con su correo de trabajo, y
  crear otro contacto con su Gmail duplica a alguien que luego hay que fusionar
  a mano -- y las fusiones de HubSpot no se deshacen. Se hacen la empresa y la
  nota, el motivo queda escrito, y el contacto lo decide una persona.

El cliente de la API (`tools/hubspot.py`) lo escribe otro módulo: aquí solo se
llama. Se importa tarde, dentro de `_cliente`, para que este fichero -- y con
él `ingest/loop.py` -- se pueda importar aunque ese otro todavía no exista.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .. import db, db_config, guards, ledger
from ..config import load_settings
from ..delivery.alerta_card import tipo_label
from ..ingest.profile_hints import FREE_MAIL_DOMAINS
from ..research.worker import DEGRADED_KEY, EMPRESA_NO_CONFIRMADA_KEY

logger = logging.getLogger(__name__)

MAX_INTENTOS = 3
# Solo estos dos estados llegan al CRM (ver la cabecera de la migración 0035).
# `incompleto` queda fuera por este mismo filtro: un dossier a medias no se
# escribe en ningún sitio permanente.
ESTADOS_SINCRONIZABLES = ("contactado", "cliente")
CLAVE_DESDE = "hubspot_desde"
CLAVE_ACTIVO = "hubspot_activo"
CLAVE_POR_DIA = "hubspot_por_dia"
POR_DIA_DEFECTO = 25

# Las tres evidencias que son una prueba de verdad. "ninguna" -- y un dossier
# antiguo que ni siquiera trae el campo -- no entra en el CRM.
EVIDENCIAS_CON_PRUEBA = ("vacantes", "necesidad_declarada", "equipo_relevante")

ACCION_PARADA = "hubspot_parada"
ACCION_PRIMERA_MARCA = "hubspot_primera_marca"
ACCION_SINCRONIZADA = "hubspot_sincronizada"
ACCION_FALLIDA = "hubspot_fallida"

# Motivos (`prospects.hubspot_motivo`): los lee una persona en el panel y en
# `persona_ver`, así que van en inglés como todo lo que se ve.
MOTIVO_SIN_DOSSIER = "no_dossier"
MOTIVO_ESTADO = "state_not_syncable"
MOTIVO_DEGRADADO = "degraded_dossier"
MOTIVO_SIN_EVIDENCIA = "no_evidence"
MOTIVO_SIN_DESTINO = "no_company_and_no_contact"
MOTIVO_DATO_PRIVADO = "private_data_blocked"
MOTIVO_FALLO = "sync_failed"
# Motivos de una sincronización que SÍ se hizo, dejando algo fuera a propósito.
MOTIVO_SIN_EMPRESA_NO_CONFIRMADA = "company_skipped: domain not confirmed"
MOTIVO_SIN_EMPRESA_SIN_DOMINIO = "company_skipped: no domain on file"
MOTIVO_SIN_CONTACTO_FREE_MAIL = "contact_skipped: free-mail address"
MOTIVO_SIN_CONTACTO_SIN_CORREO = "contact_skipped: no email on file"

# Nuestros campos propios en su portal. `asegurar_propiedades()` (en
# `tools/hubspot.py`) es quien los crea; aquí solo se rellenan.
PROP_FIT = "handoff_fit_score"
PROP_RAZON = "handoff_fit_reason"
PROP_ORIGEN = "handoff_source"
PROP_PANEL = "handoff_panel_url"
# La enumeración `source` que ya existe en sus contactos, con la opción nueva.
PROP_CONTACTO_ORIGEN = "source"
ORIGEN = "Founders Club"

# Tope de la razón de encaje en el CRM: una o dos frases, como pide el prompt
# de la síntesis. Recortar aquí evita meter un párrafo entero en una propiedad.
RAZON_MAX = 500


class NoSincronizable(Exception):
    """Esta persona no se escribe en el CRM, y es una decisión final.

    Lleva el motivo que se queda en `hubspot_motivo`: no es un fallo que se
    reintente, así que la fila pasa a `omitida` sin gastar intentos.
    """

    def __init__(self, motivo: str) -> None:
        super().__init__(motivo)
        self.motivo = motivo


# --- lo que nunca puede salir de aquí ----------------------------------------

# Un id de canal o de DM de Slack (C/D + mayúsculas y dígitos). Se exige al
# menos un dígito porque los ids reales lo llevan (C08MRLP46G6) y así una
# palabra en mayúsculas del dossier no se confunde con uno.
_ID_SLACK = re.compile(r"\b[CD][A-Z0-9]{7,}\b")
_ENLACE_SLACK = re.compile(r"slack\.com", re.IGNORECASE)


def _tiene_rastro_de_slack(texto: str) -> bool:
    if _ENLACE_SLACK.search(texto):
        return True
    return any(any(c.isdigit() for c in m.group()) for m in _ID_SLACK.finditer(texto))


def _auditar(textos: list[str]) -> None:
    """Última red antes de llamar a HubSpot.

    Lo que se manda se construye campo a campo más abajo, así que un id de
    canal o un permalink no deberían llegar nunca aquí. Pero la razón de encaje
    y el nombre de la empresa los escribe un modelo a partir de webs ajenas, y
    una sola frase copiada con un enlace de Slack dentro bastaría para publicar
    en el CRM del equipo comercial de dónde sale todo esto. Si aparece, no se
    escribe nada de esa persona: queda `omitida` con su motivo.
    """
    for texto in textos:
        if texto and _tiene_rastro_de_slack(texto):
            raise NoSincronizable(MOTIVO_DATO_PRIVADO)


# --- la marca desde la que se sincroniza -------------------------------------


def _guardar_desde(marca: datetime) -> None:
    db.execute(
        "insert into config (key, value, updated_at) values (%s, %s::jsonb, now()) "
        "on conflict (key) do update set value = excluded.value, updated_at = now()",
        (CLAVE_DESDE, json.dumps(marca.isoformat())),
    )


def marca_desde() -> datetime | None:
    """`hubspot_desde` como fecha, o None si todavía no se ha escrito.

    No escribe: una lectura no puede tener efectos. Quien la estampa es
    `sincronizar_pendientes`, una sola vez, antes de mirar qué hay pendiente.
    """
    valor = db_config.value(CLAVE_DESDE, "")
    if not isinstance(valor, str) or not valor.strip():
        return None
    try:
        fecha = datetime.fromisoformat(valor.strip())
    except ValueError:
        # Un valor corrupto no puede significar "sincronízalo todo": se trata
        # como si no hubiera marca y la pasada lo reescribe con `now()`.
        logger.warning("hubspot_desde: valor ilegible %r, se vuelve a marcar", valor)
        return None
    return fecha if fecha.tzinfo else fecha.replace(tzinfo=UTC)


def _marcar_primera_vez(ahora: datetime) -> datetime:
    """La primera pasada escribe `now()` y no sincroniza nada anterior."""
    marca = marca_desde()
    if marca is not None:
        return marca
    _guardar_desde(ahora)
    logger.info("hubspot: primera pasada, se marca %s; el histórico no entra", ahora.isoformat())
    _registrar(ACCION_PRIMERA_MARCA, {"desde": ahora.isoformat()})
    return ahora


# --- topes y candidatos ------------------------------------------------------


def _inicio_del_dia(ahora: datetime) -> datetime:
    """El mismo corte de día que el resto de topes del sistema (`zona_horaria`)."""
    zona = ZoneInfo(db_config.value("zona_horaria", "America/New_York"))
    return ahora.astimezone(zona).replace(hour=0, minute=0, second=0, microsecond=0)


def _sincronizadas_hoy(desde: datetime) -> int:
    """Las de hoy, contando también las reservadas: una reserva en curso ocupa
    sitio del cupo igual que una escritura terminada."""
    return db.fetch_one(
        "select count(*) as n from prospects "
        "where hubspot_estado in ('reservada', 'sincronizada') and hubspot_at >= %s",
        (desde,),
    )["n"]


def cupo_restante(ahora: datetime | None = None) -> int:
    ahora = ahora or datetime.now(UTC)
    tope = db_config.value(CLAVE_POR_DIA, POR_DIA_DEFECTO)
    return max(0, tope - _sincronizadas_hoy(_inicio_del_dia(ahora)))


def _candidatos(desde: datetime | None, limite: int | None) -> list[dict]:
    """Personas sin decisión de CRM, de la más antigua a la más reciente.

    `desde` nulo es solo del backfill: la pasada del bucle siempre trae la
    marca. El orden por `updated_at` ascendente hace que el backlog salga en el
    orden en que se contactó a la gente, no al revés.
    """
    if limite is not None and limite <= 0:
        return []
    return db.fetch_all(
        """
        select id, slack_user_id, full_name, email, state,
               company_name, company_name_override,
               company_domain, company_domain_override, hubspot_intentos
          from prospects
         where state = any(%s)
           and hubspot_estado is null
           and (%s::timestamptz is null or updated_at >= %s::timestamptz)
         order by updated_at
         limit %s
        """,
        (list(ESTADOS_SINCRONIZABLES), desde, desde, limite),
    )


def pendientes(ahora: datetime | None = None) -> list[dict]:
    """Lo que le toca a la pasada del bucle: `contactado` o `cliente`, sin
    decisión de CRM, no anterior a `hubspot_desde`, y como mucho lo que quede
    del tope diario.

    Sin marca todavía (primera pasada) no hay nada pendiente: el histórico no
    entra solo. `sincronizar_pendientes` estampa la marca antes de llamar aquí.
    """
    ahora = ahora or datetime.now(UTC)
    desde = marca_desde()
    if desde is None:
        return []
    return _candidatos(desde, cupo_restante(ahora))


def pendientes_backfill(limite: int) -> list[dict]:
    """El backlog entero, sin la marca y sin el tope diario: el histórico que
    una persona decide subir a mano, con su propio límite (ver `cli.py`)."""
    return _candidatos(None, limite)


# --- el plan de escritura de una persona -------------------------------------


@dataclass(frozen=True)
class Plan:
    """Exactamente lo que se va a mandar, ya decidido y ya filtrado."""

    dominio: str | None  # None: no se escribe empresa
    propiedades_empresa: dict = field(default_factory=dict)
    email: str | None = None  # None: no se escribe contacto
    propiedades_contacto: dict = field(default_factory=dict)
    nota: str = ""
    # Lo que se dejó fuera a propósito, para que se vea en el panel.
    motivo: str | None = None


def _dossier(prospect_id: str) -> dict | None:
    fila = db.fetch_one(
        "select content from dossiers where prospect_id = %s order by version desc limit 1",
        (prospect_id,),
    )
    return fila["content"] if fila else None


# De las señales solo el tipo, que es una etiqueta nuestra de una lista cerrada
# (migración 0021). Ni `cita` -- la frase literal de alguien en una comunidad
# privada -- ni `channel_id`: no se seleccionan, así que no pueden salir.
_PEDIDOS_SQL = (
    "select distinct tipo from message_signals "
    "where prospect_id = %s and detectada and tipo is not null"
)


def _pedidos(prospect_id: str) -> list[str]:
    filas = db.fetch_all(_PEDIDOS_SQL, (prospect_id,))
    return sorted({tipo_label(f["tipo"]) for f in filas})


def _enlace_panel(prospect_id) -> str | None:
    base = load_settings().panel_url
    return f"{base}/personas/{prospect_id}" if base else None


def _nombre_empresa(persona: dict, dossier: dict) -> str | None:
    nombre = persona.get("company_name_override") or None
    if not nombre:
        del_dossier = (dossier.get("empresa") or {}).get("nombre")
        nombre = del_dossier if isinstance(del_dossier, str) and del_dossier.strip() else None
    return (nombre or persona.get("company_name") or "").strip() or None


def _dominio(persona: dict, dossier: dict) -> tuple[str | None, str | None]:
    """El dominio que se puede escribir, y el motivo si no hay ninguno.

    Un dominio puesto a mano en el panel vale siempre: lo confirmó una persona.
    Sin él, el de `prospects` solo si el dossier no viene marcado como empresa
    no confirmada -- escribir un dominio adivinado mete este lead en el
    registro de una empresa desconocida, y eso no se deshace.
    """
    manual = (persona.get("company_domain_override") or "").strip().casefold()
    if manual:
        return manual, None
    if dossier.get(EMPRESA_NO_CONFIRMADA_KEY):
        return None, MOTIVO_SIN_EMPRESA_NO_CONFIRMADA
    adivinado = (persona.get("company_domain") or "").strip().casefold()
    if not adivinado:
        return None, MOTIVO_SIN_EMPRESA_SIN_DOMINIO
    return adivinado, None


def _correo(persona: dict) -> tuple[str | None, str | None]:
    """El correo con el que se puede crear contacto, y el motivo si no.

    Un correo gratuito no crea contacto: duplicaría a alguien que el equipo ya
    tiene con su correo de trabajo, y la fusión en HubSpot es irreversible.
    """
    email = (persona.get("email") or "").strip().casefold()
    if not email or "@" not in email:
        return None, MOTIVO_SIN_CONTACTO_SIN_CORREO
    if email.rsplit("@", 1)[1] in FREE_MAIL_DOMAINS:
        return None, MOTIVO_SIN_CONTACTO_FREE_MAIL
    return email, None


def _nota(fit: dict, pedidos: list[str], enlace: str | None) -> str:
    """La nota del CRM: la conclusión y el enlace a la prueba, nada más.

    Lleva la nota de encaje, su razón, qué pidió esta persona (la etiqueta del
    tipo de señal, nunca la cita) y el enlace a su ficha del panel, que es
    donde vive la evidencia con sus fuentes.
    """
    lineas = [f"Handoff agent · lead from the {ORIGEN} community"]
    puntuacion = fit.get("puntuacion")
    razon = fit.get("razon")
    lineas.append(f"Fit {puntuacion}/3" if type(puntuacion) is int else "Fit: not scored")
    if isinstance(razon, str) and razon.strip():
        lineas.append(f"Why: {razon.strip()[:RAZON_MAX]}")
    if pedidos:
        lineas.append(f"What they asked for: {', '.join(pedidos)}")
    if enlace:
        lineas.append(f"Full research, evidence and sources: {enlace}")
    return "\n".join(lineas)


def planificar(persona: dict, dossier: dict | None) -> Plan:
    """Qué se escribe de esta persona. Lanza `NoSincronizable` si nada se escribe."""
    if persona["state"] not in ESTADOS_SINCRONIZABLES:
        # El backfill y el CLI pueden traer cualquier fila; la regla de cuándo
        # se escribe en el CRM se comprueba aquí también, no solo en el SELECT.
        raise NoSincronizable(MOTIVO_ESTADO)
    if dossier is None:
        raise NoSincronizable(MOTIVO_SIN_DOSSIER)
    if dossier.get(DEGRADED_KEY):
        raise NoSincronizable(MOTIVO_DEGRADADO)

    fit = dossier.get("encaje_handoff")
    fit = fit if isinstance(fit, dict) else {}
    if fit.get("evidencia") not in EVIDENCIAS_CON_PRUEBA:
        raise NoSincronizable(MOTIVO_SIN_EVIDENCIA)

    dominio, motivo_empresa = _dominio(persona, dossier)
    email, motivo_contacto = _correo(persona)
    if dominio is None and email is None:
        # Sin empresa y sin contacto no hay a qué colgar la nota.
        raise NoSincronizable(MOTIVO_SIN_DESTINO)

    enlace = _enlace_panel(persona["id"])
    nota = _nota(fit, _pedidos(persona["id"]), enlace)
    razon = fit.get("razon")
    razon = razon.strip()[:RAZON_MAX] if isinstance(razon, str) and razon.strip() else None
    comunes = {PROP_ORIGEN: ORIGEN}
    if enlace:
        comunes[PROP_PANEL] = enlace

    propiedades_empresa: dict = {}
    if dominio:
        nombre = _nombre_empresa(persona, dossier)
        propiedades_empresa = {"domain": dominio, **comunes}
        if nombre:
            propiedades_empresa["name"] = nombre

    propiedades_contacto: dict = {}
    if email:
        propiedades_contacto = {
            "email": email,
            **comunes,
            # Su enumeración de siempre, con la opción nueva: así el lead
            # aparece en los informes que el equipo ya tiene montados.
            PROP_CONTACTO_ORIGEN: ORIGEN,
        }
        nombre, apellidos = _nombre_y_apellidos(persona.get("full_name"))
        if nombre:
            propiedades_contacto["firstname"] = nombre
        if apellidos:
            propiedades_contacto["lastname"] = apellidos
        # `type(...) is int` y no isinstance: bool es subclase de int, y un
        # `true` del modelo no puede acabar siendo la nota de encaje (misma
        # comprobación que delivery/bands.py y dossier.py).
        if type(fit.get("puntuacion")) is int:
            propiedades_contacto[PROP_FIT] = fit["puntuacion"]
        if razon:
            propiedades_contacto[PROP_RAZON] = razon

    motivo = " · ".join(m for m in (motivo_empresa, motivo_contacto) if m) or None
    plan = Plan(
        dominio=dominio,
        propiedades_empresa=propiedades_empresa,
        email=email,
        propiedades_contacto=propiedades_contacto,
        nota=nota,
        motivo=motivo,
    )
    _auditar(_textos(plan))
    return plan


def _textos(plan: Plan) -> list[str]:
    """Cada cadena que saldría hacia HubSpot, para auditarla de una vez."""
    valores = [plan.nota, plan.dominio or "", plan.email or ""]
    for propiedades in (plan.propiedades_empresa, plan.propiedades_contacto):
        valores += [str(v) for v in propiedades.values()]
    return valores


def _nombre_y_apellidos(nombre_completo: object) -> tuple[str | None, str | None]:
    if not isinstance(nombre_completo, str) or not nombre_completo.strip():
        return None, None
    partes = nombre_completo.strip().split()
    if len(partes) == 1:
        return partes[0], None
    return partes[0], " ".join(partes[1:])


# --- el cliente de la API (lo escribe tools/hubspot.py) ----------------------

# Las dos excepciones del cliente que no son culpa de esta persona: sin llave
# configurada o con la integración en pausa, no hay nada que reintentar y la
# reserva se devuelve sin gastar un intento. Se reconocen por nombre a
# propósito: así este módulo no tiene que importar `tools/hubspot.py` al
# cargarse, y `ingest/loop.py` arranca aunque ese fichero aún no exista.
PARADAS_HUBSPOT = ("HubSpotNoConfigurado", "HubSpotPausado")


def _es_parada(exc: BaseException) -> bool:
    return type(exc).__name__ in PARADAS_HUBSPOT


def _cliente():
    """El cliente de la API, importado tarde (ver la cabecera del módulo)."""
    from ..tools import hubspot

    return hubspot.HubSpot()


def _registrar(action: str, payload: dict, prospect_id: str | None = None) -> None:
    try:
        ledger.record_action(action, payload, prospect_id=prospect_id)
    except Exception:
        logger.exception("hubspot: no se pudo registrar %s en el ledger", action)


def _parada(exc: Exception) -> None:
    logger.warning("hubspot detenido: %s", exc)
    _registrar(ACCION_PARADA, {"motivo": f"{type(exc).__name__}: {exc}"})


# --- reserva, marca y liberación ---------------------------------------------


def _reservar(persona: dict) -> bool:
    return (
        db.fetch_one(
            "update prospects set hubspot_estado = 'reservada', hubspot_at = now(), "
            "hubspot_intentos = hubspot_intentos + 1 "
            "where id = %s and hubspot_estado is null returning id",
            (persona["id"],),
        )
        is not None
    )


def _omitir(prospect_id, motivo: str) -> None:
    """Decisión final sobre una fila ya reservada: no se reintenta.

    No se toca `hubspot_at`, que lo puso la reserva: así la omitida no cuenta
    para el cupo del día (solo cuentan `reservada` y `sincronizada`) pero
    tampoco se pierde cuándo se decidió.
    """
    db.execute(
        "update prospects set hubspot_estado = 'omitida', hubspot_motivo = %s "
        "where id = %s and hubspot_estado = 'reservada'",
        (motivo, prospect_id),
    )


def _liberar(persona: dict) -> None:
    """Tras un fallo al escribir: de vuelta a pendiente, o definitiva si ya se
    agotaron los intentos (la reserva ya contó el de ahora)."""
    if persona["hubspot_intentos"] + 1 >= MAX_INTENTOS:
        db.execute(
            "update prospects set hubspot_estado = 'omitida', hubspot_motivo = %s "
            "where id = %s and hubspot_estado = 'reservada'",
            (MOTIVO_FALLO, persona["id"]),
        )
    else:
        db.execute(
            "update prospects set hubspot_estado = null, hubspot_at = null "
            "where id = %s and hubspot_estado = 'reservada'",
            (persona["id"],),
        )


def _devolver_sin_gastar(persona: dict) -> None:
    """La integración no estaba disponible: la reserva vuelve atrás y el
    intento no cuenta. Nadie debe quedarse `omitida` por una llave que falta."""
    db.execute(
        "update prospects set hubspot_estado = null, hubspot_at = null, "
        "hubspot_intentos = greatest(hubspot_intentos - 1, 0) "
        "where id = %s and hubspot_estado = 'reservada'",
        (persona["id"],),
    )


def _anotar(persona: dict, plan: Plan, contact_id, company_id) -> None:
    db.execute(
        "update prospects set hubspot_estado = 'sincronizada', hubspot_contact_id = %s, "
        "hubspot_company_id = %s, hubspot_motivo = %s, hubspot_at = now() where id = %s",
        (contact_id, company_id, plan.motivo, persona["id"]),
    )


# --- la escritura ------------------------------------------------------------


def _escribir(cliente, plan: Plan) -> tuple[str | None, str | None]:
    """Empresa, contacto, asociación y nota, en ese orden: la asociación
    necesita los dos ids, y la nota se cuelga de lo que haya salido."""
    company_id = None
    contact_id = None
    if plan.dominio:
        company_id = cliente.crear_o_actualizar_empresa(plan.dominio, plan.propiedades_empresa)
    if plan.email:
        contact_id = cliente.crear_o_actualizar_contacto(plan.email, plan.propiedades_contacto)
        if company_id:
            cliente.asociar_contacto_con_empresa(contact_id, company_id)
    cliente.crear_nota(plan.nota, contact_id=contact_id, company_id=company_id)
    return contact_id, company_id


def _sincronizar_una(cliente, persona: dict) -> bool | None:
    """Escribe en HubSpot una persona ya reservada.

    True si se escribió, None si se decidió no escribirla (y queda `omitida`,
    así que la pasada sigue con las demás) y False si HubSpot falló o está en
    pausa, que es cosa de la integración y no de esta persona: ahí la pasada
    deja de insistir y las que quedan siguen pendientes.
    """
    prospect_id = str(persona["id"])
    try:
        plan = planificar(persona, _dossier(persona["id"]))
    except NoSincronizable as exc:
        # El plan se hace con la reserva en la mano, no antes: así lo que se
        # mira (dossier, señales, overrides) es lo que hay ahora mismo, y la
        # decisión -- escribir u omitir con su motivo -- la toma un solo
        # worker. Una omisión es final: no gasta más intentos.
        _omitir(persona["id"], exc.motivo)
        logger.info("hubspot: %s omitida (%s)", prospect_id, exc.motivo)
        return None

    try:
        contact_id, company_id = _escribir(cliente, plan)
    except Exception as exc:
        disponible = not _es_parada(exc)
        logger.exception("hubspot: no se pudo sincronizar %s", prospect_id)
        try:
            if disponible:
                _liberar(persona)
            else:
                _devolver_sin_gastar(persona)
        except Exception:
            logger.exception("hubspot: no se pudo liberar la reserva de %s", prospect_id)
        if not disponible:
            # La llave se fue o la integración está en pausa: insistir con las
            # demás solo gastaría reservas para devolverlas una por una.
            _parada(exc)
        else:
            _registrar(ACCION_FALLIDA, {"motivo": f"{type(exc).__name__}: {exc}"}, prospect_id)
        return False

    try:
        _anotar(persona, plan, contact_id, company_id)
    except Exception:
        # Ya está escrito en HubSpot: la fila se queda `reservada` (cuenta para
        # el tope y bloquea una segunda escritura) en vez de arriesgar un
        # duplicado que luego habría que fusionar a mano.
        logger.exception(
            "hubspot: escrito (contacto=%s, empresa=%s) pero no se pudo anotar %s",
            contact_id,
            company_id,
            prospect_id,
        )
    _registrar(
        ACCION_SINCRONIZADA,
        {"contact_id": contact_id, "company_id": company_id, "motivo": plan.motivo},
        prospect_id,
    )
    return True


def _pasada(filas: list[dict]) -> int:
    """Reserva y escribe cada fila. Devuelve cuántas se escribieron."""
    if not filas:
        return 0
    try:
        cliente = _cliente()
    except Exception as exc:  # noqa: BLE001 - sin cliente no se escribe nada y ya está
        # Sin llave, en pausa o con el módulo del cliente todavía sin escribir:
        # no se reserva nada. Un log informativo, sin traza: que falte la llave
        # es un estado normal, no un error. Si es una parada conocida, además
        # su fila en el ledger.
        logger.info("hubspot sin configurar o no disponible, se omite: %s", exc)
        if _es_parada(exc):
            _parada(exc)
        return 0

    try:
        cliente.asegurar_propiedades()
    except Exception as exc:  # noqa: BLE001 - mismo caso: la pasada se salta entera
        # Sin nuestros campos propios, lo que se escriba queda incompleto.
        logger.warning("hubspot: no se pudieron asegurar las propiedades: %s", exc)
        if _es_parada(exc):
            _parada(exc)
        return 0

    hechas = 0
    for persona in filas:
        if not _reservar(persona):
            # Otro worker la tiene.
            continue
        resultado = _sincronizar_una(cliente, persona)
        if resultado is None:
            continue
        if resultado:
            hechas += 1
        else:
            # HubSpot está fallando: no se insiste con las demás, que siguen
            # pendientes para la vuelta siguiente.
            break
    return hechas


def _sincronizar(ahora: datetime) -> int:
    if not db_config.value(CLAVE_ACTIVO, False):
        return 0
    try:
        guards.check_kill_switch()
        guards.check_monthly_budget()
    except (guards.KillSwitchActive, guards.MonthlyBudgetExceeded) as exc:
        _parada(exc)
        return 0

    # Antes de mirar qué hay pendiente: en la primera pasada esto deja la marca
    # en `now()` y la lista sale vacía. El histórico se sube con el backfill.
    _marcar_primera_vez(ahora)
    return _pasada(pendientes(ahora))


def sincronizar_pendientes(now: datetime | None = None) -> int:
    """Una pasada del CRM: devuelve cuántas personas se escribieron. Nunca lanza."""
    try:
        return _sincronizar(now or datetime.now(UTC))
    except Exception:
        logger.exception("hubspot: fallo inesperado; se reintenta en la vuelta siguiente")
        return 0


# --- el backfill del histórico (solo desde el CLI) ---------------------------


def plan_backfill(limite: int) -> list[tuple[dict, Plan | None, str | None]]:
    """Qué haría el backfill, sin tocar nada: cada persona con su plan, o con
    el motivo por el que se quedaría fuera. Es lo que imprime `--dry-run`."""
    salida = []
    for persona in pendientes_backfill(limite):
        try:
            salida.append((persona, planificar(persona, _dossier(persona["id"])), None))
        except NoSincronizable as exc:
            salida.append((persona, None, exc.motivo))
    return salida


def sincronizar_backfill(limite: int) -> int:
    """El histórico, a mano y con límite explícito.

    Se salta `hubspot_desde` -- es justo lo que viene de antes de la marca --
    y el tope diario, porque quien lo lanza ya ha elegido cuántas. Respeta el
    interruptor: con `hubspot_activo` apagado no se escribe nada.
    """
    if not db_config.value(CLAVE_ACTIVO, False):
        logger.info("hubspot: el interruptor está apagado, el backfill no escribe nada")
        return 0
    guards.check_kill_switch()
    guards.check_monthly_budget()
    return _pasada(pendientes_backfill(limite))
