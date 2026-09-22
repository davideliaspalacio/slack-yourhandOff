"""The model decides what is missing; the code decides what runs.

After the first synthesis the model names up to three searches that would close
its biggest gaps. This is where the AI interacts with the tools — but it only
proposes, the searches are read-only, and both count and length are capped.
"""

from __future__ import annotations

from ..tools import search
from .gather import Evidence, Gathered, dedupe_evidence

MAX_QUERIES = 3
MAX_QUERY_CHARS = 200


def suggested_queries(dossier: dict) -> list[str]:
    raw_queries = dossier.get("busquedas_sugeridas")
    # El validador no mira esta clave: un número rompería el bucle y una cadena
    # se iteraría letra por letra.
    if not isinstance(raw_queries, list):
        return []
    queries = []
    for raw in raw_queries:
        if isinstance(raw, str) and raw.strip():
            queries.append(raw.strip()[:MAX_QUERY_CHARS])
        if len(queries) == MAX_QUERIES:
            break
    return queries


def run_followup(prospect_id: str, gathered: Gathered, queries: list[str]) -> Gathered:
    extra: list[Evidence] = []
    errors = list(gathered.errors)
    for query in queries:
        try:
            for r in search.buscar_web(query, limit=5, prospect_id=prospect_id):
                if r.title or r.snippet:
                    extra.append(Evidence("seguimiento", r.url, r.title, r.snippet))
        except search.SearchUnavailable as exc:
            errors.append(f"seguimiento {query!r}: {exc}")
    # La salud del buscador se mide solo en la recolección inicial (dominio,
    # linkedin, prensa): el seguimiento arrastra ese recuento sin tocarlo.
    return Gathered(
        gathered.domain,
        dedupe_evidence(gathered.evidence + extra),
        gathered.jobs,
        errors,
        searches_attempted=gathered.searches_attempted,
        searches_answered=gathered.searches_answered,
        domain_guessed=gathered.domain_guessed,
        # El seguimiento no vuelve a llamar al webhook: ya se pagó y se guardó
        # en la primera pasada.
        proveedor=gathered.proveedor,
        # Igual que `proveedor`: la confirmación del dominio ya se decidió en
        # la primera pasada, el seguimiento solo la arrastra.
        unconfirmed_domain=gathered.unconfirmed_domain,
    )
