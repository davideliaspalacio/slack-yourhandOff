"""research_person: one person in, one stored dossier out.

The worker owns the rules that cost or save money: never research a discarded
person, never pay twice for a fresh dossier, never throw away a valid dossier
that was already paid for. A kill switch or the monthly cap is a system stop,
not a verdict on the person, so those propagate untouched.
"""

from __future__ import annotations

import re
import unicodedata
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
    return "manual:" + re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")


def _set_state(
    prospect_id: str, state: str, company: str | None = None, domain: str | None = None
) -> None:
    db.execute(
        "update prospects set state = %s, "
        "company_name = coalesce(%s, company_name), "
        "company_domain = coalesce(%s, company_domain), updated_at = now() "
        "where id = %s",
        (state, company, domain, prospect_id),
    )


def _store(pid: str, result, gathered, company: str | None) -> tuple[int, str | None]:
    """Guarda el dossier y deja el estado. Devuelve la versión y, si la
    búsqueda salió degradada, el motivo (la persona queda en incompleto)."""
    version = prospects.save_dossier(pid, result.dossier, sorted(gathered.sources))
    if gathered.errors:
        ledger.record_action("research_errores", {"errores": gathered.errors}, prospect_id=pid)
    degraded = None
    if gathered.search_degraded:
        degraded = (
            f"{DEGRADED_REASON}: ninguna de las {gathered.searches_attempted} "
            "búsquedas devolvió resultados"
        )
    empresa = result.dossier.get("empresa") or {}
    _set_state(
        pid,
        "incompleto" if degraded else "investigado",
        company=empresa.get("nombre") or company,
        domain=gathered.domain,
    )
    return version, degraded


def research_person(
    full_name: str | None = None,
    company: str | None = None,
    domain: str | None = None,
    slack_user_id: str | None = None,
    force: bool = False,
) -> ResearchOutcome:
    if not (full_name or company):
        raise ValueError("hace falta al menos un nombre o una empresa")

    user_id = slack_user_id or manual_user_id(full_name, company)
    person = prospects.upsert_prospect(user_id, full_name=full_name)
    pid = str(person["id"])

    if person["state"] == "descartado":
        return ResearchOutcome(pid, user_id, "omitido", None, Decimal(0), "persona descartada")

    # Incompleto significa que falta algo: la frescura no protege ese dossier.
    if not force and person["state"] != "incompleto":
        latest = db.fetch_one(
            "select version, created_at from dossiers where prospect_id = %s "
            "order by version desc limit 1",
            (pid,),
        )
        if latest and datetime.now(UTC) - latest["created_at"] < FRESH_FOR:
            return ResearchOutcome(
                pid, user_id, "omitido", latest["version"], Decimal(0), "dossier vigente"
            )

    budget = guards.RunBudget(limit_usd=guards.run_budget_limit(), prospect_id=pid)
    gathered = None
    try:
        with budget:
            gathered = gather(pid, full_name, company, domain)
            result = synthesize(pid, full_name, company, gathered, budget=budget)
            # Desde aquí hay un dossier válido y pagado: todo camino lo guarda.
            first_result, first_gathered = result, gathered

            queries = suggested_queries(result.dossier)
            if queries:
                try:
                    enriched = run_followup(pid, gathered, queries)
                    result = synthesize(pid, full_name, company, enriched, budget=budget)
                    gathered = enriched
                except SYSTEM_STOPS as exc:
                    # La parada del sistema se propaga, pero lo pagado se conserva.
                    _store(pid, first_result, first_gathered, company)
                    ledger.record_action(
                        "seguimiento_cortado", {"motivo": str(exc)}, prospect_id=pid
                    )
                    raise
                except Exception as exc:  # noqa: BLE001 - la primera pasada ya vale
                    result, gathered = first_result, first_gathered
                    ledger.record_action(
                        "seguimiento_descartado",
                        {"motivo": f"{type(exc).__name__}: {exc}"},
                        prospect_id=pid,
                    )

            version, degraded = _store(pid, result, gathered, company)
    except (DossierInvalid, guards.RunBudgetExceeded) as exc:
        _set_state(pid, "incompleto")
        errors = tuple(gathered.errors) if gathered else ()
        if errors:
            ledger.record_action("research_errores", {"errores": list(errors)}, prospect_id=pid)
        ledger.record_action("research_incompleto", {"motivo": str(exc)}, prospect_id=pid)
        return ResearchOutcome(pid, user_id, "incompleto", None, budget.spent, str(exc), errors)

    errors = tuple(gathered.errors)
    if degraded:
        return ResearchOutcome(pid, user_id, "incompleto", version, budget.spent, degraded, errors)
    return ResearchOutcome(pid, user_id, "investigado", version, budget.spent, None, errors)
