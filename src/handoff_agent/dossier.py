"""What a dossier must look like before it is allowed into the database.

The rule that matters most is grounding: every source a dossier cites must be
one we fetched. A model asked to research a company will, if allowed, produce a
confident funding round with a plausible-looking URL. The validator is what
makes "every fact has a source" true rather than aspirational.
"""

from __future__ import annotations

from decimal import Decimal

REQUIRED_SECTIONS = (
    "persona",
    "empresa",
    "contratacion",
    "senales_contexto",
    "encaje_handoff",
    "huecos",
    "resumen",
)
SOURCE_KEYS = ("fuente", "fuentes")


class DossierInvalid(ValueError):
    """The model could not produce a dossier that passes validation."""

    def __init__(self, problems: list[str], cost_usd: Decimal = Decimal(0)) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems
        self.cost_usd = cost_usd


def cited_sources(data: dict) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                child = f"{path}.{key}" if path else key
                if key in SOURCE_KEYS:
                    urls = [value] if isinstance(value, str) else (value or [])
                    found.extend((child, str(url)) for url in urls if url)
                else:
                    walk(value, child)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")

    walk(data, "")
    return found


def validate_dossier(data: dict, allowed_sources: set[str]) -> list[str]:
    if not isinstance(data, dict):
        return ["el dossier no es un objeto JSON"]

    problems = [f"falta la sección {s!r}" for s in REQUIRED_SECTIONS if s not in data]
    if problems:
        return problems

    for path, url in cited_sources(data):
        if url not in allowed_sources:
            problems.append(f"{path} cita una fuente que no se consultó: {url}")

    fit = data["encaje_handoff"]
    score = fit.get("puntuacion") if isinstance(fit, dict) else None
    if type(score) is not int or not 0 <= score <= 3:
        problems.append("encaje_handoff.puntuacion debe ser un entero de 0 a 3")

    if not isinstance(data["resumen"], str) or not data["resumen"].strip():
        problems.append("el resumen está vacío")
    if not isinstance(data["huecos"], list):
        problems.append("huecos debe ser una lista")
    if not isinstance(data["senales_contexto"], list):
        problems.append("senales_contexto debe ser una lista")

    return problems
