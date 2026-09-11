"""The model decides what is missing; the code decides what runs.

After the first synthesis the model names up to three searches that would close
its biggest gaps. This is where the AI interacts with the tools — but it only
proposes, the searches are read-only, and both count and length are capped.
"""

from __future__ import annotations

from ..tools import search
from .gather import Evidence, Gathered, SearchTally, dedupe_evidence

MAX_QUERIES = 3
MAX_QUERY_CHARS = 200


def suggested_queries(dossier: dict) -> list[str]:
    queries = []
    for raw in dossier.get("busquedas_sugeridas") or []:
        if isinstance(raw, str) and raw.strip():
            queries.append(raw.strip()[:MAX_QUERY_CHARS])
        if len(queries) == MAX_QUERIES:
            break
    return queries


def run_followup(prospect_id: str, gathered: Gathered, queries: list[str]) -> Gathered:
    extra: list[Evidence] = []
    errors = list(gathered.errors)
    # El recuento sigue desde la recolección: si el seguimiento sí encuentra
    # algo, el buscador funciona y la recolección final deja de estar degradada.
    tally = SearchTally(gathered.searches_attempted, gathered.searches_answered)
    for query in queries:
        try:
            for r in tally.run(query, limit=5, prospect_id=prospect_id):
                if r.title or r.snippet:
                    extra.append(Evidence("seguimiento", r.url, r.title, r.snippet))
        except search.SearchUnavailable as exc:
            errors.append(f"seguimiento {query!r}: {exc}")
    return Gathered(
        gathered.domain,
        dedupe_evidence(gathered.evidence + extra),
        gathered.jobs,
        errors,
        searches_attempted=tally.attempted,
        searches_answered=tally.answered,
    )
