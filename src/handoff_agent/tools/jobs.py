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


def _matches_company(row_company: str, wanted: str) -> bool:
    """JobSpy searches by keyword, not by employer, so a search for "Acme"
    happily returns jobs at other companies whose text mentions Acme. Without
    this filter the dossier fills with other employers' vacancies and the
    corroboration signal in the score becomes noise."""
    a = row_company.strip().casefold()
    b = wanted.strip().casefold()
    return bool(a) and (a == b or a.startswith(b) or b.startswith(a))


def buscar_ofertas(
    company: str, limit: int = 20, prospect_id: str | None = None
) -> list[JobPosting]:
    """Return open postings whose employer is `company`.

    Empty list if the boards block us or nothing matches the employer.
    """
    try:
        frame = _scrape(
            site_name=["linkedin", "indeed"],
            search_term=company,
            # Ask for more than we need: the employer filter below discards
            # most keyword matches.
            results_wanted=limit * 4,
        )
    except Exception as exc:  # noqa: BLE001 - degrading is the policy here
        ledger.record_action(
            action="buscar_ofertas",
            payload={"company": company},
            result={"error": str(exc), "count": 0},
            prospect_id=prospect_id,
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
        if _matches_company(str(row.get("company", "")), company)
    ][:limit]

    ledger.record_action(
        action="buscar_ofertas",
        payload={"company": company, "limit": limit},
        result={"count": len(postings), "scanned": len(frame)},
        prospect_id=prospect_id,
    )
    return postings
