"""Cuentas objetivo: alta, importación y consultas.

Es la vía de escritura desde Python (CLI e importación de CSV). El panel
escribe por panel_agregar_cuenta (0012), que aplica las mismas reglas: nombre
obligatorio, dominio normalizado y upsert por dominio (o por nombre, sin
dominio), sin borrar el linkedin_company_id que el radar ya resolvió.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import db

# El mismo patrón que el CHECK de target_accounts.domain y panel_corregir_web.
_DOMINIO_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_LINKEDIN_ID_RE = re.compile(r"^[0-9]{1,20}$")
_MAX_NOMBRE = 200


def normalizar_dominio(valor: str | None) -> str | None:
    """De "https://www.Acme.com/careers?x=1" a "acme.com". None si viene vacío;
    ValueError si lo que queda no es un dominio."""
    if valor is None or not valor.strip():
        return None
    dominio = valor.strip().lower()
    dominio = re.sub(r"^https?://", "", dominio)
    dominio = re.sub(r"^www\.", "", dominio)
    dominio = dominio.split("/", 1)[0].split("?", 1)[0].split(":", 1)[0]
    if not _DOMINIO_RE.match(dominio):
        raise ValueError(f"dominio inválido: {valor!r}")
    return dominio


def _validar(nombre: str | None, linkedin_company_id: str | None) -> tuple[str, str | None]:
    nombre = (nombre or "").strip()
    if not nombre:
        raise ValueError("el nombre de la empresa es obligatorio")
    if len(nombre) > _MAX_NOMBRE:
        raise ValueError(f"nombre demasiado largo (máximo {_MAX_NOMBRE} caracteres)")
    linkedin = (linkedin_company_id or "").strip() or None
    if linkedin is not None and not _LINKEDIN_ID_RE.match(linkedin):
        raise ValueError(f"id de empresa de LinkedIn inválido: {linkedin_company_id!r}")
    return nombre, linkedin


def agregar_cuenta(
    nombre: str,
    dominio: str | None = None,
    linkedin_company_id: str | None = None,
    source: str = "manual",
) -> dict:
    """Da de alta (o actualiza) una cuenta y devuelve su fila."""
    nombre, linkedin = _validar(nombre, linkedin_company_id)
    dominio = normalizar_dominio(dominio)
    if dominio is not None:
        return db.fetch_one(
            """
            insert into target_accounts (name, domain, linkedin_company_id, source)
            values (%s, %s, %s, %s)
            on conflict (domain) do update
                set name = excluded.name,
                    linkedin_company_id = coalesce(excluded.linkedin_company_id,
                                                   target_accounts.linkedin_company_id),
                    updated_at = now()
            returning *
            """,
            (nombre, dominio, linkedin, source),
        )

    existente = db.fetch_one(
        "select id from target_accounts where domain is null and lower(name) = lower(%s) "
        "order by created_at limit 1",
        (nombre,),
    )
    if existente:
        return db.fetch_one(
            "update target_accounts set name = %s, "
            "linkedin_company_id = coalesce(%s, linkedin_company_id), updated_at = now() "
            "where id = %s returning *",
            (nombre, linkedin, existente["id"]),
        )
    return db.fetch_one(
        "insert into target_accounts (name, linkedin_company_id, source) "
        "values (%s, %s, %s) returning *",
        (nombre, linkedin, source),
    )


@dataclass
class ResultadoImportacion:
    agregadas: int = 0
    errores: list[str] = field(default_factory=list)


def importar_csv(path: Path | str) -> ResultadoImportacion:
    """Importa un CSV con columnas nombre,dominio[,linkedin_id]. Una fila mala
    se anota y no para el resto."""
    resultado = ResultadoImportacion()
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columnas = {(c or "").strip().lower() for c in reader.fieldnames or []}
        if "nombre" not in columnas:
            raise ValueError(
                "el CSV necesita una columna 'nombre' (y opcionalmente dominio, linkedin_id)"
            )
        for linea, fila in enumerate(reader, start=2):
            fila = {(k or "").strip().lower(): (v or "").strip() for k, v in fila.items()}
            try:
                agregar_cuenta(
                    fila.get("nombre", ""),
                    fila.get("dominio") or None,
                    fila.get("linkedin_id") or None,
                    source="csv",
                )
            except ValueError as exc:
                resultado.errores.append(f"línea {linea}: {exc}")
            else:
                resultado.agregadas += 1
    return resultado


def obtener_cuenta(account_id: str) -> dict | None:
    return db.fetch_one("select * from target_accounts where id = %s", (account_id,))


def listar_cuentas() -> list[dict]:
    """Cuentas con sus vacantes abiertas y la mejor puntuación entre ellas."""
    return db.fetch_all(
        """
        select a.*,
               count(s.id) filter (where s.closed_at is null) as open_roles,
               max(s.score) filter (where s.closed_at is null) as top_score
        from target_accounts a
        left join hiring_signals s on s.account_id = a.id
        group by a.id
        order by a.name
        """
    )


def buscar_cuenta(texto: str) -> dict | None:
    """Una cuenta por dominio (en cualquier forma) o por nombre exacto, sin
    distinguir mayúsculas."""
    texto = (texto or "").strip()
    if not texto:
        return None
    try:
        dominio = normalizar_dominio(texto)
    except ValueError:
        dominio = None
    if dominio:
        cuenta = db.fetch_one("select * from target_accounts where domain = %s", (dominio,))
        if cuenta:
            return cuenta
    return db.fetch_one(
        "select * from target_accounts where lower(name) = lower(%s) "
        "or lower(linkedin_name) = lower(%s) order by created_at limit 1",
        (texto, texto),
    )


def senales_de_cuenta(account_id: str, incluir_cerradas: bool = False) -> list[dict]:
    """Vacantes de una cuenta, las abiertas primero y por puntuación."""
    return db.fetch_all(
        """
        select * from hiring_signals
        where account_id = %s and (%s or closed_at is null)
        order by closed_at is not null, score desc, last_seen_at desc
        """,
        (account_id, incluir_cerradas),
    )
