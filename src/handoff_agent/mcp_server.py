"""MCP server exposing the research toolbox.

One contract, three consumers: the automated pipeline via function calling,
the team debugging from Claude Code, and the CEO Command Center later on.
Tool names are Spanish because they are the team's interface.

Built on the MCP Python SDK 2.x API (MCPServer); in 1.x this class was named
FastMCP.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from mcp.server.mcpserver import MCPServer

from . import db, ledger
from .tools import jobs, prospects, search, web

mcp = MCPServer("handoff-tools")


@mcp.tool()
def buscar_web(query: str, limit: int = 8) -> list[dict]:
    """Busca en la web pública vía SearXNG. Devuelve título, url y extracto."""
    return [asdict(r) for r in search.buscar_web(query, limit=limit)]


@mcp.tool()
def leer_sitio(url: str, max_chars: int = 20_000) -> dict:
    """Descarga una página y devuelve su texto legible, sin navegación ni banners."""
    return asdict(web.leer_sitio(url, max_chars=max_chars))


@mcp.tool()
def buscar_ofertas(company: str, limit: int = 20) -> list[dict]:
    """Ofertas de trabajo abiertas de una empresa. Lista vacía si los boards bloquean."""
    return [asdict(p) for p in jobs.buscar_ofertas(company, limit=limit)]


@mcp.tool()
def historial_prospecto(slack_user_id: str) -> dict:
    """Qué sabemos ya de esta persona: ficha, último dossier y acciones recientes."""
    history = prospects.historial_prospecto(slack_user_id)
    return {
        "prospect": _jsonable(history["prospect"]),
        "dossier": _jsonable(history["dossier"]),
        "actions": [_jsonable(a) for a in history["actions"]],
    }


@mcp.tool()
def resumen_costes(days: int = 30) -> dict:
    """Gasto de los últimos N días, sumando llamadas a LLM y costes externos."""
    since = datetime.now(UTC) - timedelta(days=days)
    total = ledger.spend_since(since)
    counts = db.fetch_one(
        "select (select count(*) from llm_calls where created_at >= %s) as llm_calls, "
        "       (select count(*) from cost_events where created_at >= %s) as cost_events",
        (since, since),
    )
    return {
        "days": days,
        "total_usd": f"{total:.2f}",
        "llm_calls": counts["llm_calls"],
        "cost_events": counts["cost_events"],
    }


def _jsonable(row: dict | None) -> dict | None:
    """Datetimes and UUIDs do not survive JSON on their own."""
    if row is None:
        return None
    return {
        k: (v.isoformat() if hasattr(v, "isoformat") else str(v) if hasattr(v, "hex") else v)
        for k, v in row.items()
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
