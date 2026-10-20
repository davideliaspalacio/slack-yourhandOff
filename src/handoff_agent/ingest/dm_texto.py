"""El texto de los mensajes directos de Anthony con sus leads (migración 0031).

Para qué: con el texto de la conversación, el agente le puede sugerir qué
contestar (`delivery/sugerencia.py`, la otra mitad). El rastro de siempre
(`ingest/dms.py`) solo sabe quién habló último y cuándo; esto es lo que dijo.

Anthony autorizó leerlo, y se lee lo justo:

- **Detrás de un interruptor apagado** (`dm_texto_activo`). Apagado no se le
  pide a Slack ni un mensaje, y cada pasada con el interruptor apagado borra lo
  que haya guardado: texto, sugerencias y el estado de lo no leído. Apagar
  significa olvidar, no solo dejar de mirar.
- **Solo leads.** Una conversación se abre solo si su fila de `dm_rastro` está
  enlazada a una ficha de `prospects` (pasó por el research), sin descartar y
  con tarjeta. Las conversaciones personales de Anthony no se abren nunca, y si
  alguien se descarta después, su texto se borra en la pasada siguiente.
- **Solo lo reciente.** `dm_texto_retencion_dias` (10): no se trae nada más
  viejo y lo que envejece se borra.
- **Sin un método de Slack nuevo.** El historial es `conversations.history` y
  lo no leído sale de `conversations.info`, los dos ya en la lista blanca del
  lector. `conversations.mark` sigue prohibido: leer el texto no le cambia a
  Anthony lo que tiene sin leer.

`dm_mensajes` no tiene permisos para el panel ni para la API: solo lo lee el
worker, con `conversacion()`.

Un fallo de Slack en una conversación queda en `Resultado.errores` y las demás
siguen. `SlackAuthFailed` sube: la credencial necesita a una persona, y de eso
se encarga el bucle (`ingest/loop.py`), que llama aquí justo después del rastro,
con su mismo ritmo de 15 minutos.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .. import db, db_config
from ..slack_client import SlackUnavailable
from .dms import _activo as _seguimiento_activo
from .dms import _quien_escribio
from .watcher import SYSTEM_SUBTYPES

logger = logging.getLogger(__name__)

RETENCION_DEFECTO = 10
# Tope de conversaciones por pasada. Cada una cuesta dos llamadas a Slack
# (historial y estado de lectura), y leads con un DM de los últimos diez días
# hay pocos: el tope está para que un caso raro no deje la cola de research
# parada hablando con Slack. Se eligen las más recientes primero, que son las
# que pueden necesitar una respuesta; las que no entran, en la siguiente.
LIMITE_POR_PASADA = 50
CONVERSACION_DEFECTO = 30


@dataclass
class Resultado:
    conversaciones: int = 0
    guardados: int = 0
    omitidos: int = 0
    purgados: int = 0
    # Filas borradas porque el interruptor estaba apagado.
    borrados: int = 0
    errores: list[str] = field(default_factory=list)


def activo() -> bool:
    return db_config.value("dm_texto_activo", False) is True


def _limite_retencion(ahora: datetime) -> float:
    dias = db_config.value("dm_texto_retencion_dias", RETENCION_DEFECTO)
    return ahora.timestamp() - dias * 86400


def borrar_todo() -> int:
    """Borra todo el texto y todas las sugerencias. Devuelve cuántas filas se
    fueron (las de las dos tablas).

    También vacía `sin_leer` y `leido_hasta`: solo se refrescan con el
    interruptor encendido, y un contador congelado diría que Anthony tiene
    mensajes sin leer que a lo mejor ya leyó. Las dos tablas van en una sola
    transacción para que nunca quede una sugerencia sin el texto del que salió."""
    with db.transaction() as cur:
        cur.execute("delete from dm_mensajes")
        borrados = cur.rowcount
        cur.execute("delete from dm_sugerencias")
        borrados += cur.rowcount
        cur.execute(
            "update dm_rastro set sin_leer = null, leido_hasta = null "
            "where sin_leer is not null or leido_hasta is not null"
        )
    return borrados


def _purgar(limite: float) -> int:
    """Borra lo que ya no se puede guardar: lo viejo, y lo de conversaciones que
    han dejado de ser de un lead (descartado, sin tarjeta o desenlazado).

    Las sugerencias corren la misma suerte que el texto del que salieron. Sin esto,
    un lead descartado seguía enseñando su sugerencia en el panel y en Claude, y una
    sugerencia sobrevivía a la retención del texto que la produjo: la línea de
    contexto resume ese texto, así que caduca con él.

    `ts` es texto (así lo da Slack); se compara como número, igual que el
    rastro con `ultimo_ts`."""
    no_es_lead = """
        not exists (
            select 1
            from dm_rastro r
            join prospects p on p.id = r.prospect_id
            where r.dm_id = {tabla}.dm_id
              and p.state <> 'descartado'
              and not p.sin_tarjeta
        )
    """
    with db.transaction() as cur:
        cur.execute(
            f"delete from dm_mensajes m where m.ts::numeric < %s or {no_es_lead.format(tabla='m')}",
            (limite,),
        )
        borrados = cur.rowcount
        cur.execute(
            "delete from dm_sugerencias s where s.ultimo_ts::numeric < %s or "
            f"{no_es_lead.format(tabla='s')}",
            (limite,),
        )
        borrados += cur.rowcount
    return borrados


def _candidatas(limite: float) -> list[dict]:
    """Las conversaciones con leads que han tenido movimiento dentro de la
    retención, con el `ts` del último mensaje ya guardado de cada una.

    El `join` con `prospects` es interno a propósito: una conversación sin ficha
    (alguien a quien nunca se investigó) no sale de aquí y nunca se abre."""
    return db.fetch_all(
        """
        select r.dm_id,
               (select m.ts from dm_mensajes m
                where m.dm_id = r.dm_id
                order by m.ts::numeric desc
                limit 1) as ultimo_guardado
        from dm_rastro r
        join prospects p on p.id = r.prospect_id
        where p.state <> 'descartado'
          and not p.sin_tarjeta
          and r.ultimo_ts::numeric >= %s
        order by r.ultimo_ts::numeric desc
        limit %s
        """,
        (limite, LIMITE_POR_PASADA),
    )


def _de(mensaje: dict, owner_id: str) -> str | None:
    """'dueno', 'otro', o None si el mensaje no se guarda.

    Quién lo escribió sale de la misma regla que el rastro
    (`dms._quien_escribio`), para que la conversación guardada y el "quién habló
    último" nunca se contradigan. Encima, lo que el watcher ya descarta en los
    canales (`SYSTEM_SUBTYPES`: uniones, mensajes borrados, de bots...) y lo que
    no tiene texto (un archivo suelto): no hay nada que sugerir sobre eso."""
    if mensaje.get("subtype") in SYSTEM_SUBTYPES:
        return None
    if not (mensaje.get("text") or "").strip():
        return None
    return _quien_escribio(mensaje, owner_id)


def _guardar(dm_id: str, mensaje: dict, de: str) -> int:
    """Un mensaje, una fila. `on conflict do nothing`: si dos pasadas se solapan
    o Slack repite un mensaje, el que ya estaba se queda como estaba."""
    return db.execute(
        "insert into dm_mensajes (dm_id, ts, de, texto) values (%s, %s, %s, %s) "
        "on conflict (dm_id, ts) do nothing",
        (dm_id, str(mensaje["ts"]), de, mensaje["text"]),
    )


def _traer(reader, dm_id: str, oldest: str, owner_id: str, limite: float, resultado) -> None:
    mensajes = reader.history(dm_id, oldest=oldest)
    for mensaje in mensajes:
        try:
            viejo = float(mensaje.get("ts")) < limite
        except (TypeError, ValueError):
            resultado.omitidos += 1
            continue
        de = _de(mensaje, owner_id)
        if de is None or viejo:
            resultado.omitidos += 1
            continue
        resultado.guardados += _guardar(dm_id, mensaje, de)


def _refrescar_estado(reader, dm_id: str) -> None:
    """Lo no leído, tal como lo ve Anthony en su Slack. Es de lectura: preguntar
    no lo cambia."""
    estado = reader.dm_estado(dm_id)
    db.execute(
        "update dm_rastro set sin_leer = %s, leido_hasta = %s where dm_id = %s",
        (estado["sin_leer"], estado["leido_hasta"], dm_id),
    )


def pasada(reader, now: datetime | None = None) -> Resultado:
    """Una pasada: trae lo nuevo de cada conversación con un lead, refresca lo no
    leído y borra lo que ya no se puede guardar.

    Lanza `SlackAuthFailed`. Cualquier otro fallo de Slack en una conversación
    queda en `errores` y las demás siguen."""
    resultado = Resultado()
    if not activo():
        resultado.borrados = borrar_todo()
        return resultado

    ahora = now or datetime.now(UTC)
    limite = _limite_retencion(ahora)
    if not _seguimiento_activo():
        # El panel promete que con el seguimiento apagado el agente deja de leer
        # los DMs del todo: no se le pide nada a Slack. Lo ya guardado se queda
        # hasta que caduque, porque esto no es apagar el texto.
        logger.info("texto de DMs en pausa: el seguimiento está apagado")
        resultado.purgados = _purgar(limite)
        return resultado

    candidatas = _candidatas(limite)
    if candidatas:
        # Solo se pregunta quién es el dueño si hay algo que leer: sin leads con
        # movimiento, la pasada no le habla a Slack.
        owner_id = reader.owner_id()
    for candidata in candidatas:
        resultado.conversaciones += 1
        dm_id = candidata["dm_id"]
        # Después del último ya guardado, o desde el borde de la retención si
        # todavía no hay nada. Slack trata `oldest` como exclusivo: el último
        # guardado no vuelve.
        oldest = candidata["ultimo_guardado"] or f"{limite:.6f}"
        try:
            _traer(reader, dm_id, oldest, owner_id, limite, resultado)
            _refrescar_estado(reader, dm_id)
        except SlackUnavailable as exc:
            resultado.errores.append(f"{dm_id}: {exc}")
            continue

    resultado.purgados = _purgar(limite)
    return resultado


def conversacion(dm_id: str, limite: int = CONVERSACION_DEFECTO) -> list[dict]:
    """Los últimos `limite` mensajes de una conversación, del más viejo al más
    nuevo, con `ts`, `de` y `texto`. Solo lee.

    Con el interruptor apagado devuelve una lista vacía aunque aún quede texto:
    el borrado llega en la pasada siguiente, pero desde el momento en que se
    apaga nadie debería poder usar lo guardado."""
    if not activo():
        return []
    return db.fetch_all(
        """
        select ts, de, texto from (
            select ts, de, texto from dm_mensajes
            where dm_id = %s
            order by ts::numeric desc
            limit %s
        ) ultimos
        order by ts::numeric
        """,
        (dm_id, limite),
    )
