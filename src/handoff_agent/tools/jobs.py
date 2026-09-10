"""Open job postings for a company, via JobSpy.

Hiring is Handoff's buying signal, so this is the highest-value corroboration
in the dossier. JobSpy reads LinkedIn's public guest surface, so no LinkedIn
account is involved and nothing here risks an account ban.

Job boards block scrapers routinely. A failure here degrades the dossier; it
must never take down a research run.
"""
from __future__ import annotations

from dataclasses import dataclass

from .. import ledger


@dataclass(frozen=True)
class JobPosting:
    title: str
    company: str
    location: str
    url: str
    site: str


def _scrape(**kwargs):
    """Indirection so tests can substitute the scraper without network access."""
    from jobspy import scrape_jobs

    return scrape_jobs(**kwargs)


def buscar_ofertas(company: str, limit: int = 20) -> list[JobPosting]:
    """Return open postings for `company`. Empty list if the boards block us."""
    try:
        frame = _scrape(
            site_name=["linkedin", "indeed"],
            search_term=company,
            results_wanted=limit,
        )
    except Exception as exc:  # noqa: BLE001 - degrading is the policy here
        ledger.record_action(
            action="buscar_ofertas", payload={"company": company},
            result={"error": str(exc), "count": 0},
        )
        return []

    postings = [
        JobPosting(
            title=str(row.get("title", "")),
            company=str(row.get("company", "")),
            location=str(row.get("location", "")),
            url=str(row.get("job_url", "")),
            site=str(row.get("site", "")),
        )
        for _, row in frame.iterrows()
    ][:limit]

    ledger.record_action(
        action="buscar_ofertas", payload={"company": company, "limit": limit},
        result={"count": len(postings)},
    )
    return postings
