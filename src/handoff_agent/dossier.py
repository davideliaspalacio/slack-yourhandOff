"""What a dossier must look like before it is allowed into the database.

The rule that matters most is grounding: every source a dossier cites must be
one we fetched. A model asked to research a company will, if allowed, produce a
confident funding round with a plausible-looking URL. The validator is what
makes "every fact has a source" true rather than aspirational.
"""

from __future__ import annotations

import html
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
OBJECT_SECTIONS = ("persona", "empresa", "contratacion", "encaje_handoff")
LIST_SECTIONS = ("senales_contexto", "huecos")
# nombre y dominio pueden venir de la entrada; el resto hay que haberlo leído.
COMPANY_FACTS = ("sector", "empleados_aprox", "ubicacion", "descripcion")


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


def _has_source(value) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(isinstance(v, str) and v.strip() for v in value)
    return False


def _grounding_problems(data: dict) -> list[str]:
    """Una URL válida no basta: cada dato afirmado tiene que llevar la suya."""
    problems = []

    for index, item in enumerate(data["senales_contexto"]):
        path = f"senales_contexto[{index}]"
        if not isinstance(item, dict):
            problems.append(f"{path} debe ser un objeto con hecho y fuente")
            continue
        fact = item.get("hecho")
        if not isinstance(fact, str) or not fact.strip():
            problems.append(f"{path} no tiene hecho")
        source = item.get("fuente")
        if not isinstance(source, str) or not source.strip():
            problems.append(f"{path} no tiene fuente")

    persona = data["persona"]
    if persona.get("cargo") is not None and not _has_source(persona.get("fuente")):
        problems.append("persona.cargo no tiene fuente")

    empresa = data["empresa"]
    stated = [f for f in COMPANY_FACTS if empresa.get(f) is not None]
    if stated and not _has_source(empresa.get("fuentes")):
        problems.append(f"empresa afirma {', '.join(stated)} sin fuentes")

    hiring = data["contratacion"]
    if (
        hiring.get("vacantes_abiertas") is not None
        or hiring.get("roles")
        or hiring.get("roles_deslocalizables")
    ) and not _has_source(hiring.get("fuentes")):
        problems.append("contratacion afirma vacantes o roles sin fuentes")

    return problems


def validate_dossier(data: dict, allowed_sources: set[str]) -> list[str]:
    if not isinstance(data, dict):
        return ["el dossier no es un objeto JSON"]

    problems = [f"falta la sección {s!r}" for s in REQUIRED_SECTIONS if s not in data]
    if problems:
        return problems

    problems += [
        f"{s} debe ser un objeto" for s in OBJECT_SECTIONS if not isinstance(data[s], dict)
    ]
    problems += [f"{s} debe ser una lista" for s in LIST_SECTIONS if not isinstance(data[s], list)]
    if problems:
        # Con la forma rota, las comprobaciones de abajo no tienen dónde mirar.
        return problems

    for path, url in cited_sources(data):
        # fence() escapa el origen, así que el modelo cita `&amp;` donde la URL
        # real lleva `&`: se compara la forma sin escapar.
        if html.unescape(url) not in allowed_sources:
            problems.append(f"{path} cita una fuente que no se consultó: {url}")

    score = data["encaje_handoff"].get("puntuacion")
    if type(score) is not int or not 0 <= score <= 3:
        problems.append("encaje_handoff.puntuacion debe ser un entero de 0 a 3")

    if not isinstance(data["resumen"], str) or not data["resumen"].strip():
        problems.append("el resumen está vacío")

    problems += _grounding_problems(data)
    return problems
