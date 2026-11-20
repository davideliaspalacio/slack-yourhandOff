"""El resumen del día: lo que sigue pendiente, lo más urgente arriba (migración 0039).

Un solo mensaje al día en el canal de tarjetas de Handoff, con el bot de Handoff (nunca
en el Founders Club), a partir de `resumen_prioridad_hora` en `zona_horaria`. Los avisos
inmediatos dicen "acaba de pasar esto"; este dice "esto es lo que tienes abierto ahora
mismo, y por dónde empezar". Va agrupado por los tres niveles de `prioridad.py`, con un
tope de líneas por nivel (`resumen_prioridad_max_por_nivel`) y un enlace en cada línea.

Qué entra, y por qué eso es "pendiente de verdad"
-------------------------------------------------

- **Personas con tarjeta de research o con un "they asked"** de los últimos
  `resumen_prioridad_dias` que nadie ha marcado todavía: en cuanto alguien pulsa
  "Contacted" o "Discard" (en la tarjeta o en el panel) la persona deja de salir. Una
  persona es una línea aunque tenga las dos cosas, y solo cuenta la tarjeta de su
  último dossier: si se la volvió a investigar y ya no da para tarjeta, no sale.
- **Lo que espera respuesta suya**: los casos del seguimiento (`seguimiento.casos`),
  que ya comprueba en vivo contra Slack que no contestó.
- **Presentaciones sin saludar** de las últimas 48 horas, comprobadas también en vivo:
  si ya escribió en ese hilo o reaccionó a la presentación, no sale.
- **La credencial del lector caducada**, mientras siga caducada.

Qué no entra: los miembros nuevos (no hay forma de saber si ya les dio la bienvenida, y
ante la duda no se recuerda; el correo diario ya los lista) y el "What's new".

Por qué absorbe el resumen de seguimiento ("FOLLOW-UPS")
-------------------------------------------------------

Ya había un resumen diario, el de las conversaciones que se enfrían. Dos resúmenes al
día serían un mensaje más, y lo que AG pidió es organizarse mejor, no leer más. Así que
mientras este esté encendido (`resumen_prioridad_activo`), el otro no se publica
(`ingest/loop.py` elige uno de los dos) y sus casos salen aquí, cada uno en su nivel,
con las mismas líneas y marcados como avisados con la misma función (`seguimiento._marcar`).
Apagado, vuelve el de siempre, sin tocar nada más. El día del cambio no hay dos: si
"FOLLOW-UPS" ya salió hoy, este espera a mañana.

Sin duplicar y sin insistir
---------------------------

- **Se reserva antes de publicar** (`resumenes_prioridad`, una fila por día): dos
  workers, o un reintento, chocan con la clave primaria y solo uno publica.
- **Sin nada pendiente no se publica**, y el día queda cerrado (`vacio`): si a las diez
  llega una tarjeta, ya tuvo su aviso inmediato; un resumen de una línea diez minutos
  después sería decir lo mismo dos veces. Sale mañana si sigue sin tocar.
- **Si Slack falla** se reintenta en la pasada siguiente, hasta `MAX_INTENTOS`; si el
  worker muere a medias, ese día no hay resumen, que es mejor que dos.
- Respeta el kill switch y el tope mensual, como el resto de la entrega.

Nada de esto lanza: el bucle no puede caerse por un resumen. Y nada de esto escribe ni
lee nada nuevo en el Founders Club: las comprobaciones en vivo usan los métodos de
lectura que el seguimiento ya usaba.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from .. import db, db_config, guards, ledger
from ..config import load_settings
from . import alerta_card, aviso_lector, prioridad, seguimiento, seguimiento_canal, slack_writer
from .card import (
    BAND_LABELS,
    COMPANY_CHARS,
    NAME_CHARS,
    NUM_CHARS,
    _escape_and_cap,
    _is_safe_link,
    _safe_number,
)

logger = logging.getLogger(__name__)

ACCION_PUBLICADO = "resumen_prioridad_publicado"
ACCION_FALLIDO = "resumen_prioridad_fallido"
ACCION_PARADA = "resumen_prioridad_parada"

CABECERA = "🧭 *DAILY BRIEF · what to handle first*"
PIE = "People drop off this list once they are marked Contacted or Discard."

# Valores por omisión, los mismos que la migración 0039 deja en config.
HORA = 8
MAX_POR_NIVEL = 5
DIAS = 7
# Cuántas veces se intenta publicar el resumen de un día si Slack falla.
MAX_INTENTOS = 3
# Una presentación se saluda en caliente: pasados dos días ya no se recuerda.
PRESENTACION_HORAS = 48
# El tope de Slack es de 3000 caracteres por bloque `section`: un nivel con muchas
# líneas largas se reparte en varios bloques.
BLOQUE_CHARS = 2900
# Un enlace más largo que esto no es un enlace de Slack ni del panel: no se pinta.
ENLACE_CHARS = 500

# Con quién no hay nada pendiente: ya se habló con ella, o se decidió que no.
ESTADOS_ATENDIDOS = ("contactado", "cliente", "descartado")

# El orden dentro de un mismo nivel: primero quien espera una respuesta suya, luego
# quien pidió algo, luego lo demás. Un tipo que no esté aquí va al final.
PESOS = {
    prioridad.CREDENCIAL: 0,
    prioridad.RESPONDER: 1,
    prioridad.PIDIERON: 2,
    prioridad.MENCION: 3,
    prioridad.HILO: 4,
    prioridad.SIN_FICHA: 5,
    prioridad.RESEARCH: 6,
    prioridad.PRESENTACION: 7,
    prioridad.SIN_RESPUESTA: 8,
    prioridad.SIN_CONVERSACION: 9,
}


@dataclass(frozen=True)
class Item:
    """Una línea candidata del resumen. `pintar` la escribe: se llama solo para las que
    caben, porque algunas le piden su enlace a Slack."""

    nivel: str
    tipo: str
    # Desempate dentro del mismo nivel y tipo: el menor va primero.
    desempate: float
    pintar: Callable[[], str] = field(compare=False)
    # Solo los casos del seguimiento: hay que marcarlos como avisados al publicar.
    caso: seguimiento.Caso | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Grupo:
    nivel: str
    lineas: tuple[str, ...]
    # Cuántos había en total en este nivel; los que no caben se cuentan, no se pintan.
    total: int
    items: tuple[Item, ...] = ()


def _zona() -> ZoneInfo:
    return ZoneInfo(db_config.value("zona_horaria", "America/New_York"))


def activo() -> bool:
    """El interruptor de Settings. Encendido, este resumen sustituye al de seguimiento."""
    return db_config.value("resumen_prioridad_activo", True) is True


def _enlace(url: str | None, texto: str) -> str | None:
    if not url or len(url) > ENLACE_CHARS or not _is_safe_link(url):
        return None
    return f"<{url}|{texto}>"


def _enlace_panel(ruta: str, texto: str) -> str | None:
    panel_url = load_settings().panel_url
    return _enlace(f"{panel_url}{ruta}", texto) if panel_url else None


# ---------------------------------------------------------------------------------
# Personas con tarjeta o con un "they asked" que nadie ha marcado
# ---------------------------------------------------------------------------------

_SQL_TARJETAS = """
select distinct on (d.prospect_id)
       d.prospect_id::text as prospect_id, d.band, d.channel_id, d.message_ts, d.created_at,
       p.full_name, p.slack_user_id, p.company_name, p.company_name_override,
       dz.content -> 'encaje_handoff' -> 'puntuacion' as puntuacion
  from deliveries d
  join prospects p on p.id = d.prospect_id
  join dossiers dz on dz.prospect_id = d.prospect_id and dz.version = d.dossier_version
 where d.kind = 'slack'
   and d.band in ('alta', 'media')
   -- Una reserva sin `message_ts` es una tarjeta que no llegó a salir (o que no se
   -- pudo anotar): no hay nada que enlazar.
   and d.message_ts is not null
   and d.created_at >= %(desde)s
   and p.state::text <> all(%(atendidos)s::text[])
   and not coalesce(p.sin_tarjeta, false)
   -- Solo la tarjeta del último dossier: si se la volvió a investigar y ahora no da
   -- para tarjeta, la vieja ya no dice la verdad.
   and d.dossier_version = (select max(x.version) from dossiers x
                             where x.prospect_id = d.prospect_id)
 order by d.prospect_id, d.created_at desc
