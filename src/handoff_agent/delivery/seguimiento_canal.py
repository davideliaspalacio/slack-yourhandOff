"""Lo que espera respuesta de él en los canales: hilos y menciones (migración 0038).

El seguimiento (`delivery/seguimiento.py`) solo miraba los mensajes directos. Aquí se
calcula lo que se le escapaba en los canales, y `seguimiento.py` lo pinta y lo publica
con sus mismas cabeceras, su mismo cupo y sus mismos frenos. Todo es por reglas: ni una
llamada al modelo, y del texto de los mensajes no se lee ni se cita nada -- solo quién
escribió, dónde y cuándo.

Qué cuenta como pendiente
-------------------------

**Hilo** (necesita `leer_hilos`, 0032: sin él las respuestas no llegan a la base):

- El hilo lo abrió él: cualquier respuesta de una persona posterior a su último mensaje
  en ese hilo.
- El hilo lo abrió otro y él escribió dentro: solo cuenta que le conteste QUIEN ABRIÓ el
  hilo. Slack no dice a quién responde cada respuesta, y en un hilo ajeno lo normal es
  que los demás hablen entre ellos: el "Welcome!" número veinte debajo del suyo no es
  nadie esperándole. Quien abrió el hilo sí es, casi siempre, la persona a la que él le
  estaba hablando. Si un tercero quiere algo de él, lo mencionará, y eso es el caso de
  abajo.

El plazo (`seguimiento_hilo_horas`) corre desde la PRIMERA respuesta sin contestar, no
desde la última: un hilo que sigue vivo no puede aplazar el aviso para siempre.

**Mención**: una fila `mencion` de `eventos_canal` (0030; aquí solo se lee) de hace más
de `seguimiento_mencion_horas`. Se da por contestada si después de ella:

- escribió en ese hilo (el hilo donde está la mención, o el que cuelga de ella), o
- la mención no está en ningún hilo y escribió un mensaje principal en ese canal, o
- reaccionó a ella con un emoji: contestar con un 👍 es contestar, igual que en los
  mensajes directos (0033).

Lo segundo es generoso a propósito -- un mensaje suyo en el canal tres horas después
puede no tener nada que ver --, pero la duda se resuelve siempre hacia no avisar.

Ante la duda, no se avisa
-------------------------

Un recordatorio de algo que ya contestó hace que deje de mirar el canal. Y la base va
por detrás de Slack: los hilos se traen con tope y por barridos, los canales rotan, y
las reacciones no se guardan nunca. Por eso lo que sale de la base son solo candidatos:
antes de publicar cada uno se comprueba EN VIVO contra Slack (`contestado`), con
métodos de lectura que ya estaban en la lista blanca. Si ya contestó se apunta
(`resuelto_ts`) y no vuelve; si Slack no contesta o la respuesta no es concluyente, no
se avisa en esta pasada.

Con `leer_hilos` apagado no hay casos de hilo ni menciones dentro de hilos, y no se
llama a `conversations.replies` (el interruptor es justo eso). Las menciones en
mensajes principales siguen, comprobadas con el propio mensaje (`reply_users` dice si
él contestó en el hilo sin abrirlo).

Además: nunca sobre alguien descartado o en modo de pruebas, nunca sobre un bot o un
mensaje de sistema (el watcher los guarda como `ignorado`), nada anterior a la última
vez que se tocó el interruptor (ver la migración: lo viejo no se anuncia), nada de más
de `seguimiento_caduca_dias`, y como mucho `seguimiento_canal_max_avisos` avisos
inmediatos por hilo o mención. Después solo queda en el resumen del día.

La reserva (`reservar`) se hace antes de publicar y es un único UPDATE condicional: dos
workers, o un reintento, no pueden avisar dos veces de lo mismo.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from .. import db, db_config
from ..slack_client import SlackUnavailable

logger = logging.getLogger(__name__)

HILO = "hilo"
MENCION = "mencion"

# Valores por omisión, los mismos que la migración 0038 deja en config.
HORAS_HILO = 24
HORAS_MENCION = 4
MAX_AVISOS = 3
CADUCA_DIAS = 7
# La contabilidad de un hilo del que ya nadie se acuerda se borra sola.
RETENCION_DIAS = 90


@dataclass(frozen=True)
class Pendiente:
    """Un hilo o una mención que espera respuesta suya. Sin texto: no hace falta."""

    tipo: str
    channel_id: str
    # El hilo (el `ts` de su principal) o, si la mención no está en ninguno, su `ts`.
    clave_ts: str
    # Lo que espera respuesta: la primera respuesta sin contestar, o la mención.
    ts: str
    # Si eso que espera es una respuesta dentro de un hilo.
    en_hilo: bool
    slack_user_id: str | None = None
    nombre: str | None = None
    empresa: str | None = None
    prospect_id: str | None = None
    canal_nombre: str | None = None
    # Solo hilos: si el hilo lo abrió él, y cuántas respuestas más esperan detrás.
    hilo_propio: bool = False
    mas: int = 0
    # Lo que ya hay apuntado de este hilo o mención, para decidir si toca avisar.
    avisado_ts: str | None = None
    avisado_at: datetime | None = None
    avisos: int = 0


def leer_hilos() -> bool:
    return db_config.value("leer_hilos", False) is True


def caduca_ts(ahora: datetime) -> float:
    """El `ts` más viejo que todavía se recuerda (`seguimiento_caduca_dias`)."""
    return ahora.timestamp() - db_config.value("seguimiento_caduca_dias", CADUCA_DIAS) * 86400


def desde_ts(ahora: datetime, *interruptores: str) -> float:
    """Desde qué `ts` cuenta algo: lo posterior a la última vez que se tocó cualquiera de
    esos interruptores, y nunca más viejo que la caducidad.

    Es la regla de "lo viejo no se anuncia" (ver 0038). Un interruptor que no existe no
    da marca ninguna, y sin marca no cuenta nada: ante la duda, no avisar."""
    filas = db.fetch_all(
        "select key, updated_at from config where key = any(%s)", (list(interruptores),)
    )
    if len(filas) < len(interruptores):
        return ahora.timestamp()
    marca = max(f["updated_at"].timestamp() for f in filas)
    return max(marca, caduca_ts(ahora))


_SQL_HILOS = """
with mios as (
    -- Los hilos donde él ha escrito (abriéndolo o dentro) y su último mensaje en cada uno.
    -- Un principal guardado antes de tener respuestas lleva `thread_ts` nulo: su hilo es
    -- su propio `ts`.
    select channel_id, coalesce(thread_ts, ts) as hilo, max(ts::numeric) as ultimo_mio
      from slack_messages
     where user_id = %(dueno)s
     group by channel_id, coalesce(thread_ts, ts)
),
esperando as (
    select sm.channel_id, m.hilo, sm.ts, sm.user_id,
           (raiz.user_id = %(dueno)s) as hilo_propio,
           row_number() over (
               partition by sm.channel_id, m.hilo order by sm.ts::numeric
           ) as orden,
           count(*) over (partition by sm.channel_id, m.hilo) as cuantas
      from mios m
      -- Sin el principal no se sabe quién abrió el hilo, y sin saberlo no se avisa.
      join slack_messages raiz on raiz.channel_id = m.channel_id and raiz.ts = m.hilo
      join slack_messages sm
        on sm.channel_id = m.channel_id and sm.thread_ts = m.hilo and sm.ts <> m.hilo
      left join seguimiento_canal sc
        on sc.channel_id = m.channel_id and sc.clave_ts = m.hilo
      left join prospects p on p.slack_user_id = sm.user_id
     where sm.user_id is not null
       and sm.user_id <> %(dueno)s
       -- Bots y avisos de sistema: el watcher los guarda como `ignorado`.
       and sm.status <> 'ignorado'
       and sm.ts::numeric > m.ultimo_mio
       and sm.ts::numeric > %(desde)s
       -- En un hilo ajeno solo cuenta quien lo abrió (ver el docstring del módulo).
       and (raiz.user_id = %(dueno)s or sm.user_id = raiz.user_id)
       and (sc.resuelto_ts is null or sm.ts::numeric > sc.resuelto_ts::numeric)
       and (p.id is null or (p.state <> 'descartado' and not coalesce(p.sin_tarjeta, false)))
)
select e.channel_id, e.hilo as clave_ts, e.ts, e.user_id, e.hilo_propio, e.cuantas,
       true as en_hilo,
       p.id::text as prospect_id, p.full_name, p.company_name, p.company_name_override,
       c.nombre as canal_nombre,
       sc.pendiente_ts, sc.avisado_at, sc.avisos
  from esperando e
  left join prospects p on p.slack_user_id = e.user_id
  left join canales c on c.channel_id = e.channel_id
  left join seguimiento_canal sc on sc.channel_id = e.channel_id and sc.clave_ts = e.hilo
 where e.orden = 1
   and e.ts::numeric <= %(limite)s
 order by e.ts::numeric
