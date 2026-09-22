"""research_person: one person in, one stored dossier out.

The worker owns the rules that cost or save money: never research a discarded
person, never pay twice for a fresh dossier, never throw away a valid dossier
that was already paid for. A kill switch or the monthly cap is a system stop,
not a verdict on the person, so those propagate untouched.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .. import db, guards, ledger
from ..dossier import DossierInvalid
from ..tools import prospects
from .followup import run_followup, suggested_queries
from .gather import gather
from .synthesize import synthesize

FRESH_FOR = timedelta(days=180)
SYSTEM_STOPS = (guards.KillSwitchActive, guards.MonthlyBudgetExceeded)
DEGRADED_REASON = "búsqueda degradada"
DEGRADED_KEY = "degradado"
# Como DEGRADED_KEY: solo lo pone el código. Si el modelo devolviera esta
# clave por su cuenta, se descarta -- son datos de `enrichment.enrich_company`,
# nunca algo que el modelo deba inventar o repetir.
PROVEEDOR_KEY = "datos_proveedor"
# Como DEGRADED_KEY y PROVEEDOR_KEY: solo lo pone el código, a partir de
# `Gathered.unconfirmed_domain` (ver research/gather.py). Si el modelo
# devolviera esta clave por su cuenta, se descarta.
EMPRESA_NO_CONFIRMADA_KEY = "empresa_no_confirmada"


@dataclass(frozen=True)
class ResearchOutcome:
    prospect_id: str
    slack_user_id: str
    status: str
    version: int | None
    cost_usd: Decimal
    reason: str | None
    # Último y con valor por defecto: hay quien construye esto por posición.
    errors: tuple[str, ...] = ()


def manual_user_id(full_name: str | None, company: str | None) -> str:
    """Stable id for people researched by hand, before Slack is connected."""
    base = " ".join(part for part in (full_name, company) if part)
    ascii_text = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    # Un nombre no latino no deja nada en ASCII; sin esto, todos compartirían
    # "manual:" y se pisarían el dossier.
    return "manual:" + (slug or hashlib.sha1(base.encode()).hexdigest()[:12])


# Desde qué estados se puede llegar a cada uno. Un research nunca deshace lo
# que hizo una persona: contactado y cliente se quedan, descartado es final, y
# un fallo no rebaja a quien ya estaba investigado.
ALLOWED_FROM = {
    "investigado": ("nuevo", "incompleto", "investigado"),
    "incompleto": ("nuevo", "incompleto"),
}


def _set_state(
    prospect_id: str, state: str, company: str | None = None, domain: str | None = None
) -> str:
    """Pide el estado y devuelve el que de verdad quedó (puede ser otro: I4)."""
    row = db.fetch_one(
        "update prospects set "
        "state = case when state = any(%s) then %s else state end, "
        "company_name = coalesce(%s, company_name), "
        "company_domain = coalesce(%s, company_domain), updated_at = now() "
        "where id = %s returning state",
        (list(ALLOWED_FROM[state]), state, company, domain, prospect_id),
    )
    return row["state"]


def _company_name(dossier: dict, company: str | None) -> str | None:
    """El nombre que da el modelo solo si es texto; si no, el de la entrada.
    Un número rompería el UPDATE con el dossier ya guardado."""
    name = (dossier.get("empresa") or {}).get("nombre")
    return name.strip() if isinstance(name, str) and name.strip() else company


def _store(pid: str, result, gathered, company: str | None) -> tuple[int, str | None, str]:
    """Guarda el dossier y deja el estado. Devuelve la versión, el motivo si la
    búsqueda salió degradada (o None) y el estado en que quedó la persona.

    Un dossier degradado lleva `"degradado": true` en su contenido: la regla de
    frescura no lo protege, así que la próxima ejecución lo repite. Solo lo pone
    el código; si el modelo devolviera esa clave, se descarta.

    `"datos_proveedor"` es igual de código-propia: si `gathered.proveedor` trae
    algo, se guarda tal cual (ya normalizado por `enrichment.enrich_company`);
    si el modelo devolviera esa clave por su cuenta, también se descarta.

    `"empresa_no_confirmada"` es igual: si `gathered.unconfirmed_domain` trae
    algo, se guarda `{"dominio_adivinado": ...}` tal cual; si el modelo
    devolviera esa clave por su cuenta, también se descarta.
    """
    content = {
        k: v
        for k, v in result.dossier.items()
        if k not in (DEGRADED_KEY, PROVEEDOR_KEY, EMPRESA_NO_CONFIRMADA_KEY)
    }
    degraded = None
    if gathered.search_degraded:
        content[DEGRADED_KEY] = True
        degraded = (
            f"{DEGRADED_REASON}: ninguna de las {gathered.searches_attempted} "
            "búsquedas devolvió resultados"
        )
    if gathered.proveedor:
        content[PROVEEDOR_KEY] = gathered.proveedor
    if gathered.unconfirmed_domain:
        content[EMPRESA_NO_CONFIRMADA_KEY] = {"dominio_adivinado": gathered.unconfirmed_domain}
    version = prospects.save_dossier(pid, content, gathered.source_records())
    if gathered.errors:
        ledger.record_action("research_errores", {"errores": gathered.errors}, prospect_id=pid)
    state = _set_state(
        pid,
        "incompleto" if degraded else "investigado",
        company=_company_name(result.dossier, company),
        domain=gathered.domain,
    )
    return version, degraded, state


def research_person(
    full_name: str | None = None,
    company: str | None = None,
    domain: str | None = None,
    slack_user_id: str | None = None,
    force: bool = False,
    # Lo que el panel añadió a mano (`panel_ayudar_research`, migración 0008):
    # enlaces sueltos que gather() lee como cualquier otra página (salvo
    # LinkedIn, que nunca se descarga), y notas libres que synthesize() pasa
    # al modelo como orientación, nunca como fuente.
    extra_links: Sequence[str] = (),
    notes: str | None = None,
) -> ResearchOutcome:
    if not (full_name or company):
        raise ValueError("hace falta al menos un nombre o una empresa")

    user_id = slack_user_id or manual_user_id(full_name, company)
    person = prospects.upsert_prospect(user_id, full_name=full_name)
    pid = str(person["id"])

    if person["state"] == "descartado":
        return ResearchOutcome(pid, user_id, "omitido", None, Decimal(0), "persona descartada")

    # Incompleto significa que falta algo, y un dossier degradado salió de una
    # búsqueda que no funcionó: la frescura no protege ninguno de los dos.
    if not force and person["state"] != "incompleto":
        latest = db.fetch_one(
            "select version, created_at, "
            "coalesce((content->>%s)::boolean, false) as degraded "
            "from dossiers where prospect_id = %s order by version desc limit 1",
            (DEGRADED_KEY, pid),
        )
        if (
            latest
            and not latest["degraded"]
            and datetime.now(UTC) - latest["created_at"] < FRESH_FOR
        ):
            return ResearchOutcome(
                pid, user_id, "omitido", latest["version"], Decimal(0), "dossier vigente"
            )

    budget = guards.RunBudget(limit_usd=guards.run_budget_limit(), prospect_id=pid)
    gathered = None
    try:
        with budget:
            gathered = gather(pid, full_name, company, domain, extra_links=extra_links, notes=notes)
            result = synthesize(pid, full_name, company, gathered, budget=budget)
            # Desde aquí hay un dossier válido y pagado: todo camino lo guarda.
            first_result, first_gathered = result, gathered

            try:
                # Leer lo que propone el modelo ya es segunda pasada: si falla,
                # vale el primer dossier.
                queries = suggested_queries(result.dossier)
                if queries:
                    enriched = run_followup(pid, gathered, queries)
                    result = synthesize(pid, full_name, company, enriched, budget=budget)
                    gathered = enriched
            except SYSTEM_STOPS as exc:
                # La parada del sistema se propaga, pero lo pagado se conserva, y
                # la excepción lleva la versión para que el lote pueda contarlo.
                saved_version, _, _ = _store(pid, first_result, first_gathered, company)
                ledger.record_action("seguimiento_cortado", {"motivo": str(exc)}, prospect_id=pid)
                exc.saved_version = saved_version
                raise
            except Exception as exc:  # noqa: BLE001 - la primera pasada ya vale
                result, gathered = first_result, first_gathered
                ledger.record_action(
                    "seguimiento_descartado",
                    {"motivo": f"{type(exc).__name__}: {exc}"},
                    prospect_id=pid,
                )

            version, degraded, state = _store(pid, result, gathered, company)
    except (DossierInvalid, guards.RunBudgetExceeded) as exc:
        _set_state(pid, "incompleto")
        errors = tuple(gathered.errors) if gathered else ()
        if errors:
            ledger.record_action("research_errores", {"errores": list(errors)}, prospect_id=pid)
        ledger.record_action("research_incompleto", {"motivo": str(exc)}, prospect_id=pid)
        return ResearchOutcome(pid, user_id, "incompleto", None, budget.spent, str(exc), errors)

    errors = tuple(gathered.errors)
    if degraded and state == "incompleto":
        return ResearchOutcome(pid, user_id, "incompleto", version, budget.spent, degraded, errors)
    if degraded:
        # I4: quien ya estaba investigado, contactado o cliente no baja a
        # incompleto; el estado se dice tal como quedó.
        reason = f"{degraded}; se conserva el estado {state}"
        return ResearchOutcome(pid, user_id, "investigado", version, budget.spent, reason, errors)
    return ResearchOutcome(pid, user_id, "investigado", version, budget.spent, None, errors)
