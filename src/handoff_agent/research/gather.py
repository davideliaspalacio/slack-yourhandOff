"""Deterministic evidence collection. No LLM decides anything here.

Collection is nearly free and entirely predictable, so the model is not asked
what to fetch: that is where agentic loops burn money. Every step degrades on
failure — a 404 on /about or a search outage becomes an entry in `errors`, and
the dossier is built from whatever did come back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..tools import jobs, search, web

PAGE_CHARS = 6_000
CANDIDATE_PATHS = {"home": "/", "about": "/about", "careers": "/careers"}

# Results that are about the company but are not the company's own site.
NOT_A_COMPANY_SITE = (
    "linkedin.com",
    "crunchbase.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "youtube.com",
    "wikipedia.org",
    "glassdoor.com",
    "indeed.com",
    "bloomberg.com",
    "zoominfo.com",
    "pitchbook.com",
    "g2.com",
    "capterra.com",
)


@dataclass(frozen=True)
class Evidence:
    kind: str
    url: str
    title: str
    text: str


@dataclass
class Gathered:
    domain: str | None
    evidence: list[Evidence]
    jobs: list[jobs.JobPosting]
    errors: list[str] = field(default_factory=list)
    searches_attempted: int = 0
    searches_answered: int = 0

    @property
    def sources(self) -> set[str]:
        return {e.url for e in self.evidence} | {j.url for j in self.jobs if j.url}

    def source_records(self) -> list[dict]:
        """Las fuentes con su procedencia, una por URL, para guardar en
        `dossiers.sources`: `{"url", "kind", "title"}`; las vacantes llevan
        `kind="vacante"` y el título de la oferta.

        Las 6 filas antiguas (prueba real del 2026-09-10) guardan URLs sueltas:
        quien lea `dossiers.sources` debe aceptar ambas formas.
        """
        records: list[dict] = []
        seen: set[str] = set()
        candidates = [(e.url, e.kind, e.title) for e in self.evidence] + [
            (j.url, "vacante", j.title) for j in self.jobs
        ]
        for url, kind, title in candidates:
            if url and url not in seen:
                seen.add(url)
                records.append({"url": url, "kind": kind, "title": title})
        return records

    @property
    def search_degraded(self) -> bool:
        """Se buscó y ninguna búsqueda trajo nada: casi siempre el buscador
        vetado, no una empresa sin rastro. Un dossier así no es de fiar."""
        return self.searches_attempted > 0 and self.searches_answered == 0


@dataclass
class SearchTally:
    """Cuenta las búsquedas intentadas y las que devolvieron algo."""

    attempted: int = 0
    answered: int = 0

    def run(self, query: str, limit: int, prospect_id: str | None) -> list[search.SearchResult]:
        self.attempted += 1
        results = search.buscar_web(query, limit=limit, prospect_id=prospect_id)
        if results:
            self.answered += 1
        return results


def dedupe_evidence(items: list[Evidence]) -> list[Evidence]:
    """/about often redirects to the home page; keep one copy of each URL."""
    seen: set[str] = set()
    unique = []
    for item in items:
        if item.url not in seen:
            seen.add(item.url)
            unique.append(item)
    return unique


def resolve_domain(company: str, prospect_id: str, tally: SearchTally | None = None) -> str | None:
    run = tally.run if tally else search.buscar_web
    for result in run(f"{company} official website", limit=8, prospect_id=prospect_id):
        host = (urlparse(result.url).hostname or "").removeprefix("www.")
        if host and not any(host == d or host.endswith("." + d) for d in NOT_A_COMPANY_SITE):
            return host
    return None


def _search_into(evidence, errors, tally, kind, query, limit, prospect_id) -> None:
    try:
        for r in tally.run(query, limit=limit, prospect_id=prospect_id):
            if r.title or r.snippet:
                evidence.append(Evidence(kind, r.url, r.title, r.snippet))
    except search.SearchUnavailable as exc:
        errors.append(f"{kind}: {exc}")


def gather(
    prospect_id: str,
    full_name: str | None,
    company: str | None,
    domain: str | None = None,
) -> Gathered:
    evidence: list[Evidence] = []
    errors: list[str] = []
    tally = SearchTally()

    if not domain and company:
        try:
            domain = resolve_domain(company, prospect_id, tally)
        except search.SearchUnavailable as exc:
            errors.append(f"dominio: {exc}")

    if domain:
        for kind, path in CANDIDATE_PATHS.items():
            try:
                page = web.leer_sitio(
                    f"https://{domain}{path}", max_chars=PAGE_CHARS, prospect_id=prospect_id
                )
                evidence.append(Evidence(kind, page.final_url, page.title, page.text))
            except web.PageUnavailable as exc:
                errors.append(f"{kind}: {exc}")

    if full_name:
        # Fragmentos del buscador, nunca la página: descargar LinkedIn va contra
        # sus términos y es lo que tumbó a Proxycurl.
        query = f'site:linkedin.com/in "{full_name}"' + (f' "{company}"' if company else "")
        _search_into(evidence, errors, tally, "linkedin", query, 3, prospect_id)

    if company:
        _search_into(
            evidence,
            errors,
            tally,
            "prensa",
            f'"{company}" funding OR raises OR hiring',
            5,
            prospect_id,
        )

    found_jobs = jobs.buscar_ofertas(company, limit=20, prospect_id=prospect_id) if company else []
    return Gathered(
        domain,
        dedupe_evidence(evidence),
        found_jobs,
        errors,
        searches_attempted=tally.attempted,
        searches_answered=tally.answered,
    )
