"""Aviso en Slack cuando la credencial de sesión del lector caduca.

El worker ya anota el fallo en `estado_lector` (que muestra el panel) y manda
la alerta operativa (`ops_alerts`), pero nadie se entera si no entra al panel.
Aquí se avisa en el canal de tarjetas de Handoff, con el bot de Handoff: su
token es otro y sigue siendo válido aunque el del lector haya caducado.

Sin spam: como mucho un aviso cada `aviso_token_horas` (6 por defecto). El
último aviso se guarda en el ledger (`agent_actions`, acción
`aviso_credencial_lector`) y no en `config`: es un hecho que ocurrió, no un
ajuste; el ledger es el sitio donde ya se anotan los avisos y envíos, se puede
consultar el historial entero, y `config` queda para lo que el panel edita.

Se anota el intento *antes* de publicar. Así, si publicar falla (o si tras
publicar falla anotar), no se reintenta cada vuelta del bucle: el aviso solo se
repite pasada la ventana. El precio es que un fallo transitorio de Slack pierde
ese aviso durante esas horas; el fallo se registra aparte
(`aviso_credencial_fallido`) y la alerta operativa sigue yendo por su vía.
Si ni siquiera se puede anotar el intento, no se publica: sin ese registro
no habría forma de frenar el spam.

Esto nunca lanza: avisar es un extra y no puede empeorar la situación.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from .. import db, db_config, ledger
from ..config import load_settings
from . import slack_writer
from .card import _is_safe_link

logger = logging.getLogger(__name__)

ACCION_AVISO = "aviso_credencial_lector"
ACCION_FALLIDO = "aviso_credencial_fallido"
MENSAJE = "⚠️ I can no longer read Slack — the reader credential expired. "


def _texto() -> str:
    panel_url = load_settings().panel_url
    enlace = f"{panel_url}/ajustes" if panel_url else ""
    if enlace and _is_safe_link(enlace):
        return MENSAJE + f"Paste a fresh one in the panel → {enlace}"
    return MENSAJE + "Paste a fresh one in the panel."


def _toca_avisar(ahora: datetime) -> bool:
    ultimo = db.fetch_one(
        "select created_at from agent_actions where action = %s order by created_at desc limit 1",
        (ACCION_AVISO,),
    )
    if ultimo is None:
        return True
    horas = db_config.value("aviso_token_horas", 6)
    return ahora - ultimo["created_at"] >= timedelta(hours=horas)


def avisar_credencial_caducada(now: datetime | None = None) -> bool:
    """Publica el aviso si toca. True si salió a Slack. Nunca lanza."""
    ahora = now or datetime.now(UTC)
    try:
        if not _toca_avisar(ahora):
            return False
        ledger.record_action(ACCION_AVISO, {})
    except Exception:
        logger.exception("aviso de credencial: no se pudo comprobar o anotar; no se publica")
        return False

    texto = _texto()
    try:
        slack_writer.post_card(
            [{"type": "section", "text": {"type": "mrkdwn", "text": texto}}], texto
        )
    except Exception as exc:
        logger.exception("aviso de credencial: no se pudo publicar en Slack")
        try:
            ledger.record_action(ACCION_FALLIDO, {"motivo": f"{type(exc).__name__}: {exc}"})
        except Exception:
            logger.exception("aviso de credencial: no se pudo registrar el fallo")
        return False
    return True
