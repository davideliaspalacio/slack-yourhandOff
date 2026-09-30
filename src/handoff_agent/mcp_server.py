"""MCP server exposing the research toolbox.

One contract, three consumers: the automated pipeline via function calling,
the team debugging from Claude Code, and the CEO Command Center later on.
Tool names are Spanish because they are the team's interface.

Built on the MCP Python SDK 2.x API (MCPServer); in 1.x this class was named
FastMCP.
"""

from __future__ import annotations

from dataclasses import asdict

from mcp.server.mcpserver import MCPServer

from . import ledger, untrusted
from .accounts import decisor
from .accounts import repo as cuentas
from .research import worker
from .serialize import jsonable
from .tools import jobs, prospects, search, web

mcp = MCPServer("handoff-tools")


@mcp.tool()
def buscar_web(query: str, limit: int = 8, prospect_id: str | None = None) -> list[dict]:
    """Busca en la web pública vía SearXNG. Devuelve título, url y extracto."""
    return [asdict(r) for r in search.buscar_web(query, limit=limit, prospect_id=prospect_id)]


@mcp.tool()
def leer_sitio(url: str, max_chars: int = 20_000, prospect_id: str | None = None) -> dict:
    """Descarga una página y devuelve su texto legible, sin navegación ni banners."""
    page = web.leer_sitio(url, max_chars=max_chars, prospect_id=prospect_id)
    return {
        "url": page.url,
        "final_url": page.final_url,
        "title": untrusted.neutralise(page.title),
        # Delimitado a propósito: esto lo escribió un tercero y va directo al
        # contexto de un modelo. Sin marca, un texto plantado en una página de
        # careers se lee igual que una instrucción nuestra.
        "text": untrusted.fence(page.text, page.final_url),
    }


@mcp.tool()
def buscar_ofertas(company: str, limit: int = 20, prospect_id: str | None = None) -> list[dict]:
    """Ofertas de trabajo abiertas de una empresa. Lista vacía si los boards bloquean."""
    return [asdict(p) for p in jobs.buscar_ofertas(company, limit=limit, prospect_id=prospect_id)]


@mcp.tool()
def historial_prospecto(slack_user_id: str) -> dict:
    """Qué sabemos ya de esta persona: ficha, último dossier y acciones recientes."""
    history = prospects.historial_prospecto(slack_user_id)
    return {
        "prospect": jsonable(history["prospect"]),
        "dossier": jsonable(history["dossier"]),
        "actions": [jsonable(a) for a in history["actions"]],
    }


@mcp.tool()
def resumen_costes(days: int = 30) -> dict:
    """Gasto de los últimos N días, sumando llamadas a LLM y costes externos."""
    return ledger.cost_summary(days)


@mcp.tool()
def investigar_persona(
    nombre: str | None = None,
    empresa: str | None = None,
    dominio: str | None = None,
    forzar: bool = False,
) -> dict:
    """Investiga a una persona y su empresa y guarda el dossier. Cuesta dinero:
    usa GPT-4.1, con tope por ejecución. No repite si hay un dossier de menos de
    6 meses, salvo con forzar=true."""
    try:
        outcome = worker.research_person(nombre, empresa, dominio, force=forzar)
    except ValueError as exc:
        return {"estado": "error", "motivo": str(exc)}
    except worker.SYSTEM_STOPS as exc:
        # Kill switch o tope mensual: una respuesta, no una excepción cruda.
        return {"estado": "detenido", "motivo": str(exc)}
    history = prospects.historial_prospecto(outcome.slack_user_id)
    return {
        "estado": outcome.status,
        "version": outcome.version,
        "coste_usd": f"{outcome.cost_usd:.4f}",
        "motivo": outcome.reason,
        "errores": list(outcome.errors),
        "dossier": jsonable(history["dossier"]),
    }


@mcp.tool()
def senales_empresa(dominio_o_nombre: str, incluir_cerradas: bool = False) -> dict:
    """Vacantes que el radar ha visto en una cuenta objetivo (por dominio o
    nombre): título, fuentes, score, estado y fechas. Solo lee lo guardado; no
    escanea ni llama a nadie. Para escanear, `handoff radar --cuenta ID`."""
    cuenta = cuentas.buscar_cuenta(dominio_o_nombre)
    if cuenta is None:
        return {"cuenta": None, "senales": [], "motivo": "no es una cuenta objetivo"}
    senales = []
    for senal in cuentas.senales_de_cuenta(cuenta["id"], incluir_cerradas=incluir_cerradas):
        fila = jsonable(senal)
        # El título y la ubicación los escribió la empresa en un job board:
        # texto de terceros que va directo al contexto de un modelo.
        fila["title"] = untrusted.neutralise(senal["title"])
        if senal["location"]:
            fila["location"] = untrusted.neutralise(senal["location"])
        senales.append(fila)
    return {"cuenta": jsonable(cuenta), "senales": senales}


@mcp.tool()
def buscar_decisor(signal_id: str) -> dict:
    """Busca ya quién decide la contratación de una vacante del radar y deja al
    elegido en la cola de research.

    OJO: usa la cuenta de LinkedIn (Sales Navigator) de Anthony vía Unipile,
    con sus topes diarios y su horario, y cuesta dinero (GPT-4.1 para los
    cargos y, después, el research del elegido). Úsalo solo cuando de verdad
    se quiera perseguir esa vacante. Devuelve el estado (researching,
    pospuesta, sin_linkedin_id, sin_candidatos, error, omitida, ocupada), los
    cargos buscados y los candidatos ordenados, el elegido primero.
    """
    try:
        resultado = decisor.procesar_senal(signal_id)
    except LookupError as exc:
        return {"estado": "no_encontrada", "detalle": str(exc), "cargos": [], "candidatos": []}
    except worker.SYSTEM_STOPS as exc:
        return {"estado": "detenido", "detalle": str(exc), "cargos": [], "candidatos": []}
    candidatos = []
    for candidato in decisor.candidatos_de_senal(resultado.signal_id):
        fila = jsonable(candidato)
        # Nombre y headline los escribió cada persona en LinkedIn: texto de
        # terceros que va directo al contexto de un modelo.
        for clave in ("full_name", "headline", "location"):
            if candidato[clave]:
                fila[clave] = untrusted.neutralise(candidato[clave])
        candidatos.append(fila)
    return {
        "estado": resultado.estado,
        "detalle": resultado.detalle,
        "cargos": resultado.cargos,
        "candidatos": candidatos,
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
