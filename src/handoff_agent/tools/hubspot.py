"""Cliente del HubSpot propio de Handoff (portal 245974710): empresas, contactos y notas.

Este módulo es el único que escribe en el CRM. No toca Slack: ni el del Founders
Club (que es ajeno y de solo lectura) ni el de Handoff. Un test lo comprueba
leyendo el propio fichero, porque la regla importa más que la buena intención.

Qué hay detrás de cada decisión:

- **Autenticación.** Service Key en `Authorization: Bearer <key>`. Las private apps
  clásicas se retiraron en octubre de 2026, así que no hay fallback a `hapikey`.
  La llave la pega una persona en Settings y vive en la tabla `secretos`, que el
  panel escribe y nunca lee (migración 0035). Se relee en cada llamada, sin caché,
  para que una llave nueva valga en el siguiente ciclo sin reiniciar el worker.

- **Guardarraíles, antes de cada llamada que escribe, y en este orden:**
  1. Hay llave (si no, `HubSpotNoConfigurado`).
  2. `hubspot_activo` en config: apagado al nacer. Encenderlo es decisión humana.
  3. Kill switch (el de todo el sistema).
  4. Tope diario `hubspot_por_dia`, contando las acciones `hubspot_llamada` de hoy
     en `zona_horaria`. El ledger es el contador; por eso cada llamada se registra
     aunque falle.

- **Nunca se crea a ciegas.** Toda escritura es buscar-y-luego-escribir: por dominio
  en empresas, por correo en contactos. El correo es la única llave que evita
  duplicar a una persona.

- **La trampa del índice de búsqueda.** El índice de HubSpot va unos instantes por
  detrás de las escrituras. "Crear -> buscarlo -> no está -> crearlo otra vez" es la
  primera fuente de duplicados silenciosos, y no deja ningún rastro de error. Por eso
  el id SIEMPRE sale de la respuesta del create/update y se devuelve a quien llama
  para que lo guarde (`prospects.hubspot_contact_id`): aquí jamás se vuelve a buscar
  para confirmar lo que se acaba de escribir.

- **El freno.** Un 401/403 suelto puede ser un hipo de HubSpot; repetido significa
  llave muerta o sin permisos. A los `_RECHAZOS_PARA_FRENAR` seguidos se escribe
  `hubspot_activo = false` y se avisa por el webhook de alertas; esa llamada falla
  como `HubSpotError` y a partir de la siguiente ya es `HubSpotPausado`. Reactivarlo
  es siempre una decisión humana (Settings).

- **Nada se escapa.** Todo lo que puede fallar sale como `HubSpotError` o una de sus
  hijas: ni httpx, ni JSON, ni KeyError llegan al bucle del worker. La única excepción
  deliberada es `KillSwitchActive`, que es una parada de sistema y el worker ya sabe
  tratar (ver `cli.SYSTEM_STOPS`).

Al ledger llegan el método, la ruta, el código HTTP, los intentos y el id del objeto.
Nunca la llave, nunca el cuerpo de la respuesta, nunca el texto de una nota ni el
correo de nadie.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .. import db, db_config, guards, ledger, ops_alerts, secretos
from ..config import load_settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.hubapi.com"

# Dos acciones distintas a propósito: solo `hubspot_llamada` cuenta para el tope
# diario. `hubspot_estado` son las lecturas de diagnóstico del CLI, que una persona
# dispara a mano y no deberían gastarle el cupo al worker.
ACCION_LLAMADA = "hubspot_llamada"
ACCION_ESTADO = "hubspot_estado"

ZONA_POR_DEFECTO = "America/New_York"
TOPE_POR_DEFECTO = 25

# Reintentos: 429 y 5xx. Cuatro intentos con espera 1-2-4 s cubren el rate limit de
# HubSpot (ventanas de 10 s) sin que un ciclo del worker se quede colgado minutos.
_MAX_INTENTOS = 4
_ESPERA_INICIAL = 1.0
# Tope a lo que dice `Retry-After`: un valor absurdo (o un proxy que invente uno)
# no puede bloquear el proceso.
_ESPERA_MAX = 60.0

# Un 401 suelto no apaga nada; tres seguidos sí. Se reinicia con cualquier
# respuesta buena, así que solo cuenta una racha real.
_RECHAZOS_PARA_FRENAR = 3

# Tipos de asociación definidos por HubSpot (HUBSPOT_DEFINED).
NOTA_A_CONTACTO = 202
NOTA_A_EMPRESA = 190

_TEXTO_MAX = 65536


class HubSpotError(RuntimeError):
    """HubSpot falló o respondió algo inesperado. No frena nada por sí solo."""


class HubSpotNoConfigurado(HubSpotError):
    """No hay llave: ni en la tabla `secretos` ni en HUBSPOT_TOKEN."""


class HubSpotPausado(HubSpotError):
    """`hubspot_activo` está apagado: nada escribe en el CRM hasta que una persona lo encienda."""


class HubSpotTopeDiario(HubSpotError):
    """Ya se hicieron hoy todas las llamadas que permite `hubspot_por_dia`."""


class HubSpotRateLimit(HubSpotError):
    """HubSpot sigue devolviendo 429 después de todos los reintentos."""


@dataclass(frozen=True)
class Propiedad:
    """Un campo propio nuestro en el CRM de ellos.

    Las etiquetas y descripciones se ven en la pantalla de HubSpot del equipo
    comercial: van en inglés.
    """

    objeto: str
    nombre: str
    etiqueta: str
    descripcion: str
    tipo: str
    field_type: str
    grupo: str


# Los campos que el agente necesita escribir. Hay sitio de sobra (usan 9 de 1.000 en
# empresas y 10 de 1.000 en contactos), así que el research no tiene que ir apretujado
# dentro de la nota. Los grupos `contactinformation` y `companyinformation` existen en
# todos los portales: crear un grupo nuevo sería una modificación más de su esquema.
PROPIEDADES_PROPIAS: tuple[Propiedad, ...] = (
    Propiedad(
        objeto="companies",
        nombre="handoff_fit_score",
        etiqueta="Handoff fit score",
        descripcion="Fit score from 0 to 3 assigned by the Handoff agent.",
        tipo="number",
        field_type="number",
        grupo="companyinformation",
    ),
    Propiedad(
        objeto="companies",
        nombre="handoff_research_url",
        etiqueta="Handoff research",
        descripcion="Link to the research the Handoff agent put together for this company.",
        tipo="string",
        field_type="text",
        grupo="companyinformation",
    ),
    Propiedad(
        objeto="contacts",
        nombre="handoff_fit_score",
        etiqueta="Handoff fit score",
        descripcion="Fit score from 0 to 3 assigned by the Handoff agent.",
        tipo="number",
        field_type="number",
        grupo="contactinformation",
    ),
    Propiedad(
        objeto="contacts",
        nombre="handoff_slack_user_id",
        etiqueta="Handoff community member ID",
        descripcion="Member ID in the community where the Handoff agent found this person.",
        tipo="string",
        field_type="text",
        grupo="contactinformation",
    ),
    Propiedad(
        objeto="contacts",
        nombre="handoff_research_url",
        etiqueta="Handoff research",
        descripcion="Link to the research the Handoff agent put together for this person.",
        tipo="string",
        field_type="text",
        grupo="contactinformation",
    ),
)

# Su enumeración, que ya usan. Añadimos una opción; no se toca ninguna de las suyas.
SOURCE_PROPIEDAD = "source"
SOURCE_OPCION = "Founders Club"

# Solo estas claves se devuelven en el PATCH de las opciones. HubSpot añade campos de
# solo lectura a lo que devuelve el GET, y reenviarlos tal cual hace que rechace la
# petición entera -- con las opciones de ellos dentro.
_CLAVES_OPCION = ("label", "value", "description", "displayOrder", "hidden")

# Racha de 401/403 en este proceso. Módulo y no instancia: el freno protege la cuenta,
# no a un cliente concreto, y el worker crea un cliente por ciclo.
_rechazos_seguidos = 0


def _dormir(segundos: float) -> None:
    """Indirección para que los tests no duerman de verdad."""
    time.sleep(segundos)


def _ahora_utc() -> datetime:
    return datetime.now(UTC)


def _token() -> str:
    """La llave: gana la tabla `secretos`, HUBSPOT_TOKEN queda de respaldo.

    Mismo contrato que `secretos.slack_user_token()`.
    """
    token = secretos.hubspot_token()
    if not token:
        raise HubSpotNoConfigurado(
            "no hay llave de HubSpot: pégala en Settings o define HUBSPOT_TOKEN"
        )
    return token


def _cliente(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=BASE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "accept": "application/json",
        },
        timeout=load_settings().http_timeout_seconds,
    )


def _zona() -> ZoneInfo:
    """La misma zona que el resto de la operación (ver `delivery/seguimiento`)."""
    nombre = db_config.value("zona_horaria", ZONA_POR_DEFECTO)
    try:
        return ZoneInfo(nombre)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("zona_horaria inválida: %r, se usa %s", nombre, ZONA_POR_DEFECTO)
        return ZoneInfo(ZONA_POR_DEFECTO)


def _inicio_del_dia(ahora: datetime) -> datetime:
    return ahora.astimezone(_zona()).replace(hour=0, minute=0, second=0, microsecond=0)


def _llamadas_hoy() -> int:
    row = db.fetch_one(
        "select count(*) as n from agent_actions where action = %s and created_at >= %s",
        (ACCION_LLAMADA, _inicio_del_dia(_ahora_utc())),
    )
    return row["n"]


def _activo() -> bool:
    return db_config.value("hubspot_activo", False) is True


def _frenar(motivo: str) -> None:
    """Apaga HubSpot en config y avisa, solo la primera vez: con el interruptor ya
    apagado nada vuelve a escribir hasta que una persona lo encienda."""
    cambiado = db.execute(
        """
        insert into config (key, value) values ('hubspot_activo', 'false'::jsonb)
        on conflict (key) do update set value = 'false'::jsonb, updated_at = now()
            where config.value is distinct from 'false'::jsonb
        """
    )
    if cambiado:
        # El título que pone ops_alerts para un tipo desconocido es el propio tipo, así
        # que el mensaje empieza por la frase en inglés que se lee en Slack.
        ops_alerts.alert(
            "hubspot_apagado",
            "HubSpot sync is off — the key was rejected. "
            f"{motivo}. Paste a working Service Key in Settings and turn it back on.",
        )


def _espera(response: httpx.Response | None, por_defecto: float) -> float:
    """`Retry-After` si HubSpot lo manda (en segundos), si no el backoff que tocaba."""
    if response is None:
        return min(por_defecto, _ESPERA_MAX)
    bruto = response.headers.get("Retry-After", "")
    try:
        segundos = float(bruto)
    except (TypeError, ValueError):
        return min(por_defecto, _ESPERA_MAX)
    if segundos < 0:
        return min(por_defecto, _ESPERA_MAX)
    return min(segundos, _ESPERA_MAX)


def _limpiar_propiedades(propiedades: dict[str, Any]) -> dict[str, str]:
    """Fuera los vacíos: en un update, mandar "" borraría en HubSpot un dato que
    alguien del equipo metió a mano. (El cliente viejo de n8n sí los mandaba.)"""
    limpias: dict[str, str] = {}
    for clave, valor in (propiedades or {}).items():
        if valor is None or (isinstance(valor, str) and not valor.strip()):
            continue
        limpias[str(clave)] = str(valor).strip() if isinstance(valor, str) else str(valor)
    return limpias


def normalizar_dominio(dominio: str) -> str:
    """`https://WWW.Acme.com/precios` -> `acme.com`.

    HubSpot guarda el dominio pelado; buscar con el esquema delante no encuentra
    nada y acabaría creando una empresa duplicada.
    """
    texto = str(dominio or "").strip().lower()
    for prefijo in ("https://", "http://"):
        texto = texto.removeprefix(prefijo)
    texto = texto.split("/")[0].split("?")[0].removeprefix("www.")
    return texto.strip(". ")


def normalizar_email(email: str) -> str:
    return str(email or "").strip().lower()


class HubSpot:
    """El CRM de Handoff. Un cliente por ciclo del worker; no guarda estado entre llamadas.

    Ningún método toca HubSpot sin pasar antes por `_antes_de_llamar`, salvo
    `estado()`, que es diagnóstico de solo lectura y está documentado aparte.
    """

    def __init__(self, token: str | None = None) -> None:
        # Un token explícito solo para los tests y el CLI; en producción se relee de
        # `secretos` en cada llamada, que es lo que permite cambiar la llave en caliente.
        self._token_fijo = token

    # --- guardarraíles -------------------------------------------------------------

    def _antes_de_llamar(self) -> str:
        """Los frenos, en orden. Lanza si alguno dice que no. Devuelve la llave."""
        token = self._token_fijo or _token()
        if not _activo():
            raise HubSpotPausado(
                "hubspot_activo está apagado en config; encenderlo es decisión humana (Settings)"
            )
        guards.check_kill_switch()
        tope = db_config.value("hubspot_por_dia", TOPE_POR_DEFECTO)
        usadas = _llamadas_hoy()
        if usadas >= tope:
            raise HubSpotTopeDiario(f"{usadas} llamadas a HubSpot hoy, tope {tope}")
        return token

    # --- transporte ----------------------------------------------------------------

    def _intentar(
        self, client: httpx.Client, metodo: str, ruta: str, **kwargs
    ) -> tuple[httpx.Response, int]:
        """Una petición con reintentos ante 429 y 5xx. Devuelve la última respuesta.

        Un fallo de red se reintenta igual: HubSpot corta conexiones de vez en cuando y
        volver a intentarlo es más barato que dar por perdida una sincronización.
        """
        espera = _ESPERA_INICIAL
        ultimo: Exception | None = None
        for intento in range(1, _MAX_INTENTOS + 1):
            try:
                response = client.request(metodo, ruta, **kwargs)
            except httpx.HTTPError as exc:
                ultimo = exc
                if intento == _MAX_INTENTOS:
                    break
                _dormir(_espera(None, espera))
                espera *= 2
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if intento == _MAX_INTENTOS:
                    return response, intento
                _dormir(_espera(response, espera))
                espera *= 2
                continue
            return response, intento
        raise HubSpotError(f"HubSpot no responde en {ruta}: {type(ultimo).__name__}") from ultimo

    def _llamar(
        self,
        metodo: str,
        ruta: str,
        *,
        accion: str = ACCION_LLAMADA,
        guardado: bool = True,
        tolerar_404: bool = False,
        **kwargs,
    ) -> dict | None:
        """Una llamada a HubSpot, con su fila en el ledger pase lo que pase.

        `guardado=False` es solo para `estado()`: lecturas que una persona pide a mano
        y que tienen que funcionar con el interruptor apagado.
        """
        token = self._antes_de_llamar() if guardado else (self._token_fijo or _token())

        registro = {"metodo": metodo, "ruta": ruta}
        try:
            with _cliente(token) as client:
                response, intentos = self._intentar(client, metodo, ruta, **kwargs)
        except HubSpotError as exc:
            ledger.record_action(accion, registro, {"error": type(exc).__name__})
            raise

        cuerpo = _cuerpo(response)
        resultado = {
            "status": response.status_code,
            "intentos": intentos,
            "id": _id_de(cuerpo),
        }
        ledger.record_action(accion, registro, resultado)

        if response.status_code in (401, 403):
            # El freno vive fuera del diagnóstico: `hubspot-estado` lo dispara una
            # persona precisamente para comprobar una llave, y apagar (o avisar) por su
            # comprobación manual sería ruido.
            if guardado:
                self._rechazo(response.status_code, ruta)
            raise HubSpotError(f"HubSpot rechazó la llave (HTTP {response.status_code}) en {ruta}")
        if response.status_code == 429:
            raise HubSpotRateLimit(
                f"HubSpot sigue limitando después de {intentos} intentos en {ruta}"
            )
        if response.status_code == 404 and tolerar_404:
            return None
        if response.status_code >= 400:
            raise HubSpotError(f"HubSpot respondió HTTP {response.status_code} en {ruta}")

        _exito()
        return cuerpo if isinstance(cuerpo, dict) else {}

    def _rechazo(self, status: int, ruta: str) -> None:
        global _rechazos_seguidos
        _rechazos_seguidos += 1
        if _rechazos_seguidos >= _RECHAZOS_PARA_FRENAR:
            _frenar(f"HubSpot answered HTTP {status} on {ruta} {_rechazos_seguidos} times in a row")

    # --- empresas ------------------------------------------------------------------

    def buscar_empresa_por_dominio(self, dominio: str) -> str | None:
        """Id de la empresa con ese dominio, o None. El dominio es la llave: dos
        empresas con el mismo dominio en el CRM son la misma empresa dos veces."""
        dominio = normalizar_dominio(dominio)
        if not dominio:
            raise HubSpotError("hace falta un dominio para buscar una empresa")
        cuerpo = self._llamar(
            "POST",
            "/crm/v3/objects/companies/search",
            json={
                "filterGroups": [
                    {"filters": [{"propertyName": "domain", "operator": "EQ", "value": dominio}]}
                ],
                "properties": ["domain", "name"],
                "limit": 1,
            },
        )
        return _primer_id(cuerpo)

    def crear_o_actualizar_empresa(self, dominio: str, propiedades: dict | None = None) -> str:
        """Id de la empresa, creada o actualizada. Nunca crea a ciegas.

        El id sale de la respuesta y se devuelve: quien llama lo guarda. No se vuelve a
        buscar para confirmar -- el índice de HubSpot todavía no la tendría y acabaríamos
        creándola otra vez.
        """
        dominio = normalizar_dominio(dominio)
        limpias = _limpiar_propiedades({**(propiedades or {}), "domain": dominio})
        existente = self.buscar_empresa_por_dominio(dominio)
        if existente:
            # Sin nada nuevo que escribir no se gasta una llamada (ni cupo del tope).
            if set(limpias) <= {"domain"}:
                return existente
            cuerpo = self._llamar(
                "PATCH",
                f"/crm/v3/objects/companies/{existente}",
                json={"properties": limpias},
            )
            return _id_de(cuerpo) or existente
        cuerpo = self._llamar("POST", "/crm/v3/objects/companies", json={"properties": limpias})
        return _id_obligatorio(cuerpo, "empresa")

    # --- contactos -----------------------------------------------------------------

    def buscar_contacto_por_email(self, email: str) -> str | None:
        """Id del contacto con ese correo, o None.

        El 100% de los perfiles del club trae correo, y es la única llave que evita
        duplicar a una persona (ver migración 0035).
        """
        email = normalizar_email(email)
        if not email:
            raise HubSpotError("hace falta un correo para buscar un contacto")
        cuerpo = self._llamar(
            "POST",
            "/crm/v3/objects/contacts/search",
            json={
                "filterGroups": [
                    {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
                ],
                "properties": ["email"],
                "limit": 1,
            },
        )
        return _primer_id(cuerpo)

    def crear_o_actualizar_contacto(self, email: str, propiedades: dict | None = None) -> str:
        """Id del contacto, creado o actualizado. Mismo trato que la empresa."""
        email = normalizar_email(email)
        limpias = _limpiar_propiedades({**(propiedades or {}), "email": email})
        existente = self.buscar_contacto_por_email(email)
        if existente:
            if set(limpias) <= {"email"}:
                return existente
            cuerpo = self._llamar(
                "PATCH",
                f"/crm/v3/objects/contacts/{existente}",
                json={"properties": limpias},
            )
            return _id_de(cuerpo) or existente
        cuerpo = self._llamar("POST", "/crm/v3/objects/contacts", json={"properties": limpias})
        return _id_obligatorio(cuerpo, "contacto")

    # --- relaciones ----------------------------------------------------------------

    def asociar_contacto_con_empresa(self, contact_id: str, company_id: str) -> None:
        """Asociación por defecto contacto -> empresa (v4).

        PUT sobre `associations/default`: repetirlo no crea una segunda asociación, así
        que se puede llamar en cada sincronización sin comprobar nada antes.
        """
        contact_id = _id_valido(contact_id, "contacto")
        company_id = _id_valido(company_id, "empresa")
        self._llamar(
            "PUT",
            f"/crm/v4/objects/contacts/{contact_id}/associations/default/companies/{company_id}",
        )

    def crear_nota(
        self, texto: str, contact_id: str | None = None, company_id: str | None = None
    ) -> str:
        """Una nota con el research, colgada del contacto, de la empresa o de los dos.

        Una nota suelta no la ve nadie: hace falta al menos un objeto al que asociarla.
        El texto no entra en el ledger -- habla de una persona concreta.
        """
        texto = str(texto or "").strip()
        if not texto:
            raise HubSpotError("una nota vacía no aporta nada al CRM")
        asociaciones = []
        if contact_id:
            asociaciones.append(_asociacion(_id_valido(contact_id, "contacto"), NOTA_A_CONTACTO))
        if company_id:
            asociaciones.append(_asociacion(_id_valido(company_id, "empresa"), NOTA_A_EMPRESA))
        if not asociaciones:
            raise HubSpotError("una nota necesita un contacto o una empresa a la que asociarse")
        cuerpo = self._llamar(
            "POST",
            "/crm/v3/objects/notes",
            json={
                "properties": {
                    "hs_note_body": texto[:_TEXTO_MAX],
                    # Sin marca de tiempo HubSpot rechaza la nota.
                    "hs_timestamp": _ahora_utc().isoformat(),
                },
                "associations": asociaciones,
            },
        )
        return _id_obligatorio(cuerpo, "nota")

    # --- esquema -------------------------------------------------------------------

    def asegurar_propiedades(self) -> dict:
        """Deja el esquema listo, sin romper nada de lo suyo. Se puede llamar siempre.

        Dos cosas distintas:

        1. Nuestros campos (`handoff_*`): se leen uno a uno y solo se crean los que no
           están. A uno que ya existe no se le toca nada -- puede que alguien le haya
           cambiado la etiqueta a mano.
        2. Su enumeración `source`, que ya usan con cinco opciones (Event, Referral,
           Outbound Campaign, AG Connections, Organic) y de la que cuelgan sus informes:
           se lee primero, y solo si "Founders Club" no está se hace un PATCH con SUS
           opciones tal cual, en su orden, y la nuestra al final. Un PATCH con la lista
           incompleta borraría las que faltaran: es el único sitio de este módulo donde
           un error nuestro estropea datos de los que ellos dependen.
        """
        creadas: list[str] = []
        existentes: list[str] = []
        for propiedad in PROPIEDADES_PROPIAS:
            ruta = f"/crm/v3/properties/{propiedad.objeto}/{propiedad.nombre}"
            if self._llamar("GET", ruta, tolerar_404=True) is not None:
                existentes.append(f"{propiedad.objeto}.{propiedad.nombre}")
                continue
            self._llamar(
                "POST",
                f"/crm/v3/properties/{propiedad.objeto}",
                json={
                    "name": propiedad.nombre,
                    "label": propiedad.etiqueta,
                    "description": propiedad.descripcion,
                    "type": propiedad.tipo,
                    "fieldType": propiedad.field_type,
                    "groupName": propiedad.grupo,
                },
            )
            creadas.append(f"{propiedad.objeto}.{propiedad.nombre}")
        return {"creadas": creadas, "existentes": existentes, "source": self._asegurar_source()}

    def _asegurar_source(self) -> str:
        ruta = f"/crm/v3/properties/contacts/{SOURCE_PROPIEDAD}"
        actual = self._llamar("GET", ruta, tolerar_404=True)
        if actual is None:
            # No es nuestra propiedad: si no está, no la inventamos.
            return "no existe en su portal"
        opciones = actual.get("options")
        opciones = (
            [o for o in opciones if isinstance(o, dict)] if isinstance(opciones, list) else []
        )
        if _ya_esta(opciones, SOURCE_OPCION):
            return "ya estaba"
        orden = [o.get("displayOrder") for o in opciones if isinstance(o.get("displayOrder"), int)]
        nueva = {
            "label": SOURCE_OPCION,
            "value": SOURCE_OPCION,
            "description": "Lead found by the Handoff agent in the Founders Club community.",
            # Al final de su lista: no se reordena nada de lo suyo.
            "displayOrder": (max(orden) + 1) if orden else len(opciones),
            "hidden": False,
        }
        self._llamar(
            "PATCH", ruta, json={"options": [_opcion_limpia(o) for o in opciones] + [nueva]}
        )
        return "añadida"

    # --- diagnóstico ---------------------------------------------------------------

    def estado(self) -> dict:
        """Qué puede hacer esta llave, en qué portal y cuánto sitio queda para campos.

        Solo lecturas, y a propósito SIN los guardarraíles de escritura: la gracia de
        `handoff hubspot-estado` es poder comprobar una llave antes de encender nada.
        Tampoco frena ni avisa si la rechazan: ya lo está viendo una persona.
        """
        informe: dict[str, Any] = {
            "activo": _activo(),
            "llamadas_hoy": _llamadas_hoy(),
            "tope_diario": db_config.value("hubspot_por_dia", TOPE_POR_DEFECTO),
            "portal_id": None,
            "capacidades": [],
            "propiedades": None,
        }

        cuenta = self._sonda("GET", "/account-info/v3/details")
        if cuenta[0] and isinstance(cuenta[1], dict):
            informe["portal_id"] = cuenta[1].get("portalId")

        for etiqueta, metodo, ruta, kwargs in (
            ("Read companies", "GET", "/crm/v3/objects/companies", {"params": {"limit": 1}}),
            ("Read contacts", "GET", "/crm/v3/objects/contacts", {"params": {"limit": 1}}),
            ("Read notes", "GET", "/crm/v3/objects/notes", {"params": {"limit": 1}}),
            ("Read company schema", "GET", "/crm/v3/properties/companies", {}),
            ("Read contact schema", "GET", "/crm/v3/properties/contacts", {}),
        ):
            ok, _ = self._sonda(metodo, ruta, **kwargs)
            informe["capacidades"].append((etiqueta, ok))

        ok, limites = self._sonda("GET", "/crm/v3/limits/custom-properties")
        if ok and isinstance(limites, dict):
            informe["propiedades"] = {
                "tope_general": limites.get("overallLimit"),
                "por_objeto": [
                    (str(fila.get("objectType")), fila.get("usage"), fila.get("limit"))
                    for fila in (limites.get("byObjectType") or [])
                    if isinstance(fila, dict)
                ],
            }
        return informe

    def _sonda(self, metodo: str, ruta: str, **kwargs) -> tuple[bool, dict | None]:
        """Una lectura de diagnóstico: nunca lanza, solo dice si salió o no."""
        try:
            return True, self._llamar(metodo, ruta, accion=ACCION_ESTADO, guardado=False, **kwargs)
        except HubSpotNoConfigurado:
            raise
        except HubSpotError as exc:
            logger.info("hubspot: %s %s no se pudo leer (%s)", metodo, ruta, exc)
            return False, None


# --- ayudas sin estado -------------------------------------------------------------


def _exito() -> None:
    global _rechazos_seguidos
    _rechazos_seguidos = 0


def _cuerpo(response: httpx.Response) -> dict | None:
    """El JSON, o None. Que HubSpot devuelva algo que no es JSON no puede reventar
    a quien llama: se trata como respuesta sin cuerpo."""
    try:
        cuerpo = response.json()
    except ValueError:
        return None
    return cuerpo if isinstance(cuerpo, dict) else None


def _id_de(cuerpo: dict | None) -> str | None:
    if not isinstance(cuerpo, dict):
        return None
    valor = cuerpo.get("id")
    return str(valor) if isinstance(valor, (str, int)) and not isinstance(valor, bool) else None


def _id_obligatorio(cuerpo: dict | None, que: str) -> str:
    identificador = _id_de(cuerpo)
    if not identificador:
        raise HubSpotError(f"HubSpot creó {que} pero no devolvió su id")
    return identificador


def _primer_id(cuerpo: dict | None) -> str | None:
    resultados = (cuerpo or {}).get("results")
    if not isinstance(resultados, list) or not resultados:
        return None
    return _id_de(resultados[0] if isinstance(resultados[0], dict) else None)


def _id_valido(valor: object, que: str) -> str:
    """Los ids de HubSpot son numéricos; van dentro de la URL, así que se comprueban."""
    texto = str(valor or "").strip()
    if not texto.isdigit():
        raise HubSpotError(f"id de {que} de HubSpot inválido: {texto!r}")
    return texto


def _asociacion(objeto_id: str, tipo: int) -> dict:
    return {
        "to": {"id": objeto_id},
        "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": tipo}],
    }


def _ya_esta(opciones: list[dict], opcion: str) -> bool:
    """Compara por etiqueta y por valor, sin distinguir mayúsculas: el `value` interno
    de una enumeración no tiene por qué coincidir con lo que se ve en pantalla."""
    buscado = opcion.strip().lower()
    return any(
        str(o.get(clave, "")).strip().lower() == buscado
        for o in opciones
        for clave in ("label", "value")
    )


def _opcion_limpia(opcion: dict) -> dict:
    return {clave: opcion[clave] for clave in _CLAVES_OPCION if clave in opcion}