"""

_SQL_PIDIERON = """
select distinct on (p.id)
       p.id::text as prospect_id, ms.tipo, ms.confianza, ms.alerta_channel_id, ms.alerta_ts,
       ms.alerta_at,
       p.full_name, p.slack_user_id, p.company_name, p.company_name_override
  from message_signals ms
  join slack_messages sm on sm.channel_id = ms.channel_id and sm.ts = ms.ts
  -- Con ficha: sin ella no hay botón con el que decir "ya está", y una línea que no se
  -- puede quitar no es un pendiente, es ruido. El research de quien escribe se encola
  -- solo, así que la ficha llega enseguida.
  join prospects p on p.slack_user_id = sm.user_id
 where ms.detectada
   and ms.alerta_estado = 'publicada'
   and ms.alerta_at >= %(desde)s
   and p.state::text <> all(%(atendidos)s::text[])
   and not coalesce(p.sin_tarjeta, false)
 order by p.id,
          case ms.confianza when 'alta' then 0 when 'media' then 1 else 2 end,
          ms.alerta_at desc
"""


def _linea_persona(fila: dict, ahora: datetime) -> str:
    """`• *Nombre* — Empresa · they asked: support · high signal, fit 3/3 · 2 days ago ·
    Open card · Open in panel`. El nombre y la empresa salen de un perfil ajeno: se
    escapan y se recortan como en las tarjetas."""
    nombre = _escape_and_cap(fila.get("full_name") or fila.get("slack_user_id"), NAME_CHARS)
    empresa = _escape_and_cap(
        fila.get("company_name_override") or fila.get("company_name"), COMPANY_CHARS
    )
    partes = [f"*{nombre}*" + (f" — {empresa}" if empresa else "")]
    if fila.get("pidio"):
        partes.append(f"they asked: {alerta_card.tipo_label(fila['pidio'])}")
    if fila.get("band"):
        nota = _safe_number(fila.get("puntuacion"), NUM_CHARS)
        etiqueta = BAND_LABELS.get(fila["band"], "").lower()
        partes.append(f"{etiqueta} signal" + (f", fit {nota}/3" if nota else ""))
    partes.append(seguimiento._plural(max(0, (ahora - fila["cuando"]).days)))
    canal, ts = fila.get("aviso") or (None, None)
    if canal and ts:
        # El enlace real al aviso en el Slack de Handoff: lo pide el bot, es una lectura.
        try:
            tarjeta = _enlace(slack_writer.card_permalink(canal, ts), "Open card")
        except Exception:
            logger.exception("resumen: sin enlace a la tarjeta de %s", fila["prospect_id"])
            tarjeta = None
        if tarjeta:
            partes.append(tarjeta)
    panel = _enlace_panel(f"/personas/{fila['prospect_id']}", "Open in panel")
    if panel:
        partes.append(panel)
    return "• " + " · ".join(partes)


def _personas(ahora: datetime) -> list[Item]:
    parametros = {
        "desde": ahora - timedelta(days=db_config.value("resumen_prioridad_dias", DIAS)),
        "atendidos": list(ESTADOS_ATENDIDOS),
    }
    por_persona: dict[str, dict] = {}
    for fila in db.fetch_all(_SQL_TARJETAS, parametros):
        por_persona[fila["prospect_id"]] = {
            **fila,
            "cuando": fila["created_at"],
            "aviso": (fila["channel_id"], fila["message_ts"]),
            "niveles": [prioridad.nivel(prioridad.RESEARCH, banda=fila["band"])],
            "tipo": prioridad.RESEARCH,
        }
    for fila in db.fetch_all(_SQL_PIDIERON, parametros):
        nivel = prioridad.nivel(prioridad.PIDIERON, confianza=fila["confianza"])
        previa = por_persona.get(fila["prospect_id"])
        if previa is None:
            por_persona[fila["prospect_id"]] = {
                **fila,
                "cuando": fila["alerta_at"],
                "aviso": (fila["alerta_channel_id"], fila["alerta_ts"]),
                "niveles": [nivel],
                "tipo": prioridad.PIDIERON,
                "pidio": fila["tipo"],
            }
        else:
            # Una persona, una línea: con tarjeta y además pidió algo. Manda lo que más
            # corre, y el enlace sigue siendo el de su tarjeta, que es la que trae los
            # botones.
            previa["niveles"].append(nivel)
            previa["tipo"] = prioridad.PIDIERON
            previa["pidio"] = fila["tipo"]
            previa["cuando"] = max(previa["cuando"], fila["alerta_at"])
    return [
        Item(
            nivel=prioridad.mas_urgente(fila["niveles"]) or prioridad.FYI,
            tipo=fila["tipo"],
            # Lo más reciente primero: es lo que todavía está caliente.
            desempate=-fila["cuando"].timestamp(),
            pintar=lambda fila=fila: _linea_persona(fila, ahora),
        )
        for fila in por_persona.values()
    ]


# ---------------------------------------------------------------------------------
# Lo que espera respuesta suya: los casos del seguimiento
# ---------------------------------------------------------------------------------


def _seguimientos(ahora: datetime, reader) -> list[Item]:
    """Los mismos casos que traería "FOLLOW-UPS", cada uno con su nivel. Con el
    seguimiento apagado en Settings no hay ninguno, igual que no habría resumen."""
    if not db_config.value("seguimiento_activo", True):
        return []
    return [
        Item(
            nivel=seguimiento.nivel_de(caso),
            tipo=caso.tipo,
            # `casos` ya los trae en su orden: los que más llevan esperando, primero.
            desempate=float(posicion),
            pintar=lambda caso=caso: seguimiento.linea(caso),
            caso=caso,
        )
        for posicion, caso in enumerate(seguimiento.casos(ahora, reader=reader))
    ]


# ---------------------------------------------------------------------------------
# Presentaciones sin saludar
# ---------------------------------------------------------------------------------

_SQL_PRESENTACIONES = """
select e.channel_id, e.ts, e.slack_user_id, p.id::text as prospect_id,
       p.full_name, p.company_name, p.company_name_override
  from eventos_canal e
  left join prospects p on p.slack_user_id = e.slack_user_id
 where e.tipo = 'presentacion'
   -- Solo de las que ya se avisó: el resumen recuerda, no anuncia.
   and e.estado = 'publicada'
   and e.aviso_ts is not null
   and e.slack_user_id is not null
   and e.ts::numeric >= %(desde)s
   and (p.id is null or (p.state::text <> all(%(atendidos)s::text[])
                         and not coalesce(p.sin_tarjeta, false)))
 order by e.ts::numeric desc
 limit %(limite)s
