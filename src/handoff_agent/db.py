"""Postgres access for the workers.

Workers connect straight to Postgres rather than through PostgREST because the
job queue relies on SELECT ... FOR UPDATE SKIP LOCKED, which PostgREST does not
expose. This connection carries service_role and therefore bypasses RLS.
"""
from __future__ import annotations

import atexit
from functools import lru_cache
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import load_settings


@lru_cache(maxsize=1)
def get_pool() -> ConnectionPool:
    settings = load_settings()
    pool = ConnectionPool(settings.database_url, min_size=1, max_size=8, open=True)
    # Without this, psycopg's worker threads outlive the interpreter and every
    # run ends in "couldn't stop thread" warnings that hide real problems.
    atexit.register(pool.close)
    return pool


def fetch_one(sql: str, params: tuple[Any, ...] = ()) -> dict | None:
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def fetch_all(sql: str, params: tuple[Any, ...] = ()) -> list[dict]:
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def execute(sql: str, params: tuple[Any, ...] = ()) -> int:
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount
