"""Enriquecimiento de empresa vía el webhook de Handoff, como dato de proveedor.

El webhook recibe la URL del sitio de la empresa y devuelve lo que un tercero
sabe de ella (LinkedIn, un CRM, cifras de plantilla): sin fuente citable, a
veces contradictorio entre sí (tres cifras de empleados distintas, LinkedIn en
Tokio y el CRM en South San Francisco para la misma empresa). Por eso nunca se
mezcla con la evidencia investigada: se normaliza a un puñado de campos seguros
y se guarda aparte, marcado como sin verificar.

El webhook es privado (su URL nunca se registra ni se comparte) y puede tardar
hasta un minuto largo, o llevar días caído (visto en producción: HTTP 500
"Error in workflow"). Por eso esta función nunca deja que un fallo suyo tumbe
el research: cualquier error se traga, se anota en el ledger sin la URL ni el
cuerpo de la respuesta, y el research sigue sin datos de proveedor.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime

import httpx

from .. import ledger
from ..config import load_settings

logger = logging.getLogger(__name__)

_AREA_KEY_RE = re.compile(r"^[a-z_]{1,40}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_STR_CHARS = 200
_MAX_AREAS = 30
_MAX_MONTHS = 25
_MAX_GROWTH = 5
_LINKEDIN_COMPANY_PREFIX = "https://www.linkedin.com/company/"
_MIN_FOUNDING_YEAR = 1800


def _safe_int(value: object) -> int | None:
    """Solo cuenta como número un `int` de verdad: un `bool` es subclase de
    `int` en Python y un string con pinta de número no es un número."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _safe_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 4)
    return None


def _safe_str(value: object, limit: int = _STR_CHARS) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped[:limit]
    return None


def _location(node: dict, city_key: str, state_key: str, country_key: str) -> dict | None:
    loc = {}
    ciudad = _safe_str(node.get(city_key))
    region = _safe_str(node.get(state_key))
    pais = _safe_str(node.get(country_key))
    if ciudad:
        loc["ciudad"] = ciudad
    if region:
        loc["region"] = region
    if pais:
        loc["pais"] = pais
    return loc or None


def _valid_month(value: object) -> str | None:
    """Acepta solo fechas `YYYY-MM-DD` de verdad; devuelve el mes `YYYY-MM`."""
    if not isinstance(value, str) or not _DATE_RE.match(value):
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        return None
    return value[:7]


def _empleados_por_area(company: dict) -> dict[str, int] | None:
    raw = company.get("departmentalHeadCount")
    if not isinstance(raw, dict):
        return None
    areas: dict[str, int] = {}
    for key, value in raw.items():
        if len(areas) >= _MAX_AREAS:
            break
        if not isinstance(key, str) or not _AREA_KEY_RE.match(key):
            continue
        count = _safe_int(value)
        if count is None:
            continue
        areas[key] = count
    return areas or None


def _evolucion_mensual(linkedin: dict) -> list[dict] | None:
    raw = linkedin.get("monthlyEmployeeCounts")
    if not isinstance(raw, list):
        return None
    meses = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        mes = _valid_month(item.get("date"))
        empleados = _safe_int(item.get("count"))
        if mes is None or empleados is None:
            continue
        meses.append({"mes": mes, "empleados": empleados})
    if not meses:
        return None
    return meses[-_MAX_MONTHS:]


def _crecimiento(crm: dict) -> list[dict] | None:
    raw = crm.get("companyGrowthMetrics")
    if not isinstance(raw, list):
        return None
    crecimiento = []
    for item in raw:
        if len(crecimiento) >= _MAX_GROWTH:
            break
        if not isinstance(item, dict):
            continue
        meses = _safe_int(item.get("monthRange"))
        cambio = _safe_int(item.get("netChange"))
        # growthPercentage ya viene en puntos porcentuales: 0.1439 es 0,14 %.
        porcentaje = _safe_float(item.get("growthPercentage"))
        if meses is None or cambio is None or porcentaje is None:
            continue
        crecimiento.append({"meses": meses, "cambio_neto": cambio, "porcentaje": porcentaje})
    return crecimiento or None


