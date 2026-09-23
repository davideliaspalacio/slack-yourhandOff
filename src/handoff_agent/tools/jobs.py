"""Open job postings for a company, via JobSpy.

Hiring is Handoff's buying signal, so this is the highest-value corroboration
in the dossier. JobSpy reads LinkedIn's public guest surface, so no LinkedIn
account is involved and nothing here risks an account ban.

Job boards block scrapers routinely. A failure here degrades the dossier; it
must never take down a research run.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from jobspy import scrape_jobs

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
    return scrape_jobs(**kwargs)


LEGAL_SUFFIXES = (
    "inc",
    "inc.",
    "llc",
    "l.l.c.",
    "ltd",
    "ltd.",
    "limited",
    "corp",
    "corp.",
    "corporation",
    "co",
    "co.",
    "company",
    "gmbh",
    "sa",
    "s.a.",
    "sas",
    "bv",
    "plc",
    "ag",
    "oy",
    "ab",
    "pte",
    "pty",
)


def _normalise_company(name: str) -> str:
    """Lowercase, strip punctuation noise and drop a trailing legal suffix, so
    that "Acme, Inc." and "Acme" are the same employer."""
    cleaned = name.strip().casefold().replace(",", " ").replace("&", " and ")
    words = cleaned.split()
    while words and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def _matches_company(row_company: str, wanted: str) -> bool:
    """JobSpy searches by keyword, not by employer, so a search for "Acme"
    happily returns jobs at other companies whose text mentions Acme. Without
    this filter the dossier fills with other employers' vacancies and each one
    adds a spurious +2 of corroboration to the score.

    Matching is exact after normalising: prefix matching let "Metabase" pass
    for "Meta" and "Handoff Logistics" for "Handoff", which is the same bug
    wearing a different hat.
    """
    a = _normalise_company(row_company)
    return bool(a) and a == _normalise_company(wanted)


def _text_or_empty(value) -> str:
    """pandas deja NaN en las celdas vacías y str(NaN) es "nan": una URL así
    entraría en el dossier como si fuera una fuente."""
    return value if isinstance(value, str) else ""


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
            url=_text_or_empty(row.get("job_url")),
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


@dataclass(frozen=True)
class OfertasCuenta:
    """Lo que devolvieron los boards para una cuenta del radar. A diferencia de
    buscar_ofertas, los fallos no se tragan: el radar necesita saber qué board
    falló para no dar por cerradas las vacantes que solo ese board veía."""

    postings: list[JobPosting]
    errores: list[str]


def _sin_acentos(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))


def _coincide_empleador(row_company: str, wanted: str) -> bool:
    """El filtro de empleador del radar. Igual de exacto que _matches_company,
    pero acepta también la parte antes de " – ", " | " o "(" y no distingue
    acentos: LinkedIn llama "CODELCO – Corporación Nacional del Cobre de Chile"
    a Codelco, y con el filtro exacto el radar veía cero vacantes. "Codelco
    Tech" sigue sin pasar por "Codelco"."""
    variantes = {row_company, re.split(r"\s+[-–—|]\s+|\s*\(", row_company, maxsplit=1)[0]}
    return any(_matches_company(_sin_acentos(v), _sin_acentos(wanted)) for v in variantes)


def ofertas_de_cuenta(
    company: str, linkedin_company_id: str | None = None, limit: int = 50
) -> OfertasCuenta:
    """Vacantes abiertas de una cuenta objetivo, una consulta por board.

    LinkedIn por id de empresa cuando se conoce (`linkedin_company_ids` de
    JobSpy, la superficie pública, sin cuenta): es preciso y no necesita el
    filtro de empleador, que descartaría "CODELCO - Corporación Nacional del
    Cobre" al buscar "Codelco". Sin id, y siempre en Indeed, por nombre con el
    filtro de empleador de buscar_ofertas.
    """
    consultas: list[tuple[str, dict, bool]] = []
    if linkedin_company_id:
        consultas.append(
            (
                "linkedin",
                {
                    "linkedin_company_ids": [int(linkedin_company_id)],
                    # Sin ubicación, la búsqueda pública de LinkedIn se limita
                    # a EE. UU.: una empresa chilena volvía vacía (visto en vivo
                    # con Codelco el 2026-09-22).
                    "location": "Worldwide",
                    "results_wanted": limit,
                },
                False,
            )
        )
    else:
        consultas.append(
            (
                "linkedin",
                {"search_term": company, "location": "Worldwide", "results_wanted": limit * 4},
                True,
            )
        )
    consultas.append(("indeed", {"search_term": company, "results_wanted": limit * 4}, True))

    postings: list[JobPosting] = []
    errores: list[str] = []
    escaneadas = 0
    for site, kwargs, filtrar in consultas:
        try:
            frame = _scrape(site_name=[site], **kwargs)
        except Exception as exc:  # noqa: BLE001 - un board caído no tumba la cuenta
            errores.append(f"{site}: {type(exc).__name__}: {exc}"[:300])
            continue
        escaneadas += len(frame)
        encontradas = [
            JobPosting(
                title=str(row.get("title", "")),
                company=str(row.get("company", "")),
                location=_text_or_empty(row.get("location")),
                url=_text_or_empty(row.get("job_url")),
                # El board es el que se consultó: el radar guarda la fuente en
                # una columna con valores cerrados.
                site=site,
            )
            for _, row in frame.iterrows()
            if not filtrar or _coincide_empleador(str(row.get("company", "")), company)
        ]
        postings.extend(encontradas[:limit])

    ledger.record_action(
        action="ofertas_cuenta",
        payload={"company": company, "linkedin_company_id": linkedin_company_id},
        result={"count": len(postings), "scanned": escaneadas, "errores": errores},
    )
    return OfertasCuenta(postings=postings, errores=errores)
