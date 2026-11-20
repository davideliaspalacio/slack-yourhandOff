"""El enlace de LinkedIn de una persona, para los avisos y las tarjetas.

Dos enlaces distintos, y la diferencia importa:

- **El perfil**, cuando el research lo encontró. Sale del dossier (`persona.fuente`, la
  URL que el modelo citó para el cargo) o de los enlaces que el equipo puso a mano en
  el panel (`prospects.research_links`). Solo cuenta una URL `https://…linkedin.com/in/…`:
  una página de empresa o una búsqueda no son el perfil de nadie.
- **Una búsqueda de personas**, cuando no hay perfil. Se monta aquí, por código, con el
  nombre y la empresa: nunca la escribe un modelo ni un tercero. No es el perfil, y el
  texto del enlace lo dice ("Find … on LinkedIn"), para que nadie conecte con un
  homónimo creyendo que ya estaba comprobado.

El agente no abre LinkedIn ni conecta con nadie: esto solo deja el enlace a un clic.

Todo lo que entra viene de un tercero o de un modelo que leyó páginas ajenas. El perfil
se valida entero (esquema, dominio, ruta y que no pueda romper `<url|texto>`); en la
búsqueda, el nombre y la empresa van codificados dentro de la query y no pueden salirse
de ella.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import quote_plus, urlparse

# Lo que entra en la búsqueda. Un nombre de perfil de Slack puede ser cualquier cosa;
# con esto sobra para encontrar a alguien y el enlace no crece sin fin.
MAX_TERMINOS = 120
# Un perfil real ronda los 60 caracteres. Algo mucho más largo no es un perfil.
MAX_URL = 300
BUSQUEDA = "https://www.linkedin.com/search/results/people/?keywords="


def es_perfil(url: object) -> bool:
    """Si `url` es el perfil de LinkedIn de una persona y se puede enlazar tal cual."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL:
        return False
    # Lo mismo que `card._is_safe_link`, más los espacios: nada que cierre el enlace de
    # Slack antes de tiempo.
    if any(ch in url for ch in "|<>") or any(ch.isspace() for ch in url):
        return False
    partes = urlparse(url)
    if partes.scheme != "https" or partes.username or partes.password or partes.port:
        return False
    host = (partes.hostname or "").lower()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return False
    # `/in/<alias>`: las empresas viven en `/company/` y las búsquedas en `/search/`.
    return partes.path.startswith("/in/") and len(partes.path) > len("/in/")


def perfil(dossier: object, enlaces_equipo: Iterable[object] | None = None) -> str | None:
    """El perfil de la persona, o None si no se conoce.

    Primero lo que el equipo puso a mano: alguien de Handoff lo eligió a propósito, y
    eso gana a lo que citó el modelo."""
    for enlace in enlaces_equipo or ():
        if es_perfil(enlace):
            return enlace
    persona = dossier.get("persona") if isinstance(dossier, dict) else None
    fuente = persona.get("fuente") if isinstance(persona, dict) else None
    return fuente if es_perfil(fuente) else None


def busqueda(nombre: str | None, empresa: str | None = None) -> str | None:
    """Una búsqueda de personas en LinkedIn con el nombre (y la empresa, si se sabe).
    None sin nombre: buscar solo por empresa no encuentra a la persona."""
    nombre = " ".join((nombre or "").split())
    if not nombre:
        return None
    terminos = " ".join(f"{nombre} {' '.join((empresa or '').split())}".split())
    return BUSQUEDA + quote_plus(terminos[:MAX_TERMINOS])


def linea(
    dossier: object,
    nombre: str | None,
    empresa: str | None = None,
    enlaces_equipo: Iterable[object] | None = None,
) -> str | None:
    """El recordatorio de conectar, en mrkdwn de Slack: con el perfil si se conoce, con
    la búsqueda si no, y None si no hay ni nombre con que buscar.

    El texto visible del enlace es fijo: el nombre no entra en él, así que no hay nada
    de un tercero que escapar aquí."""
    url = perfil(dossier, enlaces_equipo)
    if url:
        return f"🔗 *Connect on LinkedIn:* <{url}|Open their profile>"
    url = busqueda(nombre, empresa)
    if url:
        return f"🔗 *Connect on LinkedIn:* <{url}|Find them on LinkedIn> (search, not a confirmed profile)"
    return None
