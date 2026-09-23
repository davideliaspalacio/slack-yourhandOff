"""Command line for running research by hand, before Slack is connected."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from . import guards, ledger, ops_alerts, serialize
from .accounts import radar
from .accounts import repo as cuentas
from .config import load_settings
from .delivery import digest
from .ingest import queue
from .ingest.loop import run_loop
from .ingest.resolver import resolve_pending
from .ingest.watcher import watch_tick
from .research import worker
from .slack_client import FoundersClubReader, SlackAuthFailed
from .tools import prospects, unipile

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
    try:
        outcome = worker.research_person(args.nombre, args.empresa, args.dominio, force=args.forzar)
    except SYSTEM_STOPS as exc:
        print(f"detenido: {exc}")
        return 1
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
        openings = (d.get("contratacion") or {}).get("vacantes_abiertas")
        openings = "—" if openings is None else openings
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
        elif r.outcome and r.outcome.reason:
            lines.append(f"Motivo: {r.outcome.reason}")
        if r.aviso:
            # El dossier no se pudo releer, o se guardó antes de una parada.
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
            # Si la parada llegó en la segunda pasada, el primer dossier ya se guardó.
            saved = getattr(exc, "saved_version", None)
            if saved is not None:
                row.aviso = f"dossier v{saved} guardado antes de la parada"
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
            serialize.jsonable(history["dossier"]), ensure_ascii=False, indent=2, default=str
        )
    )
    return 0


def cmd_digest(args) -> int:
    status = digest.send_daily()
    print(status)
    # Un día sin nada que contar, o un digest ya enviado por un reintento del
    # cron, no es un fallo: solo "fallido" tiene que salir en rojo en Railway.
    return 1 if status == "fallido" else 0


def cmd_web(args) -> int:
    """Sirve el receptor de los botones de la tarjeta.

    `uvicorn` se importa aquí, no arriba del módulo: es una dependencia solo
    de este comando, y el resto de la CLI (research, worker, digest...) no
    debería fallar si algún día falta. El puerto viene de `$PORT` porque
    Railway lo asigna en tiempo de ejecución -- un `startCommand` sin shell de
    por medio (`"handoff web"`, no `uvicorn ... --port $PORT`) no lo expande.

    Escucha en 0.0.0.0 (IPv4): con "::" asyncio marca el socket como solo
    IPv6 y el healthcheck de Railway, que entra por IPv4, nunca recibe
    respuesta.
    """
    import os

    import uvicorn

    uvicorn.run(
        "handoff_agent.web.app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000"))
    )
    return 0


def cmd_costes(args) -> int:
    print(json.dumps(ledger.cost_summary(days=args.dias), indent=2))
    return 0


def _slack_reader(settings):
    """None, con un mensaje claro, si falta configuración de Slack."""
    if not settings.slack_user_token:
        print("falta SLACK_USER_TOKEN en .env (token de usuario xoxp del Founders Club)")
        return None
    if not settings.slack_channel_ids:
        print("falta SLACK_CHANNEL_IDS en .env (IDs de los canales a vigilar, separados por comas)")
        return None
    return FoundersClubReader(settings.slack_user_token)


def cmd_vigilar(args) -> int:
    settings = load_settings()
    reader = _slack_reader(settings)
    if reader is None:
        return 1
    try:
        tick = watch_tick(reader, list(settings.slack_channel_ids), settings.slack_lookback_hours)
    except SlackAuthFailed as exc:
        ops_alerts.alert("slack_auth", str(exc))
        print(f"detenido: {exc}")
        return 1
    resolved = resolve_pending()
    print(
        f"mensajes nuevos: {tick.messages_new} · ignorados: {tick.messages_ignored} · "
        f"miembros nuevos: {tick.members_new}"
    )
    print(
        f"encolados: {resolved.enqueued} · archivados: {resolved.archived} · "
        f"a scoring: {resolved.to_scoring}"
    )
    for error in tick.errors:
        print(f"error: {error}")
    return 0


def cmd_cola(args) -> int:
    print(json.dumps(queue.status_counts(), indent=2))
    for failure in queue.recent_failures():
        print(f"fallido {failure['slack_user_id']}: {failure['last_error']}")
    return 0


def cmd_worker(args) -> int:
    settings = load_settings()
    reader = _slack_reader(settings)
    if reader is None:
        return 1
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # httpx logs every request at INFO with the full URL, and the alert
    # webhook URL is itself a credential -- it must never reach the logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    stop = threading.Event()
    previous_handlers = {
        sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        run_loop(
            reader,
            channels=list(settings.slack_channel_ids),
            lookback_hours=settings.slack_lookback_hours,
            poll_seconds=settings.slack_poll_seconds,
            should_stop=stop.is_set,
            # Dormir sobre el mismo evento que activa la señal. Con time.sleep,
            # un SIGTERM que llega en plena espera (hasta 10 min tras una parada
            # del sistema) no se atiende hasta el final, y Railway lo mata antes.
            sleep=stop.wait,
        )
    finally:
        # Never leave our handlers installed once the loop ends -- a test
        # suite, or anything else run in the same process, runs after this.
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    return 0


def cmd_cuentas_agregar(args) -> int:
    try:
        cuenta = cuentas.agregar_cuenta(args.nombre, args.dominio, args.linkedin_id)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    print(
        f"cuenta: {cuenta['name']} ({cuenta['domain'] or 'sin dominio'})   "
        f"linkedin: {cuenta['linkedin_company_id'] or '—'}   id: {cuenta['id']}"
    )
    return 0


def cmd_cuentas_importar(args) -> int:
    try:
        resultado = cuentas.importar_csv(Path(args.archivo))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    print(f"agregadas: {resultado.agregadas}")
    for error in resultado.errores:
        print(f"error: {error}")
    return 1 if resultado.errores else 0


def _fecha(valor) -> str:
    return f"{valor:%Y-%m-%d %H:%M}" if valor else "nunca"


def cmd_cuentas_listar(args) -> int:
    filas = cuentas.listar_cuentas()
    if not filas:
        print("no hay cuentas; añade una con `handoff cuentas agregar`")
        return 0
    for fila in filas:
        top = "—" if fila["top_score"] is None else fila["top_score"]
        linea = (
            f"{fila['name']} ({fila['domain'] or 'sin dominio'})   {fila['status']}   "
            f"{fila['open_roles']} abiertas   score {top}   "
            f"linkedin: {fila['linkedin_company_id'] or '—'}   "
            f"último escaneo: {_fecha(fila['last_scan_at'])}"
        )
        print(linea)
        if fila["last_scan_error"]:
            print(f"    error: {fila['last_scan_error']}")
    return 0


def _linea_escaneo(resultado: radar.ResultadoEscaneo) -> str:
    linea = (
        f"{resultado.nombre}: {resultado.vistas} vistas, {resultado.nuevas} nuevas, "
        f"{resultado.reabiertas} re-publicadas, {resultado.cerradas} cerradas, "
        f"{resultado.ignoradas} ignoradas, {resultado.auto_pursued} auto-pursued"
    )
    if resultado.error:
        linea += f"\n    error: {resultado.error}"
    return linea


def cmd_radar(args) -> int:
    try:
        if args.cuenta:
            try:
                account_id = str(uuid.UUID(args.cuenta))
            except ValueError:
                print(f"no existe la cuenta {args.cuenta}")
                return 1
            try:
                resultados = [radar.escanear_por_id(account_id)]
            except LookupError as exc:
                print(exc)
                return 1
        else:
            # Desde la terminal, todas las que toquen: el tope por ciclo es
            # para no parar la cola del worker, no para una ejecución a mano.
            resultados = radar.escanear_pendientes(forzar=args.forzar, limite=1_000_000)
    except SYSTEM_STOPS as exc:
        print(f"detenido: {exc}")
        return 1
    if not resultados:
        print("ninguna cuenta que escanear (usa --forzar para escanearlas todas ya)")
        return 0
    for resultado in resultados:
        print(_linea_escaneo(resultado))
    return 1 if any(r.error for r in resultados) else 0


def cmd_unipile_estado(args) -> int:
    settings = load_settings()
    if not (settings.unipile_api_key and settings.unipile_dsn and settings.unipile_account_id):
        print("falta UNIPILE_API_KEY, UNIPILE_DSN o UNIPILE_ACCOUNT_ID en .env")
        return 1
    try:
        sana = unipile.estado_cuenta()
    except unipile.UnipileError as exc:
        print(f"cuenta: sin respuesta ({exc})")
        sana = False
    else:
        print("cuenta: OK" if sana else "cuenta: NO está OK (Unipile queda pausado)")
    uso = unipile.resumen_uso()
    print(f"búsquedas hoy: {uso['busquedas_hoy']}/{uso['tope_busquedas']}")
    print(f"perfiles hoy: {uso['perfiles_hoy']}/{uso['tope_perfiles']}")
    horario = "en horario" if uso["en_horario"] else "fuera de horario"
    print(f"horario: {horario} ({uso['franja']}, {uso['zona']}; ahora {uso['ahora']})")
    print(f"pausado: {'sí' if uso['pausado'] else 'no'}")
    return 0 if sana else 1


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

    watch = sub.add_parser("vigilar", help="lee el Slack una vez y encola research")
    watch.set_defaults(func=cmd_vigilar)

    jobs_cmd = sub.add_parser("cola", help="estado de la cola de research")
    jobs_cmd.set_defaults(func=cmd_cola)

    loop_cmd = sub.add_parser("worker", help="vigila el Slack y procesa la cola sin parar")
    loop_cmd.set_defaults(func=cmd_worker)

    digest_cmd = sub.add_parser("digest", help="envía el resumen diario por email (Resend)")
    digest_cmd.set_defaults(func=cmd_digest)

    web_cmd = sub.add_parser("web", help="sirve el receptor de los botones de la tarjeta (FastAPI)")
    web_cmd.set_defaults(func=cmd_web)

    accounts_cmd = sub.add_parser("cuentas", help="cuentas objetivo que vigila el radar")
    accounts_sub = accounts_cmd.add_subparsers(dest="accion", required=True)
    add_cmd = accounts_sub.add_parser("agregar", help="añade (o actualiza) una cuenta")
    add_cmd.add_argument("nombre")
    add_cmd.add_argument("--dominio")
    add_cmd.add_argument("--linkedin-id", help="id numérico de la empresa en LinkedIn")
    add_cmd.set_defaults(func=cmd_cuentas_agregar)
    import_cmd = accounts_sub.add_parser(
        "importar", help="importa un CSV con columnas nombre,dominio[,linkedin_id]"
    )
    import_cmd.add_argument("archivo")
    import_cmd.set_defaults(func=cmd_cuentas_importar)
    list_cmd = accounts_sub.add_parser("listar", help="cuentas con sus vacantes abiertas")
    list_cmd.set_defaults(func=cmd_cuentas_listar)

    radar_cmd = sub.add_parser("radar", help="escanea las vacantes de las cuentas objetivo")
    radar_cmd.add_argument("--cuenta", metavar="ID", help="solo esta cuenta, le toque o no")
    radar_cmd.add_argument(
        "--forzar", action="store_true", help="todas las vigiladas, aunque se escanearan hace poco"
    )
    radar_cmd.set_defaults(func=cmd_radar)

    unipile_cmd = sub.add_parser("unipile", help="la cuenta de LinkedIn vía Unipile")
    unipile_sub = unipile_cmd.add_subparsers(dest="accion", required=True)
    status_cmd = unipile_sub.add_parser(
        "estado", help="estado de la cuenta, uso de hoy frente a los topes y horario"
    )
    status_cmd.set_defaults(func=cmd_unipile_estado)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