def normalise(raw: dict) -> dict | None:
    """De la respuesta cruda del webhook, solo lo seguro y lo útil.

    Descarta explícitamente `googleNews`/`googleJobs` (ya se investigan aparte),
    `prompt` (interno del proveedor), `aiSummary`, teléfono, dirección postal y
    redes sociales. Cada campo es opcional y se descarta en silencio si no
    tiene el tipo esperado. Si no sobrevive nada útil, devuelve `None`.
    """
    if not isinstance(raw, dict):
        return None
    company = raw.get("Company") if isinstance(raw.get("Company"), dict) else {}
    linkedin = raw.get("linkedIn") if isinstance(raw.get("linkedIn"), dict) else {}
    crm = raw.get("crm") if isinstance(raw.get("crm"), dict) else {}

    result: dict = {}

    empleados_linkedin = _safe_int(linkedin.get("employeeCount"))
    if empleados_linkedin is not None:
        result["empleados_linkedin"] = empleados_linkedin

    rango = {}
    rmin = _safe_int(linkedin.get("employeeRangeMin"))
    rmax = _safe_int(linkedin.get("employeeRangeMax"))
    if rmin is not None:
        rango["min"] = rmin
    if rmax is not None:
        rango["max"] = rmax
    if rango:
        result["rango_empleados"] = rango

    empleados_crm = _safe_int(crm.get("companyEmployeeCount"))
    if empleados_crm is not None:
        result["empleados_crm"] = empleados_crm

    areas = _empleados_por_area(company)
    if areas:
        result["empleados_por_area"] = areas

    evolucion = _evolucion_mensual(linkedin)
    if evolucion:
        result["evolucion_mensual"] = evolucion

    crecimiento = _crecimiento(crm)
    if crecimiento:
        result["crecimiento"] = crecimiento

    ingresos = _safe_int(crm.get("companyAnnualRevenue"))
    if ingresos is not None:
        result["ingresos_anuales_usd"] = ingresos

    anio = _safe_int(crm.get("companyYearFounded"))
    if anio is not None and _MIN_FOUNDING_YEAR <= anio <= datetime.now(UTC).year:
        result["anio_fundacion"] = anio

    sede = _location(crm, "companyCity", "companyState", "companyCountry")
    if sede:
        result["sede"] = sede

    ubicacion_linkedin = _location(linkedin, "locationCity", "locationState", "locationCountry")
    if ubicacion_linkedin:
        result["ubicacion_linkedin"] = ubicacion_linkedin

    linkedin_url = _safe_str(linkedin.get("linkedinUrl"))
    if linkedin_url and linkedin_url.startswith(_LINKEDIN_COMPANY_PREFIX):
        result["linkedin_url"] = linkedin_url

    seguidores = _safe_int(linkedin.get("followerCount"))
    if seguidores is not None:
        result["seguidores_linkedin"] = seguidores

    antiguedad = _safe_str(linkedin.get("avgTenure"))
    if antiguedad:
        result["antiguedad_media"] = antiguedad

    if not result:
        return None

    result["fuente"] = "proveedor externo (sin verificar)"
    result["consultado"] = datetime.now(UTC).date().isoformat()
    return result


def enrich_company(domain: str, prospect_id: str | None = None) -> dict | None:
    """Llama al webhook de enriquecimiento y devuelve datos normalizados de
    proveedor, o `None` si no hay webhook configurado o algo falla.

    Nunca lanza: un timeout, un 500, un JSON roto o una forma inesperada se
    tragan, se registran (sin la URL del webhook ni el cuerpo de la
    respuesta) y el research sigue sin estos datos.
    """
    settings = load_settings()
    if not settings.enrichment_webhook_url:
        logger.debug("ENRICHMENT_WEBHOOK_URL no está configurado; se omite el enriquecimiento")
        return None

    try:
        response = httpx.post(
            settings.enrichment_webhook_url,
            json={"companyUrl": f"https://{domain}"},
            timeout=settings.enrichment_timeout_seconds,
        )
        response.raise_for_status()
        raw = response.json()
    except Exception as exc:  # noqa: BLE001 - cualquier fallo del webhook se traga
        logger.warning("enriquecimiento de empresa: %s", type(exc).__name__)
        payload = {"motivo": type(exc).__name__}
        if isinstance(exc, httpx.HTTPStatusError):
            payload["status"] = exc.response.status_code
        ledger.record_action("enriquecimiento_fallido", payload, prospect_id=prospect_id)
        return None

    # El coste real del flujo (GPT-4.1 más un proveedor de datos de empresa) lo
    # paga el webhook, no nosotros; hasta conocerlo, PRICE_ENRICHMENT_PER_CALL
    # vale 0.0. La llamada sale del ledger igual: toda llamada externa pasa por
    # aquí aunque el precio de hoy sea un placeholder.
    ledger.record_cost_event(
        "enriquecimiento_empresa",
        settings.price_enrichment_per_call,
        f"Enriquecimiento de {domain}",
        prospect_id,
    )
    return normalise(raw)
