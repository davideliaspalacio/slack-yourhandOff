"""El recordatorio de las conversaciones directas que se enfrían.

Un solo mensaje al día en el canal de tarjetas de Handoff, con el bot de
Handoff (nunca en el Founders Club: el agente no escribe allí). Tres tipos de
caso, calculados sobre el rastro que deja `ingest/dms.py`:

- **They replied, you haven't**: la otra persona escribió el último mensaje
  hace más de `seguimiento_horas_para_responder` (24).
- **No answer yet**: él escribió el último y lleva más de
  `seguimiento_dias_sin_respuesta` (3) sin respuesta.
- **Marked contacted, no conversation**: alguien marcado `contactado` hace más
  de `seguimiento_dias_sin_respuesta` sin ningún DM con él. Se espera ese mismo
  plazo a propósito: pulsar "Contacted" y recibir el aviso en el mismo minuto
  no ayudaría a nadie.

El aviso inmediato (0029, `publicar_avisos`) tiene además un cuarto caso, que el
resumen no: **Unread message**, un mensaje de un lead que Anthony todavía no ha
abierto (`dm_rastro.sin_leer`, 0031). Sale en cuanto llega, sin esperar las 24
horas de "They replied", y solo si el mensaje es de las últimas 24 horas: un
despliegue anterior inundó el canal con semanas de histórico de golpe, y lo
viejo nunca se anuncia como si acabara de pasar.

Lo que este módulo NO hace: leer lo que se dijeron. Aquí solo se interpolan el
nombre, la empresa, cuánto lleva y el enlace para abrir la conversación en
Slack. "Sunny replied 2 days ago", nunca qué dijo Sunny. Desde 0031, si
Anthony lo activó (`dm_texto_activo`), el aviso inmediato lleva debajo de cada
conversación una respuesta sugerida y una línea de contexto; las escribe
`delivery/sugerencia.py`, que es quien lee la conversación. Este módulo solo
recibe esas dos cadenas y las pinta, y un test (`tests/test_dms.py`) vigila que
siga sin poder leer nada más. El resumen diario no lleva sugerencias.

Sin repetirse: cada caso de DM se marca con el `ultimo_ts` por el que se avisó
(`dm_rastro.avisado_ts`), así que no vuelve a salir hasta que la conversación
cambie -- un mensaje nuevo, de cualquiera de los dos, lo reinicia. El caso sin
conversación se marca en el ledger (`seguimiento_avisado`) y vuelve si cambia
el estado de la persona.

Desde 0038 hay tres casos más, cada uno con su subtítulo, para lo que se escapaba:

- **Mentions you haven't answered** y **Thread replies you haven't answered**: lo que
  espera respuesta suya en los canales. Qué cuenta como pendiente, y la comprobación en
  vivo contra Slack que pasa cada uno antes de salir, están en
  `delivery/seguimiento_canal.py`; aquí solo se pintan y se publican. Enlazan al
  aviso concreto y nombran a la persona y el canal.
- **Direct messages from people who aren't leads yet**: el "They replied" de siempre,
  para gente sin ficha (`seguimiento_sin_ficha_activo`, que nace apagado). El rastro
  ya guardaba esas conversaciones, sin texto como todas; lo que faltaba era el nombre,
  y se le pregunta a Slack en el momento de avisar, sin guardarlo. Interrumpe una sola
  vez por cada vez que le escriben y luego queda en el resumen del día.

Los tres necesitan el lector (`reader`): sin él -- el MCP, o un test que no lo pasa --
no salen, y todo lo demás funciona exactamente como antes.

Frenos, como en el resto de la entrega: `seguimiento_activo`, el kill switch,
el tope mensual, el tope de líneas (`seguimiento_max_por_resumen`) y un único
resumen al día (contado en `zona_horaria`, igual que el SMS y las alertas).
A un descartado no se le avisa nunca.

El día se "gasta" antes de publicar, igual que en `delivery/aviso_lector.py`:
si Slack falla, se pierde el resumen de hoy en vez de reintentarlo en cada
vuelta del bucle. Los casos no se marcan como avisados, así que mañana salen
otra vez. Nada de esto lanza: el bucle no puede caerse por un recordatorio.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .. import canales, db, db_config, guards, ledger
from ..config import load_settings
from . import card, prioridad, seguimiento_canal, slack_writer, sugerencia
from .card import COMPANY_CHARS, NAME_CHARS, _escape_and_cap, _is_safe_link

logger = logging.getLogger(__name__)

ACCION_RESUMEN = "seguimiento_resumen"
ACCION_AVISADO = "seguimiento_avisado"
ACCION_PARADA = "seguimiento_parada"
ACCION_FALLIDO = "seguimiento_fallido"
# El aviso inmediato (0029) va aparte del resumen: tiene su propio cupo diario.
ACCION_AVISO = "seguimiento_aviso"
# Lo que Slack devolvió al publicar. Se anota DESPUÉS de publicar y aparte del marcador
# del día, que se escribe antes a propósito: sin esto no hay forma de volver a señalar un
# mensaje ya publicado, y borrar uno que salió por error se convierte en buscarlo a mano
# entre las tarjetas reales. Lo usa scripts/borrar_avisos.py.
ACCION_MENSAJE = "seguimiento_mensaje"

RESPONDER = "responder"
SIN_RESPUESTA = "sin_respuesta"
SIN_CONVERSACION = "sin_conversacion"
# Solo en el aviso inmediato (0031): ver el docstring del módulo.
NO_LEIDO = "no_leido"

# Los tres de 0038. Los dos de canal los calcula `seguimiento_canal.py`; el tercero es
# "They replied" para alguien que todavía no tiene ficha.
MENCION = seguimiento_canal.MENCION
HILO = seguimiento_canal.HILO
SIN_FICHA = "responder_sin_ficha"

# Orden del resumen: primero lo que espera una respuesta suya, y dentro de cada
# grupo lo que lleva más tiempo esperando.
TITULOS = {
    NO_LEIDO: "Unread message",
    RESPONDER: "They replied, you haven't",
    MENCION: "Mentions you haven't answered",
    HILO: "Thread replies you haven't answered",
    SIN_FICHA: "Direct messages from people who aren't leads yet",
    SIN_RESPUESTA: "No answer yet",
    SIN_CONVERSACION: "Marked contacted, no conversation",
}
# Lo no leído va primero: es lo único que acaba de pasar de verdad. El resumen diario
# nunca lo trae, así que ahí el orden de los demás no cambia. Los leads van por delante
# de los canales, y la gente sin ficha detrás: todo eso espera algo de él, pero no vale
# lo mismo. Lo que no depende de él cierra la lista, como siempre.
ORDEN = (NO_LEIDO, RESPONDER, MENCION, HILO, SIN_FICHA, SIN_RESPUESTA, SIN_CONVERSACION)
# Lo que necesita preguntarle algo a Slack antes de poder salir (ver `_preparar`).
CON_LECTOR = (MENCION, HILO, SIN_FICHA)
MAX_POR_RESUMEN = 10
CABECERA = "⏰ *FOLLOW-UPS*"
# El aviso inmediato lleva otra cabecera a propósito: en el canal se distingue de un
# golpe qué es "acaba de pasar esto" y qué es la foto del día.
CABECERA_AVISO = "⏳ *WAITING ON YOU*"
# Valores por omisión de 0029, los mismos que la migración deja en config.
RECORDAR_HORAS = 4
AVISOS_POR_DIA = 20
# Lo no leído solo se anuncia si es de las últimas 24 horas. Fijo y no en config a
# propósito: es la regla que impide volver a vaciar semanas de histórico en el canal, y
# no debe poder aflojarse desde el panel por descuido.
NO_LEIDO_HORAS = 24
# Con sugerencias, cada conversación ocupa varias líneas: más de cinco por mensaje ya
# no se leen de un vistazo, así que el aviso se parte en varios.
MAX_CASOS_POR_AVISO = 5
# El tope de Slack es de 3000 caracteres por bloque `section`; la sugerencia va sola en
# el suyo y, ya escapada, puede crecer hasta 5 veces ('&' -> '&amp;'). Recortarla solo
# pasa con una sugerencia absurda, llena de '&'.
SUGERENCIA_CHARS = 2990
# El nombre de un canal lo eligió alguien del club: se escapa y se recorta como todo.
CANAL_CHARS = 90
# A quien no tiene ficha ni Slack quiere dar el nombre, en un aviso de canal.
ALGUIEN = "Someone"


@dataclass(frozen=True)
class Caso:
    tipo: str
    dias: int
    nombre: str
    empresa: str | None = None
    dm_id: str | None = None
    team_id: str | None = None
    slack_user_id: str | None = None
    prospect_id: str | None = None
    ultimo_ts: str | None = None
    # Lo no leído y los casos de canal: cuántas horas lleva. `sin_leer`, solo lo no
    # leído: cuántos hay sin abrir.
    horas: int | None = None
    sin_leer: int | None = None
    # Solo los casos de canal (0038): de qué hilo o mención se trata, y el enlace
    # permanente que da Slack una vez comprobado que sigue pendiente.
    canal: seguimiento_canal.Pendiente | None = None
    enlace: str | None = None


def _zona() -> ZoneInfo:
    return ZoneInfo(db_config.value("zona_horaria", "America/New_York"))


def _inicio_del_dia(ahora: datetime) -> datetime:
    return ahora.astimezone(_zona()).replace(hour=0, minute=0, second=0, microsecond=0)


def _nombre(fila: dict) -> str:
    return fila.get("full_name") or fila.get("slack_user_id") or "unknown"


def _empresa(fila: dict) -> str | None:
    return fila.get("company_name_override") or fila.get("company_name")


def _dias_desde_ts(ts: object, ahora: datetime) -> int:
    try:
        return max(0, int((ahora.timestamp() - float(str(ts))) // 86400))
    except (TypeError, ValueError):
        return 0


def _enlace(caso: Caso) -> str | None:
    """`https://app.slack.com/client/<team_id>/<dm_id>`, el enlace que abre la
    conversación. Sin el código del workspace (que guarda el rastro) no hay
    enlace, y la línea sale igual. Un caso de canal trae el suyo, que lleva al
    aviso concreto y no a la conversación entera."""
    if caso.canal is not None:
        return caso.enlace if _is_safe_link(caso.enlace) else None
    if not (caso.team_id and caso.dm_id):
        return None
    url = f"https://app.slack.com/client/{caso.team_id}/{caso.dm_id}"
    return url if _is_safe_link(url) else None


def _horas_desde_ts(ts: object, ahora: datetime) -> int:
    try:
        return max(0, int((ahora.timestamp() - float(str(ts))) // 3600))
    except (TypeError, ValueError):
        return 0


def _hace_horas(horas: int) -> str:
    if horas < 1:
        return "less than an hour ago"
    return f"{horas} hour{'s' if horas != 1 else ''} ago"


def _plural(dias: int) -> str:
    if dias == 0:
        return "today"
    return f"{dias} day{'s' if dias != 1 else ''} ago"


def _hace(caso: Caso) -> str:
    """Cuánto lleva esperando un caso de canal: en horas los dos primeros días (una
    mención salta a las pocas horas, y "today" no diría nada), luego en días."""
    if caso.horas is not None and caso.horas < 48:
        return _hace_horas(caso.horas)
    return _plural(caso.dias)


def _descripcion_canal(caso: Caso, quien: str) -> str:
    pendiente = caso.canal
    assert pendiente is not None
    donde = _escape_and_cap(
        canales.etiqueta(pendiente.channel_id, pendiente.canal_nombre), CANAL_CHARS
    )
    if caso.tipo == MENCION:
        en_hilo = " (in a thread)" if pendiente.en_hilo else ""
        return f"{quien} mentioned you in {donde}{en_hilo} {_hace(caso)}"
    hilo = "in your thread" if pendiente.hilo_propio else "to you in their thread"
    mas = (
        f" (+{pendiente.mas} more repl{'ies' if pendiente.mas != 1 else 'y'})"
        if pendiente.mas
        else ""
    )
    return f"{quien} replied {hilo} in {donde} {_hace(caso)}{mas}"


def _descripcion(caso: Caso) -> str:
    quien = caso.nombre.split()[0] if caso.nombre.split() else caso.nombre
    quien = _escape_and_cap(quien, NAME_CHARS)
    if caso.canal is not None:
        return _descripcion_canal(caso, quien)
    if caso.tipo == NO_LEIDO:
        cuantos = caso.sin_leer or 1
        return f"{quien} wrote {_hace_horas(caso.horas or 0)}, {cuantos} unread"
    if caso.tipo in (RESPONDER, SIN_FICHA):
        return f"{quien} replied {_plural(caso.dias)}"
    if caso.tipo == SIN_RESPUESTA:
        return f"you wrote {_plural(caso.dias)}, no answer yet"
    return f"marked contacted {_plural(caso.dias)}, no conversation in Slack"


def linea(caso: Caso) -> str:
    """Una línea del resumen. Nombre, empresa, cuánto lleva y el enlace.

    El nombre y la empresa los escribió un tercero (su perfil de Slack, o el
    equipo en el panel): pasan por `_escape_and_cap` antes de entrar en el
    bloque `mrkdwn`, como en `card.py`. El texto del mensaje no entra: no se
    guarda en ninguna parte."""
    nombre = _escape_and_cap(caso.nombre, NAME_CHARS)
    empresa = _escape_and_cap(caso.empresa, COMPANY_CHARS)
    partes = [f"*{nombre}*" + (f" — {empresa}" if empresa else ""), _descripcion(caso)]
    url = _enlace(caso)
    if url:
        partes.append(f"<{url}|Open in Slack>")
    return "• " + " · ".join(partes)


def _casos_dm(tipo: str, limite_ts: float, ahora: datetime) -> list[Caso]:
    """Los DMs esperando, de los que llevan más tiempo a los que menos.

    Esto es la foto del día: sale todo lo que sigue esperando, se haya avisado ya o no.
    Son dos trabajos distintos y es a propósito (0029) -- el aviso inmediato dice "esto
    acaba de pasar" y no se repite hasta pasadas unas horas; el resumen dice "esto es
    todo lo que tienes pendiente ahora mismo", y un pendiente no deja de serlo porque
    ayer ya se avisara. Si el resumen descontara lo avisado, con recordatorios cada
    pocas horas saldría vacío todos los días.

    Solo conversaciones con gente que ya pasó por el research (`join`, no `left join`).
    Sin ficha no hay nombre ni empresa y la línea salía con el código de Slack repetido
    dos veces -- "U0BCQGYDU64 · U0BCQGYDU64 replied 83 days ago" --, que no es un
    recordatorio sino ruido. El seguimiento es para leads; el resto de sus conversaciones
    del club no son trabajo nuestro.

    A un descartado no se le avisa nunca, igual que en `signals/alertas.py`."""
    de = "otro" if tipo == RESPONDER else "dueno"
    filas = db.fetch_all(
        """
        select r.dm_id, r.team_id, r.slack_user_id, r.ultimo_ts, p.id::text as prospect_id,
               p.full_name, p.company_name, p.company_name_override
          from dm_rastro r
          join prospects p on p.id = r.prospect_id
         where r.ultimo_de = %s
           and r.ultimo_ts::numeric < %s
           -- Contestar con un 👍 es contestar (0033): deja de estar pendiente, así que
           -- tampoco sale en la foto del día.
           and not r.respondido_con_emoji
           and p.state <> 'descartado'
           and (p.sin_tarjeta is null or not p.sin_tarjeta)
         order by r.ultimo_ts::numeric
        """,
        (de, limite_ts),
    )
    return [
        Caso(
            tipo=tipo,
            dias=_dias_desde_ts(f["ultimo_ts"], ahora),
            nombre=_nombre(f),
            empresa=_empresa(f),
            dm_id=f["dm_id"],
            team_id=f["team_id"],
            slack_user_id=f["slack_user_id"],
            prospect_id=f["prospect_id"],
            ultimo_ts=f["ultimo_ts"],
        )
        for f in filas
    ]


def _casos_sin_conversacion(dias: int, ahora: datetime) -> list[Caso]:
    """Marcado contactado y sin ningún DM con él.

    No se repite: se descarta si ya hay un aviso en el ledger posterior al
    último cambio de la persona (cambiar su estado mueve `updated_at`)."""
    filas = db.fetch_all(
        """
        select p.id::text as prospect_id, p.slack_user_id, p.full_name, p.company_name,
               p.company_name_override, p.updated_at
          from prospects p
         where p.state = 'contactado'
           and not p.sin_tarjeta
           and p.updated_at < now() - make_interval(days => %(dias)s)
           and not exists (
               select 1 from dm_rastro r
                where r.prospect_id = p.id or r.slack_user_id = p.slack_user_id
           )
           and not exists (
               select 1 from agent_actions a
                where a.prospect_id = p.id and a.action = %(accion)s
                  and a.created_at >= p.updated_at
           )
         order by p.updated_at
        """,
        {"dias": dias, "accion": ACCION_AVISADO},
    )
    return [
        Caso(
            tipo=SIN_CONVERSACION,
            dias=max(0, (ahora - f["updated_at"]).days),
            nombre=_nombre(f),
            empresa=_empresa(f),
            slack_user_id=f["slack_user_id"],
            prospect_id=f["prospect_id"],
        )
        for f in filas
    ]


def _casos_sin_ficha(ahora: datetime, para_aviso: bool) -> list[Caso]:
    """ "They replied, you haven't" con alguien que todavía no tiene ficha (0038).

    El rastro ya guarda esas conversaciones -- sin texto, como todas --; lo que hacía
    ruido era avisar sin nombre. Aquí salen sin él y `_preparar` se lo pregunta a Slack;
    a quien Slack no le pone nombre, no se le avisa.

    Tres frenos que los leads no tienen, porque esto es gente con la que nadie decidió
    trabajar: nace apagado (`seguimiento_sin_ficha_activo`); solo cuenta lo que le
    escribieron DESPUÉS de encenderlo y dentro de `seguimiento_caduca_dias`; y el aviso
    inmediato sale una sola vez por cada vez que le escriben (`avisado_ts`), sin
    recordatorio cada pocas horas. Lo que siga esperando queda en el resumen del día."""
    if db_config.value("seguimiento_sin_ficha_activo", False) is not True:
        return []
    horas = db_config.value("seguimiento_horas_para_responder", 24)
    filas = db.fetch_all(
        """
        select r.dm_id, r.team_id, r.slack_user_id, r.ultimo_ts
          from dm_rastro r
         where r.prospect_id is null
           -- El rastro enlaza la ficha en su pasada siguiente; hasta entonces puede
           -- existir ya, y quien la tiene va por el camino de los leads (o por ninguno,
           -- si está descartado).
           and not exists (select 1 from prospects p where p.slack_user_id = r.slack_user_id)
           and r.ultimo_de = 'otro'
           and r.ultimo_ts::numeric < %(limite)s
           and r.ultimo_ts::numeric > %(desde)s
           and not r.respondido_con_emoji
           and (not %(para_aviso)s or r.avisado_ts is null or r.avisado_ts <> r.ultimo_ts)
         order by r.ultimo_ts::numeric
        """,
        {
            "limite": ahora.timestamp() - horas * 3600,
            "desde": seguimiento_canal.desde_ts(ahora, "seguimiento_sin_ficha_activo"),
            "para_aviso": para_aviso,
        },
    )
    return [
        Caso(
            tipo=SIN_FICHA,
            dias=_dias_desde_ts(f["ultimo_ts"], ahora),
            nombre=f["slack_user_id"],
            dm_id=f["dm_id"],
            team_id=f["team_id"],
            slack_user_id=f["slack_user_id"],
            ultimo_ts=f["ultimo_ts"],
        )
        for f in filas
    ]


def _casos_canal(ahora: datetime, owner_id: str, para_aviso: bool) -> list[Caso]:
    """Los hilos y las menciones que, según la base, esperan respuesta suya (0038).

    Son candidatos: ninguno se publica sin que `_preparar` compruebe en vivo que sigue
    pendiente. Para el aviso inmediato se quedan fuera los que aún no toca recordar o ya
    agotaron sus avisos; el resumen del día los trae todos."""
    recordar = db_config.value("seguimiento_recordar_horas", RECORDAR_HORAS)
    lista = []
    for pendiente in seguimiento_canal.candidatos(owner_id, ahora):
        if para_aviso and not seguimiento_canal.toca_avisar(pendiente, ahora, recordar):
            continue
        lista.append(
            Caso(
                tipo=pendiente.tipo,
                dias=_dias_desde_ts(pendiente.ts, ahora),
                nombre=pendiente.nombre or ALGUIEN,
                empresa=pendiente.empresa,
                slack_user_id=pendiente.slack_user_id,
                prospect_id=pendiente.prospect_id,
                horas=_horas_desde_ts(pendiente.ts, ahora),
                canal=pendiente,
            )
        )
    return lista


def _casos_con_lector(ahora: datetime, reader, para_aviso: bool) -> tuple[list[Caso], str]:
    """Los casos de 0038 y el id del dueño de la credencial, que hace falta después para
    comprobarlos. Sin lector no hay ninguno.

    Nada de aquí puede dejar sin aviso a los leads: si falla -- Slack no dice quién es
    él, la migración aún no está aplicada --, se anota y el seguimiento de siempre sigue
    sin ellos."""
    if reader is None:
        return [], ""
    try:
        if para_aviso:
            seguimiento_canal.limpiar()
        lista = _casos_sin_ficha(ahora, para_aviso)
        owner_id = reader.owner_id()
        return lista + _casos_canal(ahora, owner_id, para_aviso), owner_id
    except Exception:
        logger.exception("seguimiento: sin casos de canal ni de gente sin ficha en esta pasada")
        return [], ""


def _preparar(caso: Caso, reader, owner_id: str) -> Caso | None:
    """Lo último antes de que un caso de 0038 pueda salir. None = no sale.

    - Hilo o mención: se comprueba en vivo que no contestó ya (y si contestó, se apunta
      para no volver a preguntar). Solo entonces se piden el nombre, si no tiene ficha, y
      el enlace permanente.
    - Sin ficha: el nombre, que Slack da y no se guarda. Sin nombre no sale: un aviso con
      el código de Slack por nombre es ruido.

    Los demás casos pasan tal cual."""
    if caso.tipo not in CON_LECTOR:
        return caso
    try:
        if caso.canal is None:
            nombre = seguimiento_canal.nombre_de(reader, caso.slack_user_id)
            return replace(caso, nombre=nombre) if nombre else None
        pendiente = caso.canal
        respondio = seguimiento_canal.contestado(reader, owner_id, pendiente)
        if respondio:
            seguimiento_canal.resolver(pendiente)
        if respondio is not False:
            return None
        nombre = pendiente.nombre or seguimiento_canal.nombre_de(reader, pendiente.slack_user_id)
        return replace(
            caso,
            nombre=nombre or ALGUIEN,
            enlace=seguimiento_canal.enlace(reader, pendiente),
        )
    except Exception:
        # Ante la duda, no avisar; y que uno falle no se lleva por delante a los demás.
        logger.exception("seguimiento: no se pudo preparar un caso de %s", caso.tipo)
        return None


def _preparados(ordenados: list[Caso], tope: int, reader, owner_id: str) -> list[Caso]:
    """Los primeros `tope` casos que de verdad pueden salir. Se prepara sobre la marcha,
    así que a Slack solo se le pregunta por lo que cabe."""
    lista: list[Caso] = []
    for caso in ordenados:
        if len(lista) >= tope:
            break
        listo = _preparar(caso, reader, owner_id)
        if listo is not None:
            lista.append(listo)
    return lista


def casos(now: datetime | None = None, limite: int | None = None, reader=None) -> list[Caso]:
    """Los casos pendientes de aviso, las más urgentes primero y acotadas.

    Sin `reader` solo lee, ni publica ni marca nada, y trae los tres casos de siempre.
    Con él -- el resumen diario -- entran además los de 0038, ya comprobados contra
    Slack; lo único que eso escribe es el apunte de lo que resultó estar contestado."""
    ahora = now or datetime.now(UTC)
    horas = db_config.value("seguimiento_horas_para_responder", 24)
    dias = db_config.value("seguimiento_dias_sin_respuesta", 3)
    tope = (
        limite
        if limite is not None
        else db_config.value("seguimiento_max_por_resumen", MAX_POR_RESUMEN)
    )
    encontrados = {
        RESPONDER: _casos_dm(RESPONDER, ahora.timestamp() - horas * 3600, ahora),
        SIN_RESPUESTA: _casos_dm(SIN_RESPUESTA, ahora.timestamp() - dias * 86400, ahora),
        SIN_CONVERSACION: _casos_sin_conversacion(dias, ahora),
    }
    extra, owner_id = _casos_con_lector(ahora, reader, para_aviso=False)
    for caso in extra:
        encontrados.setdefault(caso.tipo, []).append(caso)
    # El resumen nunca trae lo no leído (es cosa del aviso inmediato): `.get`, no `[]`.
    ordenados = [caso for tipo in ORDEN for caso in encontrados.get(tipo, [])]
    return _preparados(ordenados, max(0, int(tope)), reader, owner_id)


# Los tipos cuyo nivel depende de quién espera (ver la tabla de `prioridad.nivel`): solo
# para ellos se pregunta si la persona es un lead caliente.
_SEGUN_QUIEN = (NO_LEIDO, RESPONDER, MENCION, HILO)


def nivel_de(caso: Caso) -> str:
    """Cuánto corre un caso (0039). Lo decide `prioridad.py`; aquí solo se le dice de
    qué tipo es y, si importa, si quien espera es un lead caliente."""
    caliente = caso.tipo in _SEGUN_QUIEN and prioridad.es_caliente(caso.prospect_id)
    return prioridad.nivel(caso.tipo, caliente=caliente)


def _nivel(lista: list[Caso]) -> str | None:
    """El nivel de un mensaje que junta varios casos: el del que más corre. None si las
    marcas están apagadas en Settings."""
    if not prioridad.activa():
        return None
    return prioridad.mas_urgente(nivel_de(caso) for caso in lista)


def bloques(lista: list[Caso], cabecera: str = CABECERA, nivel: str | None = None) -> list[dict]:
    """Los bloques de Slack del resumen: una sección por tipo de caso.

    `cabecera` distingue el resumen del día del aviso inmediato (0029); el cuerpo es el
    mismo, porque las líneas dicen lo mismo en los dos casos. `nivel` es la prioridad
    del mensaje (0039) y va delante de la cabecera; sin él, la de siempre."""
    texto = [prioridad.cabecera(cabecera, nivel)]
    for tipo in ORDEN:
        del_tipo = [c for c in lista if c.tipo == tipo]
        if not del_tipo:
            continue
        texto.append(f"\n*{TITULOS[tipo]}*")
        texto += [linea(caso) for caso in del_tipo]
    cuerpo = "\n".join(texto)
    return [{"type": "section", "text": {"type": "mrkdwn", "text": cuerpo}}]


def resumen_texto(lista: list[Caso]) -> str:
    """El `text` de la notificación push: cuántos, nunca quién dijo qué."""
    return f"Follow-ups: {len(lista)} conversation{'s' if len(lista) != 1 else ''} waiting"


def _registrar(action: str, payload: dict, prospect_id: str | None = None) -> None:
    try:
        ledger.record_action(action, payload, prospect_id=prospect_id)
    except Exception:
        logger.exception("seguimiento: no se pudo registrar %s en el ledger", action)


def _parada(exc: Exception) -> None:
    logger.warning("seguimiento detenido: %s", exc)
    _registrar(ACCION_PARADA, {"motivo": f"{type(exc).__name__}: {exc}"})


def _ya_hoy(ahora: datetime) -> bool:
    return (
        db.fetch_one(
            "select 1 as hay from agent_actions where action = %s and created_at >= %s limit 1",
            (ACCION_RESUMEN, _inicio_del_dia(ahora)),
        )
        is not None
    )


def _marcar(lista: list[Caso]) -> None:
    """Lo que ya se avisó no vuelve a salir. Solo después de publicar."""
    for caso in lista:
        try:
            if caso.canal is not None:
                # El resumen no gasta avisos de un hilo ni de una mención: mientras
                # sigan pendientes y sin caducar, vuelven a salir mañana.
                continue
            if caso.dm_id:
                db.execute(
                    "update dm_rastro set avisado_ts = %s where dm_id = %s",
                    (caso.ultimo_ts, caso.dm_id),
                )
            else:
                _registrar(ACCION_AVISADO, {"tipo": caso.tipo}, caso.prospect_id)
        except Exception:
            # Como mucho, el caso se repite mañana. Peor sería perder el resto.
            logger.exception("seguimiento: no se pudo marcar el caso %s", caso.dm_id or caso.nombre)


def _clave(caso: Caso) -> tuple[str | None, str | None]:
    """Lo que hace único a un caso dentro de una pasada: su conversación, o su hilo."""
    if caso.canal is not None:
        return (caso.canal.channel_id, caso.canal.clave_ts)
    return (caso.dm_id, None)


def _casos_para_avisar(ahora: datetime, extra: list[Caso] | None = None) -> list[Caso]:
    """Las conversaciones que ya pasaron de su plazo y a las que toca avisar ahora.

    Se diferencia del resumen en una cosa: el resumen saca cada conversación una sola
    vez (`avisado_ts`), y esto vuelve a sacarla pasadas `seguimiento_recordar_horas`
    desde el último aviso (`avisado_at`, 0029). Un mensaje nuevo de cualquiera de los
    dos cambia `ultimo_ts` y la conversación entra otra vez desde cero.

    Solo interrumpe lo que espera algo DE ÉL: un mensaje sin abrir y "le contestaron y
    no ha respondido". "Escribió y no le contestan" y "marcado contactado sin
    conversación" se quedan en el resumen diario.

    La diferencia no es de volumen sino de naturaleza, y se aprendió mandando: con las
    cuatro clases interrumpiendo por igual salieron 18 mensajes en 13 horas, uno cada 45
    minutos, y 15 de las 20 conversaciones eran de las que no dependen de él. Recordarle
    cada pocas horas que alguien no le ha contestado no le deja hacer nada distinto: él
    ya hizo su parte. Lo que sí le puede costar un lead es la persona que está esperando
    su respuesta, y eso es lo único que corta su día.

    Lo no leído (0031) entra primero y una conversación sale una sola vez por pasada:
    con `seguimiento_horas_para_responder` por debajo de 24, un mismo mensaje sin abrir
    podría ser a la vez "no leído" y "contestó", y no son dos avisos.
    """
    horas_responder = db_config.value("seguimiento_horas_para_responder", 24)
    recordar = db_config.value("seguimiento_recordar_horas", RECORDAR_HORAS)
    casos: list[Caso] = []
    for tipo, limite_ts in ((RESPONDER, ahora.timestamp() - horas_responder * 3600),):
        de = "otro"
        filas = db.fetch_all(
            """
            select r.dm_id, r.team_id, r.slack_user_id, r.ultimo_ts, p.id::text as prospect_id,
                   p.full_name, p.company_name, p.company_name_override
              from dm_rastro r
              join prospects p on p.id = r.prospect_id
             where r.ultimo_de = %(de)s
               and r.ultimo_ts::numeric < %(limite)s
               -- Nunca avisada, o avisada hace más de lo que toca recordar.
               and (r.avisado_at is null
                    or r.avisado_at < %(ahora)s - make_interval(hours => %(recordar)s))
               and p.state <> 'descartado'
               and not p.sin_tarjeta
               -- Contestar con un 👍 es contestar (0033).
               and not r.respondido_con_emoji
             order by r.ultimo_ts::numeric
            """,
            {"de": de, "limite": limite_ts, "ahora": ahora, "recordar": recordar},
        )
        casos.extend(
            Caso(
                tipo=tipo,
                dias=_dias_desde_ts(f["ultimo_ts"], ahora),
                nombre=_nombre(f),
                empresa=_empresa(f),
                dm_id=f["dm_id"],
                team_id=f["team_id"],
                slack_user_id=f["slack_user_id"],
                prospect_id=f["prospect_id"],
                ultimo_ts=f["ultimo_ts"],
            )
            for f in filas
        )
    casos.extend(_casos_no_leidos(ahora))
    # Los de 0038 (`extra`) entran en la misma lista y con las mismas reglas: un solo
    # orden y un solo cupo para todo lo que espera algo de él.
    casos.extend(extra or [])
    casos.sort(key=lambda c: (ORDEN.index(c.tipo), -c.dias))
    vistos: set[tuple[str | None, str | None]] = set()
    unicos: list[Caso] = []
    for caso in casos:
        if _clave(caso) not in vistos:
            vistos.add(_clave(caso))
            unicos.append(caso)
    return unicos


def _casos_no_leidos(ahora: datetime) -> list[Caso]:
    """Un lead escribió y Anthony no lo ha abierto: se avisa ya, sin esperar el plazo
    de "They replied".

    Cuatro condiciones, cada una por algo:
    - `sin_leer > 0` y el último mensaje es del otro: si lo último lo escribió él, ya
      lo leyó.
    - No avisado por este mensaje (`avisado_ts`): sale una vez por mensaje, no en cada
      vuelta del bucle. Uno nuevo cambia `ultimo_ts` y vuelve a avisar.
    - De las últimas `NO_LEIDO_HORAS`: lo viejo sin abrir no se anuncia como nuevo. Es
      la regla que impide vaciar el histórico en el canal (ver el docstring del módulo).
    - Solo leads, igual que los otros casos: con ficha, sin descartar y fuera del modo
      de pruebas.
    """
    filas = db.fetch_all(
        """
        select r.dm_id, r.team_id, r.slack_user_id, r.ultimo_ts, r.sin_leer,
               p.id::text as prospect_id, p.full_name, p.company_name, p.company_name_override
          from dm_rastro r
          join prospects p on p.id = r.prospect_id
         where r.sin_leer > 0
           and r.ultimo_de = 'otro'
           and (r.avisado_ts is null or r.avisado_ts <> r.ultimo_ts)
           and r.ultimo_ts::numeric >= %(desde)s
           and p.state <> 'descartado'
           and not p.sin_tarjeta
           -- Contestar con un 👍 es contestar (0033).
           and not r.respondido_con_emoji
         order by r.ultimo_ts::numeric
        """,
        {"desde": ahora.timestamp() - NO_LEIDO_HORAS * 3600},
    )
    return [
        Caso(
            tipo=NO_LEIDO,
            dias=_dias_desde_ts(f["ultimo_ts"], ahora),
            nombre=_nombre(f),
            empresa=_empresa(f),
            dm_id=f["dm_id"],
            team_id=f["team_id"],
            slack_user_id=f["slack_user_id"],
            prospect_id=f["prospect_id"],
            ultimo_ts=f["ultimo_ts"],
            horas=_horas_desde_ts(f["ultimo_ts"], ahora),
            sin_leer=f["sin_leer"],
        )
        for f in filas
    ]


def _bloque_sugerida(respuesta: str) -> str:
    """La respuesta sugerida como bloque de código: se copia de un gesto y Slack no
    autoenlaza ni interpreta nada dentro. Aun así se escapa (Slack decodifica las
    entidades también ahí) y un acento grave pasa a apóstrofo para que no pueda cerrar
    el bloque antes de tiempo, igual que en `card.quote`."""
    limpia = respuesta.replace("`", "'")
    return "```" + _escape_and_cap(limpia, SUGERENCIA_CHARS) + "```"


def bloques_aviso(
    lista: list[Caso], sugeridas: dict[str, sugerencia.Sugerencia], nivel: str | None = None
) -> list[dict]:
    """Los bloques del aviso inmediato. `nivel` es su prioridad (0039), delante de la
    cabecera.

    Sin ninguna sugerencia es exactamente el formato de siempre (`bloques`). Con ellas,
    cada conversación va en su propia sección -- su línea, el contexto y la etiqueta --
    seguida de otra con la respuesta sugerida en un bloque de código. Va aparte porque
    un bloque `section` admite 3000 caracteres y la sugerencia sola puede acercarse.

    El contexto lo escribió el modelo leyendo una conversación de un tercero: aunque
    `sugerencia.py` ya lo validó, pasa por `card.quote` como cualquier cita ajena, para
    que ni un enlace ni una mención puedan salir vivos de ahí."""
    if not any(caso.dm_id in sugeridas for caso in lista if caso.dm_id):
        return bloques(lista, CABECERA_AVISO, nivel)
    cabecera = prioridad.cabecera(CABECERA_AVISO, nivel)
    resultado = [{"type": "section", "text": {"type": "mrkdwn", "text": cabecera}}]
    for tipo in ORDEN:
        del_tipo = [c for c in lista if c.tipo == tipo]
        for i, caso in enumerate(del_tipo):
            partes = [f"*{TITULOS[tipo]}*"] if i == 0 else []
            partes.append(linea(caso))
            propuesta = sugeridas.get(caso.dm_id or "")
            if propuesta:
                partes += [card.quote(propuesta.contexto), "_Suggested reply:_"]
            resultado.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(partes)}}
            )
            if propuesta:
                resultado.append(
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": _bloque_sugerida(propuesta.texto)},
                    }
                )
    return resultado


def _sugeridas(lista: list[Caso]) -> dict[str, sugerencia.Sugerencia]:
    """Una sugerencia por conversación, si `sugerencia.py` la da. Aquí no se lee nada de
    la conversación: solo se piden las dos cadenas ya validadas. Con `dm_texto_activo`
    apagado no hay ninguna y el aviso sale como siempre."""
    sugeridas: dict[str, sugerencia.Sugerencia] = {}
    for caso in lista:
        # Solo leads: de la conversación de alguien sin ficha no se lee nada, nunca.
        if not caso.dm_id or caso.tipo == SIN_FICHA:
            continue
        propuesta = sugerencia.sugerir(caso.dm_id)
        if propuesta is not None:
            sugeridas[caso.dm_id] = propuesta
    return sugeridas


def _avisos_hoy(ahora: datetime) -> int:
    fila = db.fetch_one(
        "select count(*) as n from agent_actions where action = %s and created_at >= %s",
        (ACCION_AVISO, _inicio_del_dia(ahora)),
    )
    return fila["n"] if fila else 0


def _marcar_avisados(lista: list[Caso], ahora: datetime) -> None:
    """Apunta el aviso: por qué mensaje fue y cuándo. Solo después de publicar."""
    for caso in lista:
        if not caso.dm_id:
            continue
        try:
            db.execute(
                "update dm_rastro set avisado_ts = %s, avisado_at = %s where dm_id = %s",
                (caso.ultimo_ts, ahora, caso.dm_id),
            )
        except Exception:
            # Como mucho se repite en la pasada siguiente. Peor sería perder el resto.
            logger.exception("seguimiento: no se pudo marcar el aviso de %s", caso.dm_id)


def _reservados(tanda: list[Caso], ahora: datetime) -> list[Caso]:
    """La tanda sin los casos de canal que no se pudieron reservar. La reserva gasta uno
    de los avisos de ese hilo o mención ANTES de publicar: si otro worker se adelantó, o
    en este rato dejó de tocar, aquí se cae. Los DMs no se reservan uno a uno: su freno
    es el marcador del cupo, como siempre."""
    recordar = db_config.value("seguimiento_recordar_horas", RECORDAR_HORAS)
    return [
        caso
        for caso in tanda
        if caso.canal is None or seguimiento_canal.reservar(caso.canal, ahora, recordar)
    ]


def _liberar(tanda: list[Caso]) -> None:
    for caso in tanda:
        if caso.canal is None:
            continue
        try:
            seguimiento_canal.liberar(caso.canal)
        except Exception:
            # Como mucho ese hilo pierde uno de sus avisos. Peor sería lanzar aquí.
            logger.exception("seguimiento: no se pudo liberar la reserva de un caso de canal")


def _avisar(now: datetime | None, reader=None) -> int:
    ahora = now or datetime.now(UTC)
    if not db_config.value("seguimiento_activo", True):
        return 0
    try:
        guards.check_kill_switch()
        guards.check_monthly_budget()
    except (guards.KillSwitchActive, guards.MonthlyBudgetExceeded) as exc:
        _parada(exc)
        return 0

    settings = load_settings()
    if not (settings.handoff_bot_token and settings.handoff_channel_id):
        logger.info("avisos de seguimiento sin configurar (faltan HANDOFF_SLACK_*), se omite")
        return 0

    cupo = db_config.value("seguimiento_avisos_por_dia", AVISOS_POR_DIA) - _avisos_hoy(ahora)
    if cupo <= 0:
        # El resumen diario sigue siendo la red: lo que no avisó hoy sale ahí.
        return 0

    extra, owner_id = _casos_con_lector(ahora, reader, para_aviso=True)
    lista = _preparados(_casos_para_avisar(ahora, extra), cupo, reader, owner_id)
    if not lista:
        return 0
    # Solo para lo que de verdad se va a publicar: lo que el cupo dejó fuera no gasta
    # ninguna llamada al modelo.
    sugeridas = _sugeridas(lista)

    # En tandas de `MAX_CASOS_POR_AVISO`. Cada tanda es un mensaje y cuenta como un aviso
    # del cupo diario, igual que antes; como el cupo ya recortó la lista por casos, el
    # número de mensajes nunca puede pasarse de él. Si una tanda falla, se para: el resto
    # sale en la pasada siguiente, sin marcar.
    publicados = 0
    for inicio in range(0, len(lista), MAX_CASOS_POR_AVISO):
        tanda = _reservados(lista[inicio : inicio + MAX_CASOS_POR_AVISO], ahora)
        if not tanda:
            continue
        _registrar(
            ACCION_AVISO,
            {
                "casos": len(tanda),
                "tipos": [c.tipo for c in tanda],
                "sugerencias": sum(1 for c in tanda if c.dm_id in sugeridas),
            },
        )
        try:
            nivel = _nivel(tanda)
            canal, ts = slack_writer.post_card(
                bloques_aviso(tanda, sugeridas, nivel),
                prioridad.resumen(resumen_texto(tanda), nivel),
            )
        except Exception as exc:
            logger.exception("seguimiento: no se pudo publicar el aviso")
            _registrar(ACCION_FALLIDO, {"motivo": f"{type(exc).__name__}: {exc}"})
            _liberar(tanda)
            break
        _registrar(ACCION_MENSAJE, {"tipo": "aviso", "canal": canal, "ts": ts})
        _marcar_avisados(tanda, ahora)
        publicados += len(tanda)
    return publicados


def publicar_avisos(now: datetime | None = None, reader=None) -> int:
    """Avisa de las conversaciones que ya pasaron de su plazo, y vuelve a recordarlas
    cada `seguimiento_recordar_horas` mientras sigan esperando; y de lo no leído de las
    últimas 24 horas, una vez por mensaje. Con `dm_texto_activo`, cada conversación
    lleva su respuesta sugerida. Con `reader` (el lector del Founders Club, solo para
    leer) entran además los hilos, las menciones y la gente sin ficha de 0038.
    Devuelve cuántas salieron. Nunca lanza: un recordatorio no puede tumbar el bucle."""
    try:
        return _avisar(now, reader)
    except Exception:
        logger.exception("seguimiento: aviso fallido; se reintenta en la pasada siguiente")
        return 0


def _publicar(now: datetime | None, reader=None) -> int:
    ahora = now or datetime.now(UTC)
    if not db_config.value("seguimiento_activo", True):
        return 0
    try:
        guards.check_kill_switch()
        guards.check_monthly_budget()
    except (guards.KillSwitchActive, guards.MonthlyBudgetExceeded) as exc:
        _parada(exc)
        return 0

    settings = load_settings()
    if not (settings.handoff_bot_token and settings.handoff_channel_id):
        # Sin bot o sin canal no hay dónde publicar; el día no se gasta.
        logger.info("seguimiento sin configurar (faltan HANDOFF_SLACK_*), se omite")
        return 0
    if _ya_hoy(ahora):
        return 0

    lista = casos(ahora, reader=reader)
    if not lista:
        # Nada que contar: no se publica y el día queda libre, por si más tarde
        # aparece un caso.
        return 0

    _registrar(ACCION_RESUMEN, {"casos": len(lista), "tipos": [c.tipo for c in lista]})
    try:
        nivel = _nivel(lista)
        canal, ts = slack_writer.post_card(
            bloques(lista, nivel=nivel), prioridad.resumen(resumen_texto(lista), nivel)
        )
    except Exception as exc:
        logger.exception("seguimiento: no se pudo publicar el resumen")
        _registrar(ACCION_FALLIDO, {"motivo": f"{type(exc).__name__}: {exc}"})
        return 0
    _registrar(ACCION_MENSAJE, {"tipo": "resumen", "canal": canal, "ts": ts})
    _marcar(lista)
    return len(lista)


def publicar_resumen(now: datetime | None = None, reader=None) -> int:
    """Publica el resumen de hoy si toca. Devuelve cuántas líneas salieron. Con `reader`
    trae también los casos de 0038. Nunca lanza: un recordatorio no puede tumbar el
    bucle."""
    try:
        return _publicar(now, reader)
    except Exception:
        logger.exception("seguimiento: fallo inesperado; se reintenta en la vuelta siguiente")
        return 0
