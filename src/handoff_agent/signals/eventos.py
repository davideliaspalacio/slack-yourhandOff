"""Dos avisos de canal que no cuestan una llamada al modelo (migración 0030).

- **Mención**: alguien escribió el id de la cuenta lectora en un mensaje. Es una
  búsqueda de cadena sobre lo que el watcher ya guardó.
- **Presentación**: un mensaje nuevo en el canal de presentaciones. Lo que se publica
  ahí es, por definición, gente presentándose.

Por eso están aquí y no en `signals/procesar.py`: ahí cada fila es el resultado de
preguntarle al modelo, con su coste y su confianza. Estos dos no hay nada que
interpretar, así que son gratis y salen en la misma vuelta.

Lo que sí comparten con las alertas de `signals/alertas.py`:

- Se reserva antes de publicar, para que un segundo worker o un reintento no dupliquen.
- Tope diario por tipo, contado en `zona_horaria`.
- Nunca a un descartado, y nunca sobre un mensaje del propio dueño de la credencial (el
  watcher ya los marca `ignorado` y la consulta los salta igual).
- Nada de esto puede propagarse: avisar es un extra y no puede tumbar el bucle.

El texto del mensaje se usa para decidir y para citarlo en el aviso, y se cita como
código en línea igual que en las tarjetas (`delivery/card.py`), para que Slack no
convierta en enlace ni en mención nada de lo que alguien escribió.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .. import db, db_config, ledger
from ..config import load_settings
from ..delivery import card, slack_writer
from ..slack_client import SlackUnavailable

logger = logging.getLogger(__name__)

MENCION = "mencion"
PRESENTACION = "presentacion"

ACCION_PUBLICADO = "evento_publicado"
ACCION_FALLIDO = "evento_fallido"

CABECERAS = {
    MENCION: "💬 *YOU WERE MENTIONED*",
    PRESENTACION: "👋 *NEW INTRODUCTION*",
}


def _zona() -> ZoneInfo:
    return ZoneInfo(db_config.value("zona_horaria", "America/New_York"))


def _inicio_del_dia(ahora: datetime) -> datetime:
    return ahora.astimezone(_zona()).replace(hour=0, minute=0, second=0, microsecond=0)


def _tope(tipo: str) -> int:
    clave = "menciones_por_dia" if tipo == MENCION else "presentaciones_por_dia"
    defecto = 50 if tipo == MENCION else 20
    return db_config.value(clave, defecto)


def _publicados_hoy(tipo: str, ahora: datetime) -> int:
    fila = db.fetch_one(
        "select count(*) as n from eventos_canal "
        "where tipo = %s and estado = 'publicada' and created_at >= %s",
        (tipo, _inicio_del_dia(ahora)),
    )
    return fila["n"] if fila else 0


def detectar(owner_id: str) -> int:
    """Apunta los eventos de los mensajes que todavía no se habían mirado.

    No publica: solo deja las filas en `pendiente`. Devuelve cuántas creó.

    Los mensajes del dueño quedan fuera por dos vías: el watcher los marca `ignorado`
    y además se exige que el autor no sea él -- mencionarse a sí mismo o presentarse
    en su propio club no es un aviso.
    """
    canal_presentaciones = str(db_config.value("canal_presentaciones", "") or "").strip().upper()
    creados = 0

    # Mención: su id aparece como `<@U...>` en el texto, que es como Slack lo guarda.
    creados += db.execute(
        """
        insert into eventos_canal (channel_id, ts, tipo, slack_user_id, prospect_id)
        select sm.channel_id, sm.ts, %(tipo)s, sm.user_id, p.id
          from slack_messages sm
          left join prospects p on p.slack_user_id = sm.user_id
         where sm.status <> 'ignorado'
           and sm.user_id is not null
           and sm.user_id <> %(dueno)s
           and position(%(aguja)s in coalesce(sm.text, '')) > 0
           and (p.state is null or p.state <> 'descartado')
        on conflict (channel_id, ts, tipo) do nothing
        """,
        {"tipo": MENCION, "dueno": owner_id, "aguja": f"<@{owner_id}>"},
    )

    # Presentación: cualquier mensaje de persona en el canal de presentaciones.
    if canal_presentaciones:
        creados += db.execute(
            """
            insert into eventos_canal (channel_id, ts, tipo, slack_user_id, prospect_id)
            select sm.channel_id, sm.ts, %(tipo)s, sm.user_id, p.id
              from slack_messages sm
              left join prospects p on p.slack_user_id = sm.user_id
             where sm.status <> 'ignorado'
               and sm.user_id is not null
               and sm.user_id <> %(dueno)s
               and upper(sm.channel_id) = %(canal)s
               and btrim(coalesce(sm.text, '')) <> ''
               and (p.state is null or p.state <> 'descartado')
            on conflict (channel_id, ts, tipo) do nothing
            """,
            {"tipo": PRESENTACION, "dueno": owner_id, "canal": canal_presentaciones},
        )
    return creados


def _pendientes(tipo: str, limite: int) -> list[dict]:
    return db.fetch_all(
        """
        select e.id::text as id, e.channel_id, e.ts, e.tipo, e.slack_user_id,
               sm.text, c.nombre as canal_nombre,
               p.full_name, p.company_name, p.company_name_override
          from eventos_canal e
          join slack_messages sm on sm.channel_id = e.channel_id and sm.ts = e.ts
          left join canales c on c.channel_id = e.channel_id
          left join prospects p on p.id = e.prospect_id
         where e.tipo = %s and e.estado = 'pendiente'
         order by e.ts::numeric
         limit %s
        """,
        (tipo, limite),
    )


def _quien(fila: dict) -> str:
    nombre = (fila.get("full_name") or "").strip() or fila.get("slack_user_id") or "Someone"
    empresa = (fila.get("company_name_override") or fila.get("company_name") or "").strip()
    return f"*{nombre}* — {empresa}" if empresa else f"*{nombre}*"


def _texto_aviso(fila: dict, reader) -> str:
    from .. import canales

    partes = [CABECERAS[fila["tipo"]], "", _quien(fila)]
    canal = canales.etiqueta(fila["channel_id"], fila.get("canal_nombre"))
    partes.append(f"in {canal}")
    cita = (fila.get("text") or "").strip()
    if cita:
        # El mismo renderizado que las tarjetas: dentro de un código en línea, para que
        # Slack no autoenlace una URL que alguien haya metido en su mensaje.
        partes.append(card.quote(cita))
    enlace = None
    try:
        enlace = reader.permalink(fila["channel_id"], fila["ts"])
    except SlackUnavailable:
        # El aviso vale sin enlace: lo importante es que AG sepa que pasó.
        logger.warning("evento %s: sin enlace permanente", fila["id"])
    if enlace:
        partes.append(f"<{enlace}|Open in Slack>")
    return "\n".join(partes)


def _reservar(evento_id: str) -> bool:
    """Marca la fila como publicada antes de publicar, para no duplicar."""
    return bool(
        db.execute(
            "update eventos_canal set estado = 'publicada' where id = %s and estado = 'pendiente'",
            (evento_id,),
        )
    )


def _liberar(evento_id: str, motivo: str) -> None:
    db.execute(
        "update eventos_canal set estado = 'pendiente', motivo = %s where id = %s",
        (motivo, evento_id),
    )


def _publicar_tipo(tipo: str, reader, ahora: datetime) -> int:
    cupo = _tope(tipo) - _publicados_hoy(tipo, ahora)
    if cupo <= 0:
        return 0
    publicados = 0
    for fila in _pendientes(tipo, cupo):
        if not _reservar(fila["id"]):
            continue
        texto = _texto_aviso(fila, reader)
        try:
            _, ts = slack_writer.post_card(
                [{"type": "section", "text": {"type": "mrkdwn", "text": texto}}], texto
            )
        except Exception as exc:
            logger.exception("evento %s: no se pudo publicar", fila["id"])
            _liberar(fila["id"], f"{type(exc).__name__}: {exc}")
            ledger.record_action(ACCION_FALLIDO, {"tipo": tipo, "motivo": type(exc).__name__})
            continue
        db.execute("update eventos_canal set aviso_ts = %s where id = %s", (ts, fila["id"]))
        ledger.record_action(ACCION_PUBLICADO, {"tipo": tipo, "canal": fila["channel_id"]})
        publicados += 1
    return publicados


def publicar_pendientes(reader, now: datetime | None = None) -> int:
    """Detecta y publica los eventos de canal. Devuelve cuántos avisos salieron.
    Nunca lanza: avisar es un extra y no puede tumbar el bucle."""
    ahora = now or datetime.now(UTC)
    try:
        settings = load_settings()
        if not (settings.handoff_bot_token and settings.handoff_channel_id):
            return 0
        detectar(reader.owner_id())
        return sum(_publicar_tipo(tipo, reader, ahora) for tipo in (MENCION, PRESENTACION))
    except Exception:
        logger.exception("eventos de canal: fallo; se reintenta en la vuelta siguiente")
        return 0
