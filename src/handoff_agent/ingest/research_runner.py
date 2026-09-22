"""Take one job from the queue and research that person.

The Slack profile feeds the research: the real name, a company parsed from the
title and, best of all, a company domain from a work email. The panel can also
hand the agent more than a correction (`panel_ayudar_research`, migration 0008):
a company name override, extra links, and free-text notes, on top of the
domain override `panel_corregir_web` (0007) already offered. Company priority
is override > Slack title > whatever is already stored; domain priority is
unchanged (override > work email > guessed by research/gather.py). Both
overrides are trusted input -- research/gather.py only ever verifies a domain
or a link it had to guess or was handed unvetted. System stops and a dead
token hand the job back untouched and propagate -- they need a person, not a
retry.

`queue.complete`/`fail`/`give_up`/`release` are guarded so they only ever
change the job this worker actually claimed (see queue.py's started_at
check). If `reclaim_stale()` handed the job to a second worker while this one
was still mid-research, those calls come back as a no-op (None/False here).
That must never be reported as "reintento" or "hecho" -- the job is not ours
to report on any more, so the status is "descartado" instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .. import db
from ..delivery import sms
from ..delivery.deliver import deliver_for
from ..research import worker
from ..slack_client import SlackAuthFailed, SlackUnavailable, SlackUserNotFound
from . import queue
from .profile_hints import company_from_title, domain_from_email

logger = logging.getLogger(__name__)

# Estados de ResearchOutcome que dejaron un dossier entregable. "omitido" (bot,
# descartado, dossier aún vigente) no trae una versión nueva que anunciar.
DELIVERABLE_STATUSES = ("investigado", "incompleto")


def _prospect_overrides(slack_user_id: str) -> dict | None:
    """Todo lo que ya vive en `prospects` para esta persona: lo que guardó un
    research anterior (nombre, empresa) y lo que el panel fijó a mano
    (`panel_corregir_web`/`panel_ayudar_research`, migraciones 0007 y 0008).
    Una sola lectura, no cuatro: se pide junto para no ir y volver a la base
    por cada override. None si la persona todavía no existe -- para alguien
    nuevo no hay fila, y por tanto no hay overrides ni nada guardado."""
    return db.fetch_one(
        "select full_name, company_name, company_domain_override, "
        "company_name_override, research_links, research_notes "
        "from prospects where slack_user_id = %s",
        (slack_user_id,),
    )


@dataclass(frozen=True)
class RunResult:
    # limitado | hecho | omitido | reintento | fallido | detenido | descartado
    status: str
    slack_user_id: str | None = None
    detail: str | None = None


def _discarded(job: dict, transition: str, uid: str, detail: str) -> RunResult:
    logger.warning(
        "run_next_job: la tarea %s ya no era nuestra al %s (reclamada por otro worker)",
        job["id"],
        transition,
    )
    return RunResult("descartado", uid, detail)


def _retry_or_fail(job: dict, uid: str, error: str) -> RunResult:
    status = queue.fail(job, error)
    if status is None:
        return _discarded(job, "reintentar", uid, error)
    return RunResult("fallido" if status == "fallido" else "reintento", uid, error)


def run_next_job(reader) -> RunResult | None:
    if queue.finished_last_hour() >= queue.hourly_limit():
        return RunResult("limitado")
    job = queue.claim_next()
    if job is None:
        return None
    uid = job["slack_user_id"]

    try:
        profile = reader.user_profile(uid)
    except SlackAuthFailed:
        queue.release(job)
        raise
    except SlackUserNotFound:
        # Sin perfil de Slack (se fue del workspace, o la persona se creó a
        # mano): se investiga con lo que ya hay guardado, en vez de reintentar
        # algo que nunca va a aparecer. Si no hay nada guardado, research_person
        # levanta ValueError más abajo y el job se da por perdido.
        profile = None
    except SlackUnavailable as exc:
        return _retry_or_fail(job, uid, str(exc))

    if profile is not None and (profile.is_bot or profile.deleted):
        reason = "bot o usuario eliminado"
        if not queue.complete(job, {"estado": "omitido", "motivo": reason}):
            return _discarded(job, "completar", uid, reason)
        return RunResult("omitido", uid, reason)

    overrides = _prospect_overrides(uid)
    if profile is not None:
        full_name = profile.real_name or None
        title_company = company_from_title(profile.title)
        email_domain = domain_from_email(profile.email)
    else:
        full_name = overrides["full_name"] if overrides else None
        title_company = None
        email_domain = None
    # Empresa: lo que alguien fijó a mano en el panel gana siempre -- lo puso
    # después de ver un título de Slack ambiguo o vacío. Sin override, el
    # título manda; sin ninguno de los dos (o sin perfil de Slack), lo que ya
    # había guardado un research anterior.
    company = (
        (overrides["company_name_override"] if overrides else None)
        or title_company
        or (overrides["company_name"] if overrides else None)
    )
    domain = (overrides["company_domain_override"] if overrides else None) or email_domain
    extra_links = tuple((overrides["research_links"] if overrides else None) or ())
    notes = overrides["research_notes"] if overrides else None
    try:
        outcome = worker.research_person(
            full_name,
            company,
            domain,
            slack_user_id=uid,
            # Un job manual ("Investigar más" del panel o de la tarjeta) tiene
            # que forzar aunque el dossier siga vigente -- si no, siempre
            # termina en "omitido: dossier vigente" y el botón no hace nada.
            force=(job["reason"] == "manual"),
            extra_links=extra_links,
            notes=notes,
        )
    except worker.SYSTEM_STOPS:
        queue.release(job)
        raise
    except ValueError as exc:  # ni nombre ni empresa: no hay a quién investigar
        if not queue.give_up(job, str(exc)):
            return _discarded(job, "dar por perdida", uid, str(exc))
        return RunResult("fallido", uid, str(exc))
    except Exception as exc:  # noqa: BLE001 - se reintenta con espera
        return _retry_or_fail(job, uid, f"{type(exc).__name__}: {exc}")

    if not queue.complete(
        job,
        {
            "estado": outcome.status,
            "version": outcome.version,
            "coste_usd": str(outcome.cost_usd),
            "motivo": outcome.reason,
            "empresa": company,
            "dominio": domain,
        },
    ):
        return _discarded(job, "completar", uid, outcome.status)

    # La investigación ya quedó pagada y guardada (complete() ya corrió), así
    # que cualquier fallo de entrega -- Slack caído, Twilio caído, un error de
    # base de datos, un bug -- se registra y se traga: nunca puede convertir
    # un research terminado en un reintento o un fallo. El SMS de la Task 7
    # corre dentro de la misma protección, justo después de la tarjeta, y
    # solo cuando la tarjeta de verdad salió con banda alta.
    if outcome.status in DELIVERABLE_STATUSES and outcome.version:
        try:
            band = deliver_for(outcome.prospect_id, reader)
            if band == "alta":
                sms.maybe_send(outcome.prospect_id, band)
        except Exception:
            # logger.exception ya evita que ruff lo marque como "except" ciego.
            logger.exception("run_next_job: la entrega falló tras completar la tarea %s", job["id"])

    return RunResult("hecho", uid, outcome.status)
