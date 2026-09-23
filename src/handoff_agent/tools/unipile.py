"""Cliente de Unipile, de SOLO LECTURA, sobre la cuenta de Sales Navigator de Anthony.

Es la única pieza del proyecto que toca LinkedIn con una cuenta, y la cuenta es
de una persona: si LinkedIn la restringe, Anthony pierde su LinkedIn. Por eso
(spec del radar, §3) todo lo que hay aquí es para resolver el id de una empresa
y buscar personas, nunca para invitar, escribir ni mandar InMail — no existe
ningún método para eso, y un test lo comprueba.

Antes de cada llamada a LinkedIn, en este orden:

1. Kill switch (el de todo el sistema).
2. `unipile_pausado` en config: si está activo, nada toca LinkedIn hasta que
   una persona lo desactive (Settings del panel).
3. Horario humano: lunes a viernes, de `unipile_hora_inicio` a
   `unipile_hora_fin` en `unipile_zona`.
4. Tope diario: las acciones `unipile_busqueda` de hoy (en esa zona) en
   `agent_actions`. El ledger es el contador; por eso cada llamada se registra
   aunque falle.
5. Estado de la cuenta en Unipile: si no está OK, se frena.
6. Una pausa aleatoria de 4 a 15 s desde la llamada anterior.

El freno: si Unipile responde 401/403, o que la cuenta está desconectada o en
checkpoint, o el estado de la cuenta no es OK, se escribe
`unipile_pausado = true`, se avisa por el webhook de alertas y se lanza
`UnipilePausado`. Reactivarlo es siempre una decisión humana.

La clave va en la cabecera X-API-KEY y nunca se registra; del cuerpo de las
respuestas solo llega al ledger el código HTTP y cuántos resultados hubo.
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .. import db, db_config, guards, ledger, ops_alerts
from ..config import Settings, load_settings

logger = logging.getLogger(__name__)

ACCION_BUSQUEDA = "unipile_busqueda"
ACCION_ESTADO = "unipile_estado"
# Visitas a perfiles (fase 2 del spec): aún no hay ninguna llamada que lo use,
# pero el tope y el resumen de uso ya lo cuentan.
ACCION_PERFIL = "unipile_perfil"

PAUSA_MIN_SEGUNDOS = 4
PAUSA_MAX_SEGUNDOS = 15
ZONA_POR_DEFECTO = "America/New_York"
_MAX_RESULTADOS = 25
_STR_CHARS = 300
_PERFIL_PREFIX = "https://www.linkedin.com/"
_ID_EMPRESA_RE = re.compile(r"^[0-9]{1,20}$")
# Palabras con las que Unipile describe una cuenta que LinkedIn ha cortado
# (errors/disconnected_account, checkpoint, credenciales caducadas).
_SENALES_DE_CUENTA_CAIDA = ("disconnected", "checkpoint", "credentials")


class UnipileError(RuntimeError):
    """Unipile falló o respondió algo inesperado. No frena nada por sí solo."""


class UnipileNoConfigurado(UnipileError):
    """Faltan UNIPILE_API_KEY, UNIPILE_DSN o UNIPILE_ACCOUNT_ID."""


class UnipilePausado(UnipileError):
    """unipile_pausado está activo: nada toca LinkedIn hasta que una persona lo reactive."""


class UnipileFueraDeHorario(UnipileError):
    """Fuera de la franja humana (lunes a viernes, hora_inicio <= h < hora_fin)."""


class UnipileTopeDiario(UnipileError):
    """Ya se hicieron hoy todas las búsquedas que permite unipile_busquedas_por_dia."""


@dataclass(frozen=True)
class Posicion:
    empresa: str | None
    company_id: str | None
    cargo: str | None


@dataclass(frozen=True)
class PersonaLinkedIn:
    linkedin_id: str
    full_name: str | None
    first_name: str | None
    last_name: str | None
    public_identifier: str | None
    profile_url: str | None
    headline: str | None
    location: str | None
    posiciones: tuple[Posicion, ...] = ()


# Indirecciones para que los tests no duerman ni dependan del reloj real.
def _dormir(segundos: float) -> None:
    time.sleep(segundos)


def _azar(minimo: float, maximo: float) -> float:
    return random.uniform(minimo, maximo)


def _ahora(zona: ZoneInfo) -> datetime:
    return datetime.now(zona)


# Monotónico de la última llamada a LinkedIn en este proceso, para la pausa.
_ultima_llamada: float | None = None


def _credenciales() -> Settings:
    settings = load_settings()
    if not (settings.unipile_api_key and settings.unipile_dsn and settings.unipile_account_id):
        raise UnipileNoConfigurado(
            "faltan UNIPILE_API_KEY, UNIPILE_DSN o UNIPILE_ACCOUNT_ID en el entorno"
        )
    return settings


def _cliente(settings: Settings) -> httpx.Client:
    return httpx.Client(
        base_url=f"https://{settings.unipile_dsn}/api/v1",
        headers={"X-API-KEY": settings.unipile_api_key, "accept": "application/json"},
        timeout=settings.http_timeout_seconds,
    )


def _zona() -> ZoneInfo:
    nombre = db_config.value("unipile_zona", ZONA_POR_DEFECTO)
    try:
        return ZoneInfo(nombre)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("unipile_zona inválida: %r, se usa %s", nombre, ZONA_POR_DEFECTO)
        return ZoneInfo(ZONA_POR_DEFECTO)


def _franja() -> tuple[int, int]:
    return db_config.value("unipile_hora_inicio", 8), db_config.value("unipile_hora_fin", 19)


def _en_horario(ahora: datetime, inicio: int, fin: int) -> bool:
    return ahora.weekday() < 5 and inicio <= ahora.hour < fin


def _usadas_hoy(accion: str, ahora: datetime) -> int:
    inicio_dia = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
    row = db.fetch_one(
        "select count(*) as n from agent_actions where action = %s and created_at >= %s",
        (accion, inicio_dia),
    )
    return row["n"]


def _pausado() -> bool:
    return db_config.value("unipile_pausado", False)


def _frenar(motivo: str) -> None:
    """Pausa Unipile en config y avisa, solo la primera vez: con la pausa ya
    puesta, nada vuelve a llamar hasta que una persona la quite."""
    cambiado = db.execute(
        """
        insert into config (key, value) values ('unipile_pausado', 'true'::jsonb)
        on conflict (key) do update set value = 'true'::jsonb, updated_at = now()
            where config.value is distinct from 'true'::jsonb
        """
    )
    if cambiado:
        ops_alerts.alert(
            "unipile_pausado",
            f"{motivo}. Nada toca LinkedIn hasta reactivar unipile_pausado (Settings del panel).",
        )


def _cuenta_caida(response: httpx.Response) -> bool:
    if response.status_code in (401, 403):
        return True
    if response.status_code < 400:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    texto = " ".join(str(body.get(k, "")) for k in ("type", "title", "detail")).lower()
    return any(palabra in texto for palabra in _SENALES_DE_CUENTA_CAIDA)


def estado_cuenta() -> bool:
    """True si la cuenta de LinkedIn conectada en Unipile está sana.

    No consulta LinkedIn (es el estado que guarda Unipile), así que no pasa por
    los topes ni por el horario. Si la cuenta no está OK, frena Unipile y
    devuelve False. Un fallo de red o un 5xx lanza UnipileError sin frenar:
    que Unipile no conteste no dice nada de la cuenta de Anthony.
    """
    settings = _credenciales()
    try:
        with _cliente(settings) as client:
            response = client.get(f"accounts/{settings.unipile_account_id}")
    except httpx.HTTPError as exc:
        ledger.record_action(ACCION_ESTADO, {}, {"error": type(exc).__name__})
        raise UnipileError(f"Unipile no responde: {type(exc).__name__}") from exc

    if _cuenta_caida(response):
        ledger.record_action(ACCION_ESTADO, {}, {"status": response.status_code, "sana": False})
        _frenar(f"Unipile rechazó la consulta de la cuenta (HTTP {response.status_code})")
        return False
    if response.status_code >= 400:
        ledger.record_action(ACCION_ESTADO, {}, {"status": response.status_code})
        raise UnipileError(f"Unipile respondió HTTP {response.status_code} al consultar la cuenta")

    try:
        body = response.json()
    except ValueError as exc:
        ledger.record_action(ACCION_ESTADO, {}, {"status": response.status_code, "error": "json"})
        raise UnipileError("Unipile devolvió una cuenta que no es JSON") from exc

    fuentes = body.get("sources") if isinstance(body, dict) else None
    estados = [
        str(f.get("status")) if isinstance(f, dict) else "?"
        for f in (fuentes if isinstance(fuentes, list) else [])
    ]
    sana = bool(estados) and all(estado == "OK" for estado in estados)
    ledger.record_action(
        ACCION_ESTADO, {}, {"status": response.status_code, "sana": sana, "estados": estados}
    )
    if not sana:
        _frenar(f"la cuenta de LinkedIn en Unipile no está OK (estados: {estados or 'ninguno'})")
    return sana


def _antes_de_linkedin() -> Settings:
    """Los guardarraíles, en orden. Lanza si alguno dice que no."""
    settings = _credenciales()
    guards.check_kill_switch()
    if _pausado():
        raise UnipilePausado(
            "unipile_pausado está activo en config; reactivarlo es decisión humana (Settings)"
        )
    zona = _zona()
    ahora = _ahora(zona)
    inicio, fin = _franja()
    if not _en_horario(ahora, inicio, fin):
        raise UnipileFueraDeHorario(
            f"fuera de horario: {ahora:%a %H:%M} en {zona.key}, franja lun-vie {inicio}-{fin}"
        )
    tope = db_config.value("unipile_busquedas_por_dia", 25)
    usadas = _usadas_hoy(ACCION_BUSQUEDA, ahora)
    if usadas >= tope:
        raise UnipileTopeDiario(f"{usadas} búsquedas hoy, tope {tope}")
    if not estado_cuenta():
        raise UnipilePausado("la cuenta de LinkedIn en Unipile no está OK; Unipile queda pausado")
    _pausar()
    return settings


def _pausar() -> None:
    """Una ráfaga de búsquedas es justo lo que LinkedIn marca como bot."""
    if _ultima_llamada is None:
        return
    objetivo = _azar(PAUSA_MIN_SEGUNDOS, PAUSA_MAX_SEGUNDOS)
    espera = objetivo - (time.monotonic() - _ultima_llamada)
    if espera > 0:
        _dormir(espera)


def _llamar_linkedin(metodo: str, ruta: str, registro: dict, **kwargs) -> dict:
    """Una llamada que toca LinkedIn, ya pasados los guardarraíles. Siempre
    deja su fila en el ledger: es lo que cuenta para el tope diario."""
    global _ultima_llamada
    settings = _antes_de_linkedin()
    try:
        with _cliente(settings) as client:
            response = client.request(metodo, ruta, **kwargs)
    except httpx.HTTPError as exc:
        ledger.record_action(ACCION_BUSQUEDA, registro, {"error": type(exc).__name__})
        raise UnipileError(f"Unipile no responde: {type(exc).__name__}") from exc
    finally:
        _ultima_llamada = time.monotonic()

    if _cuenta_caida(response):
        ledger.record_action(ACCION_BUSQUEDA, registro, {"status": response.status_code})
        _frenar(f"LinkedIn/Unipile respondió HTTP {response.status_code} en {ruta}")
        raise UnipilePausado(f"Unipile respondió HTTP {response.status_code}; queda pausado")
    if response.status_code >= 400:
        ledger.record_action(ACCION_BUSQUEDA, registro, {"status": response.status_code})
        raise UnipileError(f"Unipile respondió HTTP {response.status_code} en {ruta}")

    try:
        body = response.json()
    except ValueError:
        body = None
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        ledger.record_action(
            ACCION_BUSQUEDA, registro, {"status": response.status_code, "error": "sin items"}
        )
        raise UnipileError(f"Unipile devolvió una respuesta sin items en {ruta}")
    ledger.record_action(
        ACCION_BUSQUEDA, registro, {"status": response.status_code, "items": len(items)}
    )
    return body


def _texto(value: object) -> str | None:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        texto = str(value).strip()
        return texto[:_STR_CHARS] or None
    return None


def resolver_empresa(nombre: str) -> list[tuple[str, str]]:
    """Empresas de LinkedIn que encajan con `nombre`: [(id, título), ...].

    Cuenta como una búsqueda para el tope diario. Elegir cuál es la buena es
    cosa de quien llama (ver accounts/radar.py).
    """
    nombre = nombre.strip()
    settings = _credenciales()
    body = _llamar_linkedin(
        "GET",
        "linkedin/search/parameters",
        {"ruta": "linkedin/search/parameters", "tipo": "COMPANY", "keywords": nombre},
        params={
            "account_id": settings.unipile_account_id,
            "type": "COMPANY",
            "keywords": nombre,
            "limit": 5,
        },
    )
    resultados = []
    for item in body["items"]:
        if not isinstance(item, dict):
            continue
        empresa_id = _texto(item.get("id"))
        titulo = _texto(item.get("title"))
        if empresa_id and titulo and _ID_EMPRESA_RE.match(empresa_id):
            resultados.append((empresa_id, titulo))
    return resultados


def _persona(item: dict) -> PersonaLinkedIn | None:
    linkedin_id = _texto(item.get("id"))
    if not linkedin_id:
        return None
    url = _texto(item.get("public_profile_url"))
    posiciones = []
    raw = item.get("current_positions")
    for posicion in raw if isinstance(raw, list) else []:
        if isinstance(posicion, dict):
            posiciones.append(
                Posicion(
                    empresa=_texto(posicion.get("company")),
                    company_id=_texto(posicion.get("company_id")),
                    cargo=_texto(posicion.get("role")),
                )
            )
    return PersonaLinkedIn(
        linkedin_id=linkedin_id,
        full_name=_texto(item.get("name")),
        first_name=_texto(item.get("first_name")),
        last_name=_texto(item.get("last_name")),
        public_identifier=_texto(item.get("public_identifier")),
        # La URL acaba en el panel como enlace: solo si es de LinkedIn.
        profile_url=url if url and url.startswith(_PERFIL_PREFIX) else None,
        headline=_texto(item.get("headline")),
        location=_texto(item.get("location")),
        posiciones=tuple(posiciones),
    )


def buscar_personas(company_id: str, keywords: str, limit: int = 5) -> list[PersonaLinkedIn]:
    """Búsqueda de Sales Navigator de personas de una empresa por cargo.

    Cuenta como una búsqueda para el tope diario, devuelva lo que devuelva.
    """
    company_id = str(company_id).strip()
    if not _ID_EMPRESA_RE.match(company_id):
        raise ValueError(f"id de empresa de LinkedIn inválido: {company_id!r}")
    limit = max(1, min(int(limit), _MAX_RESULTADOS))
    settings = _credenciales()
    body = _llamar_linkedin(
        "POST",
        "linkedin/search",
        {"ruta": "linkedin/search", "company_id": company_id, "keywords": keywords},
        params={"account_id": settings.unipile_account_id, "limit": limit},
        json={
            "api": "sales_navigator",
            "category": "people",
            "keywords": keywords,
            "company": {"include": [company_id]},
        },
    )
    personas = [_persona(item) for item in body["items"] if isinstance(item, dict)]
    return [p for p in personas if p is not None][:limit]


def resumen_uso() -> dict:
    """Uso de hoy frente a los topes, y si ahora mismo se podría llamar.

    Solo lee config y el ledger: no llama a Unipile.
    """
    zona = _zona()
    ahora = _ahora(zona)
    inicio, fin = _franja()
    return {
        "busquedas_hoy": _usadas_hoy(ACCION_BUSQUEDA, ahora),
        "tope_busquedas": db_config.value("unipile_busquedas_por_dia", 25),
        "perfiles_hoy": _usadas_hoy(ACCION_PERFIL, ahora),
        "tope_perfiles": db_config.value("unipile_perfiles_por_dia", 40),
        "en_horario": _en_horario(ahora, inicio, fin),
        "franja": f"lun-vie {inicio}:00-{fin}:00",
        "zona": zona.key,
        "ahora": ahora.isoformat(timespec="minutes"),
        "pausado": _pausado(),
    }