"""

_SQL_MENCIONES = """
with menciones as (
    select e.channel_id, e.ts, e.slack_user_id as user_id,
           (sm.thread_ts is not null and sm.thread_ts <> sm.ts) as en_hilo,
           case when sm.thread_ts is not null and sm.thread_ts <> sm.ts
                then sm.thread_ts else sm.ts end as clave_ts
      from eventos_canal e
      join slack_messages sm on sm.channel_id = e.channel_id and sm.ts = e.ts
     where e.tipo = 'mencion'
       and e.estado <> 'omitida'
       and e.slack_user_id is not null
       and e.slack_user_id <> %(dueno)s
       and e.ts::numeric > %(desde)s
       and e.ts::numeric <= %(limite)s
)
select m.channel_id, m.clave_ts, m.ts, m.user_id, m.en_hilo,
       false as hilo_propio, 1 as cuantas,
       p.id::text as prospect_id, p.full_name, p.company_name, p.company_name_override,
       c.nombre as canal_nombre,
       sc.pendiente_ts, sc.avisado_at, sc.avisos
  from menciones m
  left join prospects p on p.slack_user_id = m.user_id
  left join canales c on c.channel_id = m.channel_id
  left join seguimiento_canal sc on sc.channel_id = m.channel_id and sc.clave_ts = m.clave_ts
 where (p.id is null or (p.state <> 'descartado' and not coalesce(p.sin_tarjeta, false)))
   and (sc.resuelto_ts is null or m.ts::numeric > sc.resuelto_ts::numeric)
   -- Con los hilos apagados, de lo que pasa dentro de un hilo no se sabe nada fiable.
   and (%(hilos)s or not m.en_hilo)
   -- Ya escribió después: en ese hilo, o en el canal si no hay hilo.
   and not exists (
       select 1 from slack_messages r
        where r.channel_id = m.channel_id
          and r.user_id = %(dueno)s
          and r.ts::numeric > m.ts::numeric
          and (coalesce(r.thread_ts, r.ts) = m.clave_ts
               or (not m.en_hilo and (r.thread_ts is null or r.thread_ts = r.ts)))
   )
 order by m.ts::numeric
