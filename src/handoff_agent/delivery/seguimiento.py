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
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .. import db, db_config, guards, ledger
from ..config import load_settings
from . import card, slack_writer, sugerencia
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

# Orden del resumen: primero lo que espera una respuesta suya, y dentro de cada
# grupo lo que lleva más tiempo esperando.
TITULOS = {
    NO_LEIDO: "Unread message",
    RESPONDER: "They replied, you haven't",
    SIN_RESPUESTA: "No answer yet",
    SIN_CONVERSACION: "Marked contacted, no conversation",
}
# Lo no leído va primero: es lo único que acaba de pasar de verdad. El resumen diario
# nunca lo trae, así que ahí el orden de los otros tres no cambia.
ORDEN = (NO_LEIDO, RESPONDER, SIN_RESPUESTA, SIN_CONVERSACION)
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
    # Solo lo no leído: cuántas horas lleva y cuántos hay sin abrir.
    horas: int | None = None
    sin_leer: int | None = None


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
    enlace, y la línea sale igual."""
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


def _descripcion(caso: Caso) -> str:
    quien = caso.nombre.split()[0] if caso.nombre.split() else caso.nombre
    quien = _escape_and_cap(quien, NAME_CHARS)
    if caso.tipo == NO_LEIDO:
        cuantos = caso.sin_leer or 1
        return f"{quien} wrote {_hace_horas(caso.horas or 0)}, {cuantos} unread"
    if caso.tipo == RESPONDER:
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


def casos(now: datetime | None = None, limite: int | None = None) -> list[Caso]:
    """Los casos pendientes de aviso, las más urgentes primero y acotadas.

    Solo lee: ni publica ni marca nada. Lo usa el resumen diario y también el
    MCP para responder "¿a quién le debo respuesta?"."""
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
    # El resumen nunca trae lo no leído (es cosa del aviso inmediato): `.get`, no `[]`.
    ordenados = [caso for tipo in ORDEN for caso in encontrados.get(tipo, [])]
    return ordenados[: max(0, int(tope))]


def bloques(lista: list[Caso], cabecera: str = CABECERA) -> list[dict]:
    """Los bloques de Slack del resumen: una sección por tipo de caso.

    `cabecera` distingue el resumen del día del aviso inmediato (0029); el cuerpo es el
    mismo, porque las líneas dicen lo mismo en los dos casos."""
    texto = [cabecera]
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


def _casos_para_avisar(ahora: datetime) -> list[Caso]:
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
    casos.sort(key=lambda c: (ORDEN.index(c.tipo), -c.dias))
    vistos: set[str | None] = set()
    unicos: list[Caso] = []
    for caso in casos:
        if caso.dm_id not in vistos:
            vistos.add(caso.dm_id)
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


def bloques_aviso(lista: list[Caso], sugeridas: dict[str, sugerencia.Sugerencia]) -> list[dict]:
    """Los bloques del aviso inmediato.

    Sin ninguna sugerencia es exactamente el formato de siempre (`bloques`). Con ellas,
    cada conversación va en su propia sección -- su línea, el contexto y la etiqueta --
    seguida de otra con la respuesta sugerida en un bloque de código. Va aparte porque
    un bloque `section` admite 3000 caracteres y la sugerencia sola puede acercarse.

    El contexto lo escribió el modelo leyendo una conversación de un tercero: aunque
    `sugerencia.py` ya lo validó, pasa por `card.quote` como cualquier cita ajena, para
    que ni un enlace ni una mención puedan salir vivos de ahí."""
    if not any(caso.dm_id in sugeridas for caso in lista if caso.dm_id):
        return bloques(lista, CABECERA_AVISO)
    resultado = [{"type": "section", "text": {"type": "mrkdwn", "text": CABECERA_AVISO}}]
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
        if not caso.dm_id:
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


def _avisar(now: datetime | None) -> int:
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

    lista = _casos_para_avisar(ahora)[:cupo]
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
        tanda = lista[inicio : inicio + MAX_CASOS_POR_AVISO]
        _registrar(
            ACCION_AVISO,
            {
                "casos": len(tanda),
                "tipos": [c.tipo for c in tanda],
                "sugerencias": sum(1 for c in tanda if c.dm_id in sugeridas),
            },
        )
        try:
            canal, ts = slack_writer.post_card(
                bloques_aviso(tanda, sugeridas), resumen_texto(tanda)
            )
        except Exception as exc:
            logger.exception("seguimiento: no se pudo publicar el aviso")
            _registrar(ACCION_FALLIDO, {"motivo": f"{type(exc).__name__}: {exc}"})
            break
        _registrar(ACCION_MENSAJE, {"tipo": "aviso", "canal": canal, "ts": ts})
        _marcar_avisados(tanda, ahora)
        publicados += len(tanda)
    return publicados


def publicar_avisos(now: datetime | None = None) -> int:
    """Avisa de las conversaciones que ya pasaron de su plazo, y vuelve a recordarlas
    cada `seguimiento_recordar_horas` mientras sigan esperando; y de lo no leído de las
    últimas 24 horas, una vez por mensaje. Con `dm_texto_activo`, cada conversación
    lleva su respuesta sugerida. Devuelve cuántas salieron. Nunca lanza: un
    recordatorio no puede tumbar el bucle."""
    try:
        return _avisar(now)
    except Exception:
        logger.exception("seguimiento: aviso fallido; se reintenta en la pasada siguiente")
        return 0


def _publicar(now: datetime | None) -> int:
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

    lista = casos(ahora)
    if not lista:
        # Nada que contar: no se publica y el día queda libre, por si más tarde
        # aparece un caso.
        return 0

    _registrar(ACCION_RESUMEN, {"casos": len(lista), "tipos": [c.tipo for c in lista]})
    try:
        canal, ts = slack_writer.post_card(bloques(lista), resumen_texto(lista))
    except Exception as exc:
        logger.exception("seguimiento: no se pudo publicar el resumen")
        _registrar(ACCION_FALLIDO, {"motivo": f"{type(exc).__name__}: {exc}"})
        return 0
    _registrar(ACCION_MENSAJE, {"tipo": "resumen", "canal": canal, "ts": ts})
    _marcar(lista)
    return len(lista)


def publicar_resumen(now: datetime | None = None) -> int:
    """Publica el resumen de hoy si toca. Devuelve cuántas líneas salieron.
    Nunca lanza: un recordatorio no puede tumbar el bucle."""
    try:
        return _publicar(now)
    except Exception:
        logger.exception("seguimiento: fallo inesperado; se reintenta en la vuelta siguiente")
        return 0
