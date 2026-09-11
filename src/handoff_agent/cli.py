"""Command line for running research by hand, before Slack is connected."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from . import guards, mcp_server
from .research import worker
from .tools import prospects

SYSTEM_STOPS = (guards.KillSwitchActive, guards.MonthlyBudgetExceeded)


@dataclass
class BatchRow:
    nombre: str | None
    empresa: str | None
    dominio: str | None
    outcome: worker.ResearchOutcome | None = None
    error: str | None = None
    aviso: str | None = None
    dossier: dict | None = None


def _money(value: Decimal) -> str:
    return f"${value:.4f}"


def _dossier_for(outcome: worker.ResearchOutcome) -> dict | None:
    history = prospects.historial_prospecto(outcome.slack_user_id)
    stored = history.get("dossier")
    return stored["content"] if stored else None


def cmd_research(args) -> int:
    outcome = worker.research_person(args.nombre, args.empresa, args.dominio, force=args.forzar)
    print(
        f"estado: {outcome.status}   coste: {_money(outcome.cost_usd)}   versión: {outcome.version}"
    )
    if outcome.reason:
        print(f"motivo: {outcome.reason}")
    if outcome.errors:
        print("errores:")
        for error in outcome.errors:
            print(f"  - {error}")
    dossier = _dossier_for(outcome)
    if dossier:
        print(json.dumps(dossier, ensure_ascii=False, indent=2))
    return 0


def _read_rows(path: Path) -> list[BatchRow]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [
            BatchRow(
                nombre=(row.get("nombre") or "").strip() or None,
                empresa=(row.get("empresa") or "").strip() or None,
                dominio=(row.get("dominio") or "").strip() or None,
            )
            for row in csv.DictReader(handle)
        ]


def _degraded(row: BatchRow) -> bool:
    reason = row.outcome.reason if row.outcome else None
    return bool(reason) and reason.startswith(worker.DEGRADED_REASON)


def _write_report(path: Path, rows: list[BatchRow], stopped: str | None) -> None:
    done = [r for r in rows if r.outcome]
    investigated = [r for r in done if r.outcome.status == "investigado"]
    total = sum((r.outcome.cost_usd for r in done), Decimal(0))
    average = total / len(investigated) if investigated else Decimal(0)

    lines = [
        f"# Informe de research — {datetime.now(UTC):%Y-%m-%d %H:%M} UTC",
        "",
        f"- Investigados: {len(investigated)}",
        f"- Incompletos: {sum(1 for r in done if r.outcome.status == 'incompleto')}",
        f"- Omitidos: {sum(1 for r in done if r.outcome.status == 'omitido')}",
        f"- Degradados: {sum(1 for r in rows if _degraded(r))}",
        f"- Con error: {sum(1 for r in rows if r.error)}",
        f"- Con aviso: {sum(1 for r in rows if r.aviso)}",
        f"- Coste total: {_money(total)}",
        f"- Coste medio por dossier: {_money(average)}",
    ]
    if stopped:
        lines += ["", f"**Lote detenido:** {stopped}"]

    lines += [
        "",
        "| Persona | Empresa | Estado | Coste | Encaje | Vacantes | Fuentes |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        d = r.dossier or {}
        status = r.outcome.status if r.outcome else ("error" if r.error else "sin procesar")
        if _degraded(r):
            status += " (búsqueda degradada)"
        cost = _money(r.outcome.cost_usd) if r.outcome else "—"
        fit = (d.get("encaje_handoff") or {}).get("puntuacion", "—")
        openings = (d.get("contratacion") or {}).get("vacantes_abiertas", "—")
        sources = len((d.get("empresa") or {}).get("fuentes") or [])
        lines.append(
            f"| {r.nombre or '—'} | {r.empresa or '—'} | {status} | {cost} | {fit} | {openings} | {sources} |"
        )

    lines += ["", "## Detalle"]
    for r in rows:
        lines += ["", f"### {r.nombre or '—'} — {r.empresa or '—'}"]
        if _degraded(r):
            lines += [f"**Búsqueda degradada:** {r.outcome.reason}", ""]
        if r.error:
            lines.append(f"Error: {r.error}")
        elif r.dossier:
            lines.append(r.dossier.get("resumen", ""))
            gaps = r.dossier.get("huecos") or []
            if gaps:
                lines.append("")
                lines.append("Huecos: " + "; ".join(map(str, gaps)))
            if r.aviso:
                lines.append("")
                lines.append(f"Aviso: {r.aviso}")
        elif r.outcome and r.outcome.reason:
            lines.append(f"Motivo: {r.outcome.reason}")
        if r.aviso and not r.dossier:
            lines.append(f"Aviso: {r.aviso}")
        if r.outcome and r.outcome.errors:
            lines += ["", "Errores: " + "; ".join(r.outcome.errors)]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_batch(args) -> int:
    rows = _read_rows(Path(args.archivo))
    stopped = None
    for index, row in enumerate(rows):
        try:
            row.outcome = worker.research_person(row.nombre, row.empresa, row.dominio)
        except SYSTEM_STOPS as exc:
            stopped = str(exc)
            row.error = str(exc)
            break
        except Exception as exc:  # noqa: BLE001 - una fila no tumba el lote
            row.error = f"{type(exc).__name__}: {exc}"

        if row.outcome:
            try:
                row.dossier = _dossier_for(row.outcome)
            except Exception as exc:  # noqa: BLE001
                row.aviso = f"dossier guardado pero no se pudo leer: {type(exc).__name__}: {exc}"

        status_msg = row.outcome.status if row.outcome else row.error
        console_line = f"{row.nombre or '—'} / {row.empresa or '—'}: {status_msg}"
        if row.aviso:
            console_line += f" — aviso: {row.aviso}"
        print(console_line)
        if index < len(rows) - 1:
            # Una ráfaga de búsquedas es lo que hace saltar el CAPTCHA de los motores.
            time.sleep(args.pausa)

    _write_report(Path(args.salida), rows, stopped)
    print(f"informe: {args.salida}")
    return 1 if stopped else 0


def cmd_dossier(args) -> int:
    history = prospects.historial_prospecto(args.slack_user_id)
    print(
        json.dumps(
            mcp_server.jsonable(history["dossier"]), ensure_ascii=False, indent=2, default=str
        )
    )
    return 0


def cmd_costes(args) -> int:
    print(json.dumps(mcp_server.resumen_costes(days=args.dias), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="handoff")
    sub = parser.add_subparsers(dest="command", required=True)

    one = sub.add_parser("research", help="investiga a una persona")
    one.add_argument("nombre", nargs="?")
    one.add_argument("--empresa")
    one.add_argument("--dominio")
    one.add_argument("--forzar", action="store_true", help="aunque haya un dossier vigente")
    one.set_defaults(func=cmd_research)

    batch = sub.add_parser("research-batch", help="investiga un CSV y genera un informe")
    batch.add_argument("archivo")
    batch.add_argument("--salida", required=True)
    batch.add_argument(
        "--pausa",
        type=float,
        default=15.0,
        metavar="SEGUNDOS",
        help="espera entre filas para no disparar el CAPTCHA de los buscadores",
    )
    batch.set_defaults(func=cmd_batch)

    show = sub.add_parser("dossier", help="muestra el último dossier de una persona")
    show.add_argument("slack_user_id")
    show.set_defaults(func=cmd_dossier)

    costs = sub.add_parser("costes", help="gasto de los últimos N días")
    costs.add_argument("--dias", type=int, default=30)
    costs.set_defaults(func=cmd_costes)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