"""


def _pendiente(tipo: str, fila: dict) -> Pendiente:
    return Pendiente(
        tipo=tipo,
        channel_id=fila["channel_id"],
        clave_ts=fila["clave_ts"],
        ts=fila["ts"],
        en_hilo=bool(fila["en_hilo"]),
        slack_user_id=fila["user_id"],
        nombre=(fila.get("full_name") or "").strip() or None,
        empresa=fila.get("company_name_override") or fila.get("company_name"),
        prospect_id=fila.get("prospect_id"),
        canal_nombre=fila.get("canal_nombre"),
        hilo_propio=bool(fila["hilo_propio"]),
        mas=max(0, int(fila["cuantas"]) - 1),
        avisado_ts=fila.get("pendiente_ts"),
        avisado_at=fila.get("avisado_at"),
        avisos=fila.get("avisos") or 0,
    )


def candidatos(owner_id: str, ahora: datetime) -> list[Pendiente]:
    """Lo que, según la base, espera respuesta suya y ya pasó de su plazo. Del más viejo
    al más nuevo, uno por hilo.

    Solo lee, y son CANDIDATOS: la base puede ir por detrás de Slack, así que nada de
    esto se publica sin pasar antes por `contestado`."""
    hilos = leer_hilos()
    por_clave: dict[tuple[str, str], Pendiente] = {}

    if db_config.value("seguimiento_menciones_activo", True) is True:
        horas = db_config.value("seguimiento_mencion_horas", HORAS_MENCION)
        filas = db.fetch_all(
            _SQL_MENCIONES,
            {
                "dueno": owner_id,
                "desde": desde_ts(ahora, "seguimiento_menciones_activo"),
                "limite": ahora.timestamp() - horas * 3600,
                "hilos": hilos,
            },
        )
        for fila in filas:
            # Varias menciones en un mismo hilo son un solo pendiente: la más vieja.
            por_clave.setdefault((fila["channel_id"], fila["clave_ts"]), _pendiente(MENCION, fila))

    if hilos and db_config.value("seguimiento_hilos_activo", True) is True:
        horas = db_config.value("seguimiento_hilo_horas", HORAS_HILO)
        filas = db.fetch_all(
            _SQL_HILOS,
            {
                "dueno": owner_id,
                # También desde que se encendió la lectura de hilos: al encenderla llega
                # de golpe una semana de respuestas viejas.
                "desde": desde_ts(ahora, "seguimiento_hilos_activo", "leer_hilos"),
                "limite": ahora.timestamp() - horas * 3600,
            },
        )
        for fila in filas:
            # Un hilo donde además lo mencionaron sale una vez, como mención.
            por_clave.setdefault((fila["channel_id"], fila["clave_ts"]), _pendiente(HILO, fila))

    return sorted(por_clave.values(), key=lambda p: float(p.ts))


def toca_avisar(pendiente: Pendiente, ahora: datetime, recordar_horas: int) -> bool:
    """Si a este pendiente le toca un aviso inmediato ahora: nunca se avisó por él, o
    pasó el intervalo de recordar y no ha agotado sus avisos. `reservar` lo vuelve a
    comprobar en la base, que es quien manda; esto solo evita preguntarle a Slack por lo
    que de todas formas no iba a salir."""
    tope = db_config.value("seguimiento_canal_max_avisos", MAX_AVISOS)
    if tope <= 0:
        return False
    if pendiente.avisado_ts != pendiente.ts:
        return True
    if pendiente.avisos >= tope:
        return False
    if pendiente.avisado_at is None:
        return True
    return (ahora - pendiente.avisado_at).total_seconds() >= recordar_horas * 3600


def _reacciono(mensaje: dict, owner_id: str) -> bool:
    """Si él puso alguna reacción. Solo se mira quién, nunca cuál (como en 0033)."""
    return any(owner_id in (r.get("users") or []) for r in mensaje.get("reactions") or [])


def _ts(mensaje: dict) -> float:
    try:
        return float(mensaje.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def contestado(reader, owner_id: str, pendiente: Pendiente) -> bool | None:
    """Pregunta a Slack, ahora, si él ya contestó. True = sí (o ya no hay nada que
    contestar: lo borraron); False = sigue pendiente; None = no se pudo saber.

    Quien llama solo avisa con un False. Todo lo que usa es de lectura."""
    objetivo = float(pendiente.ts)
    try:
        if leer_hilos():
            mensajes = reader.respuestas(pendiente.channel_id, pendiente.clave_ts)
            if not any(m.get("ts") == pendiente.ts for m in mensajes):
                return True
            for mensaje in mensajes:
                suyo = mensaje.get("user") == owner_id
                if suyo and _ts(mensaje) > objetivo:
                    return True
                # Reaccionó a lo que le esperaba, o a algo que le dijeron después.
                if not suyo and _ts(mensaje) >= objetivo and _reacciono(mensaje, owner_id):
                    return True
        else:
            if pendiente.en_hilo:
                # Sin leer hilos no hay forma de comprobarlo.
                return None
            mensaje = reader.mensaje(pendiente.channel_id, pendiente.ts)
            if mensaje is None:
                return True
            en_el_hilo = mensaje.get("reply_users") or []
            if owner_id in en_el_hilo or _reacciono(mensaje, owner_id):
                return True
            try:
                total = int(mensaje.get("reply_users_count") or 0)
            except (TypeError, ValueError):
                return None
            if total > len(en_el_hilo):
                # Slack recorta esa lista: él puede estar entre los que no vienen.
                return None
        if pendiente.tipo == MENCION and not pendiente.en_hilo:
            # Mención suelta en el canal: también vale que escribiera después ahí.
            for mensaje in reader.history(pendiente.channel_id, oldest=pendiente.ts):
                if mensaje.get("user") == owner_id and _ts(mensaje) > objetivo:
                    return True
        return False
    except SlackUnavailable as exc:
        logger.info(
            "seguimiento de canal: no se pudo comprobar %s/%s (%s); no se avisa",
            pendiente.channel_id,
            pendiente.ts,
            exc,
        )
        return None


def resolver(pendiente: Pendiente) -> None:
    """Apunta que lo pendiente ya está contestado, para no volver a preguntarlo."""
    db.execute(
        """
        insert into seguimiento_canal (channel_id, clave_ts, tipo, pendiente_ts, resuelto_ts)
        values (%(canal)s, %(clave)s, %(tipo)s, %(ts)s, %(ts)s)
        on conflict (channel_id, clave_ts) do update
            set resuelto_ts = excluded.resuelto_ts, actualizado_at = now()
          where seguimiento_canal.resuelto_ts is null
             or seguimiento_canal.resuelto_ts::numeric < excluded.resuelto_ts::numeric
        """,
        {
            "canal": pendiente.channel_id,
            "clave": pendiente.clave_ts,
            "tipo": pendiente.tipo,
            "ts": pendiente.ts,
        },
    )


def reservar(pendiente: Pendiente, ahora: datetime, recordar_horas: int) -> bool:
    """Gasta un aviso de este pendiente ANTES de publicarlo. False si no toca (otro
    worker se adelantó, aún no pasó el intervalo o ya agotó sus avisos).

    Un `pendiente_ts` distinto del apuntado es un caso nuevo en el mismo hilo -- contestó
    y volvieron a escribirle --, y su cuenta empieza de cero."""
    tope = db_config.value("seguimiento_canal_max_avisos", MAX_AVISOS)
    if tope <= 0:
        return False
    return bool(
        db.execute(
            """
            insert into seguimiento_canal
                   (channel_id, clave_ts, tipo, pendiente_ts, avisado_at, avisos)
            values (%(canal)s, %(clave)s, %(tipo)s, %(ts)s, %(ahora)s, 1)
            on conflict (channel_id, clave_ts) do update
                set tipo = excluded.tipo,
                    pendiente_ts = excluded.pendiente_ts,
                    avisado_at = excluded.avisado_at,
                    avisos = case
                        when seguimiento_canal.pendiente_ts = excluded.pendiente_ts
                        then seguimiento_canal.avisos + 1
                        else 1
                    end,
                    actualizado_at = now()
              where seguimiento_canal.pendiente_ts <> excluded.pendiente_ts
                 or (seguimiento_canal.avisos < %(tope)s
                     and (seguimiento_canal.avisado_at is null
                          or seguimiento_canal.avisado_at
                             <= %(ahora)s - make_interval(hours => %(recordar)s)))
            """,
            {
                "canal": pendiente.channel_id,
                "clave": pendiente.clave_ts,
                "tipo": pendiente.tipo,
                "ts": pendiente.ts,
                "ahora": ahora,
                "tope": tope,
                "recordar": recordar_horas,
            },
        )
    )


def liberar(pendiente: Pendiente) -> None:
    """Devuelve la reserva de un aviso que no llegó a publicarse: sale en la pasada
    siguiente, sin haber gastado ninguno de los suyos."""
    db.execute(
        """
        update seguimiento_canal
           set avisos = greatest(avisos - 1, 0), avisado_at = null, actualizado_at = now()
         where channel_id = %s and clave_ts = %s and pendiente_ts = %s
        """,
        (pendiente.channel_id, pendiente.clave_ts, pendiente.ts),
    )


def limpiar() -> int:
    """Borra la contabilidad que lleva meses sin moverse. Devuelve cuántas filas."""
    return db.execute(
        "delete from seguimiento_canal where actualizado_at < now() - make_interval(days => %s)",
        (RETENCION_DIAS,),
    )


def nombre_de(reader, slack_user_id: str | None) -> str | None:
    """El nombre de alguien sin ficha, preguntado a Slack en el momento (`users.info`) y
    sin guardarlo en ninguna parte. None si no se sabe, si es un bot o si ya no está."""
    if not slack_user_id:
        return None
    try:
        perfil = reader.user_profile(slack_user_id)
    except SlackUnavailable:
        return None
    if perfil.is_bot or perfil.deleted:
        return None
    return (perfil.real_name or "").strip() or None


def enlace(reader, pendiente: Pendiente) -> str | None:
    """El enlace permanente a lo que espera respuesta. None si Slack no lo da: el aviso
    vale sin él."""
    try:
        return reader.permalink(pendiente.channel_id, pendiente.ts)
    except SlackUnavailable:
        return None