"""


def _linea_presentacion(fila: dict, nombre: str, enlace: str | None, ahora: datetime) -> str:
    empresa = _escape_and_cap(
        fila.get("company_name_override") or fila.get("company_name"), COMPANY_CHARS
    )
    horas = seguimiento._horas_desde_ts(fila["ts"], ahora)
    partes = [
        f"*{_escape_and_cap(nombre, NAME_CHARS)}*" + (f" — {empresa}" if empresa else ""),
        f"introduced themselves {seguimiento._hace_horas(horas)}, no hello from you yet",
    ]
    decir_hola = _enlace(enlace, "Say hi in Slack")
    if decir_hola:
        partes.append(decir_hola)
    return "• " + " · ".join(partes)


def _presentaciones(ahora: datetime, reader, tope: int) -> list[Item]:
    """Quien se presentó hace poco y todavía no tiene un saludo suyo.

    Lo que sale de la base son candidatos, como en `seguimiento_canal.py`: la base va
    por detrás de Slack y las reacciones no se guardan. Cada uno se comprueba en vivo
    con `seguimiento_canal.contestado` -- escribió en el hilo de la presentación, o
    reaccionó a ella -- y solo sale si Slack dice claramente que no. Sin lector, o ante
    cualquier duda, no sale ninguno."""
    if reader is None or tope <= 0:
        return []
    filas = db.fetch_all(
        _SQL_PRESENTACIONES,
        {
            "desde": ahora.timestamp() - PRESENTACION_HORAS * 3600,
            "atendidos": list(ESTADOS_ATENDIDOS),
            # Algunos ya estarán saludados: se mira el doble de lo que cabe, no todo.
            "limite": tope * 2,
        },
    )
    if not filas:
        return []
    owner_id = reader.owner_id()
    items: list[Item] = []
    for fila in filas:
        if len(items) >= tope:
            break
        pendiente = seguimiento_canal.Pendiente(
            tipo=seguimiento_canal.HILO,
            channel_id=fila["channel_id"],
            clave_ts=fila["ts"],
            ts=fila["ts"],
            en_hilo=False,
            slack_user_id=fila["slack_user_id"],
        )
        try:
            if seguimiento_canal.contestado(reader, owner_id, pendiente) is not False:
                continue
            nombre = (fila.get("full_name") or "").strip() or seguimiento_canal.nombre_de(
                reader, fila["slack_user_id"]
            )
            if not nombre:
                # Un recordatorio con un código de usuario por nombre es ruido.
                continue
            linea = _linea_presentacion(
                fila, nombre, seguimiento_canal.enlace(reader, pendiente), ahora
            )
        except Exception:
            # Ante la duda, no recordar; y que una falle no se lleva a las demás.
            logger.exception("resumen: no se pudo comprobar una presentación")
            continue
        items.append(
            Item(
                nivel=prioridad.nivel(prioridad.PRESENTACION),
                tipo=prioridad.PRESENTACION,
                desempate=-float(fila["ts"]),
                pintar=lambda linea=linea: linea,
            )
        )
    return items


# ---------------------------------------------------------------------------------
# La credencial del lector
# ---------------------------------------------------------------------------------


def _credencial() -> list[Item]:
    """Si el último sondeo falló porque Slack rechazó la credencial, eso va lo primero:
    mientras siga así el agente no ve nada. Se lee el latido que el worker ya deja en
    cada sondeo (`estado_lector.py`); un fallo pasajero de Slack no cuenta."""
    estado = db_config.value("lector_estado", {})
    if estado.get("ok") is not False or estado.get("error") != "SlackAuthFailed":
        return []
    return [
        Item(
            nivel=prioridad.nivel(prioridad.CREDENCIAL),
            tipo=prioridad.CREDENCIAL,
            desempate=0.0,
            pintar=lambda: "• " + aviso_lector._texto(),
        )
    ]


# ---------------------------------------------------------------------------------
# Juntarlo, ordenarlo y pintarlo
# ---------------------------------------------------------------------------------


def pendientes(now: datetime | None = None, reader=None) -> list[Item]:
    """Todo lo pendiente, de lo más urgente a lo menos. Cada fuente va por separado: si
    una falla (Slack no contesta, una consulta rota), el resumen sale con las demás."""
    ahora = now or datetime.now(UTC)
    tope = max(0, int(db_config.value("resumen_prioridad_max_por_nivel", MAX_POR_NIVEL)))
    fuentes: tuple[tuple[str, Callable[[], list[Item]]], ...] = (
        ("credencial", _credencial),
        ("personas", lambda: _personas(ahora)),
        ("seguimiento", lambda: _seguimientos(ahora, reader)),
        ("presentaciones", lambda: _presentaciones(ahora, reader, tope)),
    )
    items: list[Item] = []
    for nombre, fuente in fuentes:
        try:
            items += fuente()
        except Exception:
            logger.exception("resumen: la fuente %s falló; sale sin ella", nombre)
    return sorted(
        items,
        key=lambda i: (prioridad.orden(i.nivel), PESOS.get(i.tipo, len(PESOS)), i.desempate),
    )


def agrupar(items: list[Item], tope: int) -> list[Grupo]:
    """Los grupos del resumen, uno por nivel con algo dentro, en orden de urgencia. Solo
    se pintan las `tope` primeras líneas de cada uno; una línea que no se puede pintar
    (o que no cabría en un bloque) se cae sola y no arrastra a las demás."""
    grupos: list[Grupo] = []
    for nivel in prioridad.NIVELES:
        del_nivel = [i for i in items if i.nivel == nivel]
        lineas: list[str] = []
        mostrados: list[Item] = []
        for item in del_nivel[: max(0, tope)]:
            try:
                linea = item.pintar()
            except Exception:
                logger.exception("resumen: no se pudo pintar una línea de %s", item.tipo)
                continue
            if not linea or len(linea) > BLOQUE_CHARS:
                continue
            lineas.append(linea)
            mostrados.append(item)
        if lineas:
            grupos.append(Grupo(nivel, tuple(lineas), len(del_nivel), tuple(mostrados)))
    return grupos


def _secciones(titulo: str, lineas: list[str]) -> list[dict]:
    """Un nivel en uno o más bloques `section`, sin pasar del tope de Slack en ninguno."""
    bloques: list[dict] = []
    actual = titulo
    for linea in lineas:
        if len(actual) + 1 + len(linea) > BLOQUE_CHARS:
            bloques.append({"type": "section", "text": {"type": "mrkdwn", "text": actual}})
            actual = linea
        else:
            actual += "\n" + linea
    bloques.append({"type": "section", "text": {"type": "mrkdwn", "text": actual}})
    return bloques


def bloques(grupos: list[Grupo]) -> list[dict]:
    """Los bloques de Slack: la cabecera, un nivel detrás de otro y un pie que dice cómo
    se vacía la lista."""
    salida = [{"type": "section", "text": {"type": "mrkdwn", "text": CABECERA}}]
    for grupo in grupos:
        titulo = (
            f"{prioridad.marca(grupo.nivel)} — {prioridad.EXPLICACION[grupo.nivel]} ({grupo.total})"
        )
        lineas = list(grupo.lineas)
        resto = grupo.total - len(lineas)
        if resto > 0:
            lineas.append(f"_+{resto} more not shown_")
        salida += _secciones(titulo, lineas)
    pie = PIE
    panel = _enlace_panel("/", "Open the panel")
    if panel:
        pie += f" {panel} to see everything."
    salida.append({"type": "context", "elements": [{"type": "mrkdwn", "text": pie}]})
    return salida


def resumen_texto(grupos: list[Grupo]) -> str:
    """El texto de la notificación del móvil: cuántos de cada nivel, nunca quién."""
    nombres = {prioridad.AHORA: "act now", prioridad.HOY: "today", prioridad.FYI: "FYI"}
    return "Daily brief: " + " · ".join(f"{g.total} {nombres[g.nivel]}" for g in grupos)


# ---------------------------------------------------------------------------------
# Publicarlo: una vez al día, reservando antes
# ---------------------------------------------------------------------------------


def _registrar(action: str, payload: dict) -> None:
    try:
        ledger.record_action(action, payload)
    except Exception:
        logger.exception("resumen: no se pudo registrar %s en el ledger", action)


def _parada(exc: Exception, ahora: datetime) -> None:
    """Una parada del sistema se anota una vez al día, no en cada pasada del bucle."""
    logger.warning("resumen del día detenido: %s", exc)
    inicio = ahora.astimezone(_zona()).replace(hour=0, minute=0, second=0, microsecond=0)
    ya = db.fetch_one(
        "select 1 as hay from agent_actions where action = %s and created_at >= %s limit 1",
        (ACCION_PARADA, inicio),
    )
    if ya is None:
        _registrar(ACCION_PARADA, {"motivo": f"{type(exc).__name__}: {exc}"})


def _decidido(dia: date) -> bool:
    """Si el día ya no admite otro intento: salió, se cerró vacío, está saliendo ahora
    mismo, o falló demasiadas veces."""
    fila = db.fetch_one("select estado, intentos from resumenes_prioridad where dia = %s", (dia,))
    if fila is None:
        return False
    return fila["estado"] != "fallido" or fila["intentos"] >= MAX_INTENTOS


def _cerrar_vacio(dia: date) -> None:
    db.execute(
        "insert into resumenes_prioridad (dia, estado, intentos) values (%s, 'vacio', 0) "
        "on conflict (dia) do nothing",
        (dia,),
    )


def _reservar(dia: date) -> bool:
    """Se queda el día ANTES de publicar. Una sola sentencia: si otro worker ya lo
    tiene, o ya salió, no devuelve fila y aquí no se publica nada. Solo un día
    `fallido` con intentos de sobra se puede volver a reservar."""
    return (
        db.fetch_one(
            """
            insert into resumenes_prioridad (dia, estado, intentos) values (%(dia)s, 'reservado', 1)
            on conflict (dia) do update
                set estado = 'reservado',
                    intentos = resumenes_prioridad.intentos + 1,
                    actualizado_at = now()
              where resumenes_prioridad.estado = 'fallido'
                and resumenes_prioridad.intentos < %(tope)s
            returning dia
            """,
            {"dia": dia, "tope": MAX_INTENTOS},
        )
        is not None
    )


def _cuentas(grupos: list[Grupo]) -> dict[str, int]:
    return {
        nivel: sum(len(g.lineas) for g in grupos if g.nivel == nivel) for nivel in prioridad.NIVELES
    }


def _publicar(now: datetime | None, reader) -> int:
    ahora = now or datetime.now(UTC)
    if not activo():
        return 0
    local = ahora.astimezone(_zona())
    if local.hour < db_config.value("resumen_prioridad_hora", HORA):
        return 0
    dia = local.date()
    if _decidido(dia):
        return 0
    if seguimiento._ya_hoy(ahora):
        # El día en que se enciende: si "FOLLOW-UPS" ya salió hoy, este sería el segundo
        # resumen del día. Empieza mañana.
        return 0
    try:
        guards.check_kill_switch()
        guards.check_monthly_budget()
    except (guards.KillSwitchActive, guards.MonthlyBudgetExceeded) as exc:
        _parada(exc, ahora)
        return 0

    settings = load_settings()
    if not (settings.handoff_bot_token and settings.handoff_channel_id):
        # Sin bot o sin canal no hay dónde publicar; el día no se gasta.
        logger.info("resumen del día sin configurar (faltan HANDOFF_SLACK_*), se omite")
        return 0

    tope = max(0, int(db_config.value("resumen_prioridad_max_por_nivel", MAX_POR_NIVEL)))
    grupos = agrupar(pendientes(ahora, reader), tope)
    if not grupos:
        _cerrar_vacio(dia)
        return 0
    if not _reservar(dia):
        return 0

    cuentas = _cuentas(grupos)
    try:
        canal, ts = slack_writer.post_card(bloques(grupos), resumen_texto(grupos))
    except Exception as exc:
        logger.exception("resumen del día: no se pudo publicar")
        try:
            db.execute(
                "update resumenes_prioridad set estado = 'fallido', actualizado_at = now() "
                "where dia = %s and estado = 'reservado'",
                (dia,),
            )
        except Exception:
            # Se queda `reservado`: hoy ya no sale. Peor sería arriesgar un duplicado.
            logger.exception("resumen del día: no se pudo liberar la reserva de %s", dia)
        _registrar(ACCION_FALLIDO, {"dia": str(dia), "motivo": f"{type(exc).__name__}: {exc}"})
        return 0

    try:
        db.execute(
            "update resumenes_prioridad set estado = 'publicado', ahora = %s, hoy = %s, "
            "fyi = %s, channel_id = %s, message_ts = %s, actualizado_at = now() where dia = %s",
            (
                cuentas[prioridad.AHORA],
                cuentas[prioridad.HOY],
                cuentas[prioridad.FYI],
                canal,
                ts,
                dia,
            ),
        )
    except Exception:
        # Ya salió a Slack: la fila se queda `reservado` y sigue bloqueando un segundo
        # resumen, que es lo que importa.
        logger.exception("resumen del día: publicado (ts=%s) pero no se pudo anotar", ts)
    _registrar(ACCION_PUBLICADO, {"dia": str(dia), **cuentas, "canal": canal, "ts": ts})
    # Los casos del seguimiento que salieron quedan avisados exactamente igual que si
    # hubieran salido en "FOLLOW-UPS": la misma función, las mismas marcas.
    seguimiento._marcar([i.caso for g in grupos for i in g.items if i.caso is not None])
    return sum(cuentas.values())


def publicar(now: datetime | None = None, reader=None) -> int:
    """Publica el resumen de hoy si toca. Devuelve cuántas líneas salieron. `reader` (el
    lector del Founders Club) solo se usa para leer: sin él salen las personas y los
    mensajes directos, pero no los hilos, las menciones ni las presentaciones. Nunca
    lanza: un resumen no puede tumbar el bucle."""
    try:
        return _publicar(now, reader)
    except Exception:
        logger.exception("resumen del día: fallo inesperado; se reintenta en la pasada siguiente")
        return 0
