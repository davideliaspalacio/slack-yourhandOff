"""Rows from the database, made safe for JSON.

Shared by the CLI and the MCP server so neither has to import the other.
"""

from __future__ import annotations


def jsonable(row: dict | None) -> dict | None:
    """Datetimes and UUIDs do not survive JSON on their own."""
    if row is None:
        return None
    return {
        k: (v.isoformat() if hasattr(v, "isoformat") else str(v) if hasattr(v, "hex") else v)
        for k, v in row.items()
    }
