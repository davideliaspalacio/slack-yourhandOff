"""Cuándo se puede volver a investigar sola a una persona cuyo research salió mal.

"Mal" son dos cosas: un dossier degradado (ninguna búsqueda devolvió nada) o un research
que no llegó a dejar dossier (`research_incompleto`: el modelo no dio uno válido, o se
pasó del presupuesto). Hasta ahora ninguna de las dos tenía freno: cualquier disparador
-- otro mensaje, el padrón de otro canal -- rehacía el research entero, lo pagaba entero
y, como cada dossier nuevo era una versión nueva, publicaba otra tarjeta. Una empresa
poco conocida cuyas búsquedas vuelven vacías no mejora por preguntar otra vez a los
cinco minutos.

La regla, en un solo sitio (la usan `research/worker.py` y `ingest/resolver.py`):

- entre dos intentos automáticos pasan al menos `research_reintento_horas` (24): un
  buscador vetado por CAPTCHA suele volver en horas, no en minutos;
- como mucho `research_reintentos_max` (2) reintentos automáticos: tres research
  pagados por persona, y después solo a mano;
- un dossier sano pone la cuenta a cero, y los fallos de hace más de `CADUCA` dejan de
  contar: quien vuelve a escribir medio año después merece otro intento.

El research pedido a mano (`force`: "Help the research" del panel, `--forzar`, el MCP)
no pasa por aquí: una persona que lo pide sabe algo que el agente no.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .. import db, db_config

HORAS = 24
MAXIMO = 2
# El mismo plazo que la frescura de un dossier sano (`worker.FRESH_FOR`).
CADUCA = timedelta(days=180)


@dataclass(frozen=True)
class Estado:
    # Research fallidos seguidos (degradados o sin dossier) desde el último dossier sano.
    intentos: int
    ultimo: datetime | None


def estado(prospect_id: str, ahora: datetime | None = None) -> Estado:
    ahora = ahora or datetime.now(UTC)
    fila = db.fetch_one(
        """
        with sano as (
            select max(version) as version, max(created_at) as en
              from dossiers
             where prospect_id = %(pid)s
               and not coalesce((content->>'degradado')::boolean, false)
        ), fallos as (
            select d.created_at
              from dossiers d, sano
             where d.prospect_id = %(pid)s
               and coalesce((d.content->>'degradado')::boolean, false)
               and (sano.version is null or d.version > sano.version)
            union all
            select a.created_at
              from agent_actions a, sano
             where a.prospect_id = %(pid)s
               and a.action = 'research_incompleto'
               and (sano.en is null or a.created_at > sano.en)
        )
        select count(*) as intentos, max(created_at) as ultimo
          from fallos
         where created_at >= %(desde)s
        """,
        {"pid": prospect_id, "desde": ahora - CADUCA},
    )
    return Estado(fila["intentos"], fila["ultimo"])


def espera(prospect_id: str, ahora: datetime | None = None) -> str | None:
    """Por qué NO toca investigar todavía a esta persona, o None si se puede.

    Solo tiene sentido preguntarlo de quien quedó `incompleto` o tiene como último un
    dossier degradado; de los demás decide la frescura del dossier."""
    ahora = ahora or datetime.now(UTC)
    visto = estado(prospect_id, ahora)
    if visto.intentos == 0 or visto.ultimo is None:
        return None
    maximo = db_config.value("research_reintentos_max", MAXIMO)
    if visto.intentos > maximo:
        return (
            f"reintentos automáticos agotados ({visto.intentos} research sin un dossier "
            "sano); solo a mano"
        )
    horas = db_config.value("research_reintento_horas", HORAS)
    if ahora - visto.ultimo < timedelta(hours=horas):
        return f"reintento en espera: el último research falló hace menos de {horas} h"
    return None
