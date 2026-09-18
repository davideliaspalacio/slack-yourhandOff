"""El email diario: lo que no merece interrumpir, pero sí saberse.

Miembros nuevos investigados, señales de banda baja y el gasto del día. Las
bandas alta y media ya fueron a Slack (y, si son muy calientes, a SMS):
repetirlas aquí es ruido, así que solo banda baja aparece como línea propia.
Corre una vez por la mañana (cron de Railway, ver railway.digest.json) y
siempre resume el día anterior en la zona horaria configurada -- a las 9 de
la mañana en Nueva York, "hoy" todavía no tiene nada que contar.

Como con la tarjeta de Slack y el SMS, todo lo que se interpola aquí (nombre,
empresa, resumen del dossier) lo escribió un tercero o un modelo leyendo
páginas de terceros: pasa siempre por `html.escape` antes de entrar en el
HTML del correo.
"""

from __future__ import annotations

import html
import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx

from .. import db, db_config, guards, ledger
from ..config import load_settings
from . import bands
from .deliver import NO_ALERT_STATES

logger = logging.getLogger(__name__)
RESEND_URL = "https://api.resend.com/emails"

# El resumen del dossier es texto libre de un modelo leyendo páginas ajenas:
# se recorta para que una línea no se trague todo el correo.
RESUMEN_CHARS = 300


@dataclass
class DigestData:
    low: list[dict]
    new_members_count: int
    new_member_lines: list[dict]
    spend: Decimal
    rows: list[dict]


def _zone() -> ZoneInfo:
    return ZoneInfo(db_config.value("zona_horaria", "America/New_York"))


def _window(day: date) -> tuple[datetime, datetime]:
    """[inicio, inicio_del_día_siguiente) en la zona configurada, no en UTC.

    Medianoche a medianoche en Nueva York no coincide con medianoche a
    medianoche en UTC, y usar UTC recortaría o repetiría horas del día real
    de Anthony. El intervalo es semiabierto: un dossier guardado justo a la
    medianoche siguiente pertenece al día siguiente, no a este.
    """
    zone = _zone()
    start = datetime(day.year, day.month, day.day, tzinfo=zone)
    return start, start + timedelta(days=1)


def _configured(settings) -> bool:
    return bool(settings.resend_api_key and settings.digest_from and settings.digest_to)


def _rows_in_window(start: datetime, end: datetime) -> list[dict]:
    """Una fila por persona: su dossier más reciente creado en la ventana.

    `distinct on` con `order by ... version desc` se queda con la última
    versión cuando alguien se investigó dos veces el mismo día -- un join
    directo contra research_jobs multiplicaba filas por cada tarea terminada
    de esa persona. "Miembro nuevo" se resuelve aparte, con un exists, para
    que tampoco multiplique: no importa cuántas tareas 'miembro_nuevo' tenga,
    solo si tiene alguna.
    """
    return db.fetch_all(
        """
        select distinct on (d.prospect_id)
            p.full_name, p.company_name, p.slack_user_id, d.content,
            exists (
                select 1 from research_jobs j
                where j.slack_user_id = p.slack_user_id and j.reason = 'miembro_nuevo'
            ) as nuevo_miembro
        from dossiers d
        join prospects p on p.id = d.prospect_id
        where d.created_at >= %s and d.created_at < %s and p.state <> all(%s)
        order by d.prospect_id, d.version desc
        """,
        (start, end, list(NO_ALERT_STATES)),
    )


def _spend(start: datetime, end: datetime) -> Decimal:
    row = db.fetch_one(
        """
        select coalesce(
            (select sum(cost_usd) from llm_calls where created_at >= %s and created_at < %s), 0
        ) + coalesce(
            (select sum(cost_usd) from cost_events where created_at >= %s and created_at < %s), 0
        ) as total
        """,
        (start, end, start, end),
    )
    return Decimal(row["total"])


def _collect(day: date) -> DigestData:
    start, end = _window(day)
    rows = _rows_in_window(start, end)
    low = [r for r in rows if bands.band_for(r["content"]) == "baja"]
    new_members = [r for r in rows if r["nuevo_miembro"]]
    # Corrección de banda: un miembro nuevo en banda alta o media ya tuvo su
    # tarjeta de Slack -- cuenta, pero no se repite con línea propia. Solo se
    # lista si su banda es baja o no tiene banda (aún sin puntuación clara).
    new_member_lines = [r for r in new_members if bands.band_for(r["content"]) in ("baja", None)]
    return DigestData(
        low=low,
        new_members_count=len(new_members),
        new_member_lines=new_member_lines,
        spend=_spend(start, end),
        rows=rows,
    )


def _resumen(content: dict) -> str:
    collapsed = " ".join((content.get("resumen") or "").split())
    return html.escape(collapsed[:RESUMEN_CHARS])


def _line(prefix: str, row: dict) -> str:
    who = html.escape(row["full_name"] or row["slack_user_id"])
    company = f" — {html.escape(row['company_name'])}" if row["company_name"] else ""
    resumen = _resumen(row["content"])
    detail = f" — {resumen}" if resumen else ""
    return f"<li>{prefix}: {who}{company}{detail}</li>"


