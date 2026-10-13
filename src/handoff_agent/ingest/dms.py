"""El rastro de los mensajes directos del dueño de la credencial.

Para qué: Anthony escribió a un lead por DM, el lead contestó, y se le pasó
responder. El agente no puede contestar por él -- nunca escribe en ese
workspace -- pero sí puede llevar la cuenta de qué conversaciones están
esperando y avisarle (`delivery/seguimiento.py`).

Qué se guarda, y nada más (tabla `dm_rastro`, migración 0027): el id del DM, el
workspace (para el enlace), con quién es, la marca de tiempo del último mensaje
y si lo escribió él o la otra persona. **El texto de un DM no se guarda en
ninguna parte**: de cada conversación se lee un solo mensaje (`last_message`,
`limit=1`), se mira `ts` y `user`, y el resto se descarta aquí mismo.

Qué queda fuera, por diseño y no por casualidad:

- Los grupos (`mpim`) y los canales: se piden solo `types=im` y además se
  exige el prefijo `D` del id, que el CHECK de la tabla repite.
- Los bots y las apps: el primer rastro de una conversación comprueba el perfil
  de la otra persona (`users.info`) y, si es un bot, no se guarda. Después ya no
  hace falta volver a preguntar: la fila existe porque pasó esa comprobación.
- La conversación consigo mismo (el DM del dueño con el dueño).
- Lo viejo: una conversación cuyo último mensaje pase de
  `seguimiento_retencion_dias` (90) no se guarda, y si estaba guardada se
  borra (`limpiar_retencion`).

Un fallo de Slack en una conversación no tumba las demás: se anota en
`ResultadoRastro.errores` y se sigue. El bucle del worker (`ingest/loop.py`) lo
llama cada `MINUTOS_ENTRE_PASADAS`, no en cada vuelta: un DM que se enfría no
tiene prisa, y cada pasada son dos llamadas a Slack por conversación como mucho.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .. import db, db_config
from ..slack_client import SlackUnavailable

logger = logging.getLogger(__name__)

# Cada cuánto le toca una pasada al rastro (lo usa ingest/loop.py).
MINUTOS_ENTRE_PASADAS = 15
# Tope de conversaciones por pasada: la lista de DMs de alguien con 1.400
# compañeros puede ser muy larga, y una pasada no puede dejar la cola de
# research parada hablando con Slack. Las que no entran, en la siguiente.
LIMITE_POR_PASADA = 200
RETENCION_DEFECTO = 90
# El DM de Slackbot: ni es una persona ni hay nada que responderle.
SLACKBOT = "USLACKBOT"
# Quién escribió el último mensaje, tal como lo guarda `dm_rastro.ultimo_de`.
DUENO = "dueno"
OTRO = "otro"


@dataclass
class ResultadoRastro:
    vistos: int = 0
    guardados: int = 0
    omitidos: int = 0
    borrados: int = 0
    errores: list[str] = field(default_factory=list)


def _activo() -> bool:
    return db_config.value("seguimiento_activo", True) is True


def _limite_retencion(ahora: datetime) -> float:
    dias = db_config.value("seguimiento_retencion_dias", RETENCION_DEFECTO)
    return ahora.timestamp() - dias * 86400


def limpiar_retencion(ahora: datetime | None = None) -> int:
    """Borra el rastro de las conversaciones cuyo último mensaje es demasiado
    viejo. Devuelve cuántas filas se fueron."""
    limite = _limite_retencion(ahora or datetime.now(UTC))
    return db.execute("delete from dm_rastro where ultimo_ts::numeric < %s", (limite,))


def _conocidos() -> set[str]:
    """Los DMs que ya están en el rastro: su perfil ya se comprobó una vez."""
    return {f["dm_id"] for f in db.fetch_all("select dm_id from dm_rastro")}


def _es_uno_a_uno(conversacion: dict, owner_id: str) -> bool:
    """True solo si es un DM de dos personas con alguien que no es el dueño.

    Se exige `is_im`, que no sea `is_mpim`, el prefijo D del id y que haya una
    `user`. Slack ya filtra por `types=im`, pero esto no depende de que lo
    haga: es la misma regla que impone el CHECK de la tabla."""
    dm_id = conversacion.get("id") or ""
    usuario = conversacion.get("user") or ""
    return bool(
        dm_id.startswith("D")
        and conversacion.get("is_im", True)
        and not conversacion.get("is_mpim")
        and usuario
        and usuario != owner_id
        and usuario != SLACKBOT
        and not conversacion.get("is_user_deleted")
    )


def _es_bot(reader, slack_user_id: str) -> bool:
    """Si la otra parte es un bot o una app, esta conversación no se rastrea.

    Solo se pregunta la primera vez que se ve un DM (ver `_conocidos`). Si
    Slack no contesta, se trata como bot: mejor no guardar un rastro que
    guardar el de una app."""
    try:
        return reader.user_profile(slack_user_id).is_bot
    except SlackUnavailable as exc:
        logger.info("rastro: sin perfil de %s (%s); no se guarda", slack_user_id, exc)
        return True


def _quien_escribio(mensaje: dict, owner_id: str) -> str | None:
    """'dueno', 'otro', o None si no se puede saber (un mensaje de sistema).

    Lo único que se mira del mensaje es el autor; el texto no se toca."""
    autor = mensaje.get("user")
    if mensaje.get("bot_id") and autor != owner_id:
        # Un recordatorio de Slackbot o la respuesta de una app en el DM: no
        # es la otra persona contestando, pero tampoco es él.
        return None
    if not autor:
        return None
    return DUENO if autor == owner_id else OTRO


def _guardar(dm_id: str, team_id: str, slack_user_id: str, ts: str, de: str) -> None:
    """Upsert del rastro. Un `ultimo_ts` distinto reinicia el aviso: la
    conversación cambió, así que el caso vuelve a estar vivo."""
    db.execute(
        """
        insert into dm_rastro (dm_id, team_id, slack_user_id, prospect_id, ultimo_ts, ultimo_de)
        values (
            %(dm_id)s, nullif(%(team_id)s, ''), %(slack_user_id)s,
            (select id from prospects where slack_user_id = %(slack_user_id)s),
            %(ts)s, %(de)s
        )
        on conflict (dm_id) do update set
            team_id = coalesce(nullif(%(team_id)s, ''), dm_rastro.team_id),
            slack_user_id = excluded.slack_user_id,
            -- Quien ya estaba enlazado no se desenlaza; quien aún no era
            -- prospect se enlaza en cuanto lo es.
            prospect_id = coalesce(excluded.prospect_id, dm_rastro.prospect_id),
            ultimo_ts = excluded.ultimo_ts,
            ultimo_de = excluded.ultimo_de,
            avisado_ts = case
                when dm_rastro.ultimo_ts = excluded.ultimo_ts then dm_rastro.avisado_ts
                else null
            end,
            actualizado_at = now()
        """,
        {
            "dm_id": dm_id,
            "team_id": team_id,
            "slack_user_id": slack_user_id,
            "ts": ts,
            "de": de,
        },
    )


def rastro_tick(reader, now: datetime | None = None) -> ResultadoRastro:
    """Una pasada por los DMs del dueño de la credencial.

    Lanza `SlackAuthFailed` (la credencial necesita a una persona, y de eso ya
    se encarga el bucle). Cualquier otro fallo de Slack en una conversación
    queda en `errores` y las demás siguen."""
    resultado = ResultadoRastro()
    if not _activo():
        logger.info("rastro de DMs desactivado (seguimiento_activo)")
        return resultado

    ahora = now or datetime.now(UTC)
    owner_id = reader.owner_id()
    try:
        team_id = reader.team_id()
    except SlackUnavailable as exc:
        # Sin el código del workspace el rastro se guarda igual; lo único que
        # falta es el enlace para abrir la conversación.
        team_id = ""
        resultado.errores.append(f"team_id: {exc}")

    try:
        conversaciones = reader.dms()
    except SlackUnavailable as exc:
        resultado.errores.append(f"conversations_list: {exc}")
        return resultado

    conocidos = _conocidos()
    limite = _limite_retencion(ahora)
    for conversacion in sorted(conversaciones, key=lambda c: c.get("id") or "")[:LIMITE_POR_PASADA]:
        resultado.vistos += 1
        dm_id = conversacion.get("id") or ""
        slack_user_id = conversacion.get("user") or ""
        if not _es_uno_a_uno(conversacion, owner_id):
            resultado.omitidos += 1
            continue
        if dm_id not in conocidos and _es_bot(reader, slack_user_id):
            resultado.omitidos += 1
            continue
        try:
            mensaje = reader.last_message(dm_id)
        except SlackUnavailable as exc:
            resultado.errores.append(f"{dm_id}: {exc}")
            continue
        if not mensaje or not mensaje.get("ts"):
            resultado.omitidos += 1
            continue
        de = _quien_escribio(mensaje, owner_id)
        if de is None:
            resultado.omitidos += 1
            continue
        try:
            if float(mensaje["ts"]) < limite:
                # Conversación apagada hace meses: no entra en el rastro.
                resultado.omitidos += 1
                continue
        except (TypeError, ValueError):
            resultado.omitidos += 1
            continue
        _guardar(dm_id, team_id, slack_user_id, str(mensaje["ts"]), de)
        resultado.guardados += 1

    resultado.borrados = limpiar_retencion(ahora)
    return resultado
