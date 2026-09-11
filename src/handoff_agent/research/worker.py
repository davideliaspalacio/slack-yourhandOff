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


@dataclass(frozen=True)
class ResearchOutcome:
    prospect_id: str
    slack_user_id: str
    status: str
    version: int | None
    cost_usd: Decimal
    reason: str | None


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

    if not force:
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
    try:
        with budget:
            gathered = gather(pid, full_name, company, domain)
            result = synthesize(pid, full_name, company, gathered, budget=budget)

            queries = suggested_queries(result.dossier)
            if queries:
                enriched = run_followup(pid, gathered, queries)
                try:
                    result = synthesize(pid, full_name, company, enriched, budget=budget)
                    gathered = enriched
                except (DossierInvalid, guards.RunBudgetExceeded) as exc:
                    # La primera pasada ya es válida y está pagada: se guarda.
                    ledger.record_action(
                        "seguimiento_descartado", {"motivo": str(exc)}, prospect_id=pid
                    )

            version = prospects.save_dossier(pid, result.dossier, sorted(gathered.sources))
            empresa = result.dossier.get("empresa") or {}
            _set_state(
                pid, "investigado", company=empresa.get("nombre") or company, domain=gathered.domain
            )
    except (DossierInvalid, guards.RunBudgetExceeded) as exc:
        _set_state(pid, "incompleto")
        ledger.record_action("research_incompleto", {"motivo": str(exc)}, prospect_id=pid)
        return ResearchOutcome(pid, user_id, "incompleto", None, budget.spent, str(exc))

    return ResearchOutcome(pid, user_id, "investigado", version, budget.spent, None)