def _format(day: date, data: DigestData) -> tuple[str, str]:
    parts = [f"<h2>Founders Club — {day.isoformat()}</h2>"]
    parts.append(f"<p>Señales de banda baja: {len(data.low)}</p>")
    if data.low:
        parts.append("<ul>" + "".join(_line("Señal baja", r) for r in data.low) + "</ul>")
    parts.append(f"<p>Miembros nuevos investigados: {data.new_members_count}</p>")
    if data.new_member_lines:
        lines = "".join(_line("Miembro nuevo", r) for r in data.new_member_lines)
        parts.append(f"<ul>{lines}</ul>")
    parts.append(f"<p>Gasto del día: ${data.spend:.4f}</p>")
    subject = f"Founders Club — {len(data.low)} señales bajas, ${data.spend:.2f}"
    return subject, "".join(parts)


def build(day: date) -> tuple[str, str]:
    return _format(day, _collect(day))


def _send_email(subject: str, body: str) -> None:
    settings = load_settings()
    response = httpx.post(
        RESEND_URL,
        headers={"Authorization": f"Bearer {settings.resend_api_key}"},
        json={
            "from": settings.digest_from,
            "to": list(settings.digest_to),
            "subject": subject,
            "html": body,
        },
        timeout=settings.http_timeout_seconds,
    )
    response.raise_for_status()


def _already_sent(day: date) -> bool:
    return (
        db.fetch_one(
            "select id from agent_actions where action = 'digest_enviado' and payload->>'dia' = %s",
            (day.isoformat(),),
        )
        is not None
    )


def _default_day(now: datetime) -> date:
    """El cron corre una vez por la mañana: a esa hora, el día en curso en la
    zona de Anthony casi no tiene nada que contar. El resumen es de ayer."""
    return (now.astimezone(_zone()) - timedelta(days=1)).date()


def send_daily(day: date | None = None, now: datetime | None = None) -> str:
    try:
        guards.check_kill_switch()
        guards.check_monthly_budget()
    except (guards.KillSwitchActive, guards.MonthlyBudgetExceeded) as exc:
        # Igual que sms.maybe_send y deliver.deliver_for: las paradas del
        # sistema cortan el envío sin propagarse hacia el runner.
        logger.warning("digest detenido: %s", exc)
        ledger.record_action("digest_detenido", {"motivo": f"{type(exc).__name__}: {exc}"})
        return "detenido"

    settings = load_settings()
    if not _configured(settings):
        # Mientras falte RESEND_API_KEY, DIGEST_FROM o DIGEST_TO, el digest
        # simplemente no existe: ni intento, ni fallo registrado cada mañana.
        logger.info("digest sin configurar (faltan RESEND_API_KEY/DIGEST_FROM/DIGEST_TO)")
        return "sin_configurar"

    now = now or datetime.now(tz=UTC)
    day = day or _default_day(now)

    if _already_sent(day):
        # Un reintento del cron de Railway no debe mandar el mismo resumen
        # dos veces -- nada de reintentos ciegos sobre una entrega ya hecha.
        logger.info("digest: el resumen de %s ya se envió", day.isoformat())
        return "ya_enviado"

    data = _collect(day)
    if not data.low and not data.new_members_count and not data.spend:
        # Un día con solo bandas alta/media sigue teniendo gasto que contar:
        # solo se calla cuando no hay nada de las tres cosas.
        logger.info("digest: nada que contar el %s", day.isoformat())
        return "nada"

    subject, body = _format(day, data)
    # La marca se escribe antes de enviar, como la reserva de la tarjeta de
    # Slack: si la base falla después de que el email ya salió, un reintento
    # encuentra la marca y no manda un segundo resumen del mismo día.
    marker = db.fetch_one(
        "insert into agent_actions (action, payload) values ('digest_enviado', %s) returning id",
        (json.dumps({"dia": day.isoformat(), "filas": len(data.rows)}),),
    )
    try:
        _send_email(subject, body)
    except Exception as exc:  # noqa: BLE001 - un fallo de Resend no tumba el cron
        # Nunca str(exc) ni la petición: llevan la cabecera Authorization.
        # Del status sí se guarda algo útil (401 no es lo mismo que 500).
        payload = {"motivo": type(exc).__name__}
        if isinstance(exc, httpx.HTTPStatusError):
            payload["status"] = exc.response.status_code
        logger.error("no se pudo enviar el resumen diario (%s)", payload)
        db.execute("delete from agent_actions where id = %s", (marker["id"],))
        ledger.record_action("digest_fallido", payload)
        return "fallido"

    try:
        ledger.record_cost_event(
            "resend_email", settings.price_resend_per_email, "Resumen diario", None
        )
    except Exception:  # noqa: BLE001 - el email ya salió; no se reporta como fallo
        logger.error("digest enviado, pero no se pudo anotar su coste en cost_events")
    return "enviado"
