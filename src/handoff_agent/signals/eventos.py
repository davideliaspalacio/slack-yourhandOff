"""Dos avisos de canal que se detectan sin preguntarle al modelo (migración 0030).

- **Mención**: alguien escribió el id de la cuenta lectora en un mensaje. Es una
  búsqueda de cadena sobre lo que el watcher ya guardó.
- **Presentación**: un mensaje nuevo en el canal de presentaciones. Lo que se publica
  ahí es, por definición, gente presentándose.

Por eso están aquí y no en `signals/procesar.py`: ahí cada fila es el resultado de
preguntarle al modelo, con su coste y su confianza. En estos dos no hay nada que
interpretar para saber que pasaron, así que detectarlos es gratis y salen en la misma
vuelta.

Cada tipo tiene su propio aviso, con su cabecera y su forma (0037), para que se
distingan de un vistazo en el canal y en la notificación del móvil:

- La **mención** es corta: quién, dónde, la cita y el enlace para contestar.
- La **presentación** es una llamada a la acción: saludar. Lleva la cita, un saludo
  sugerido listo para copiar (`delivery/saludo.py`; es lo único de aquí que llama al
  modelo, tiene su interruptor y el aviso sale igual sin él), el recordatorio de
  conectar en LinkedIn (`delivery/linkedin.py`) y el botón que abre la presentación.

Nada de esto escribe en el Founders Club: el saludo es texto para que Anthony lo copie.

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
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .. import db, db_config, ledger
from ..config import load_settings
from ..delivery import card, linkedin, prioridad, saludo, slack_writer
from ..ingest.profile_hints import company_from_title
from ..slack_client import SlackUnavailable
from . import procesar

logger = logging.getLogger(__name__)

MENCION = "mencion"
PRESENTACION = "presentacion"

ACCION_PUBLICADO = "evento_publicado"
ACCION_FALLIDO = "evento_fallido"

CABECERAS = {
    MENCION: "💬 *YOU WERE MENTIONED*",
    PRESENTACION: "👋 *NEW INTRODUCTION · say hi*",
}
# Lo que se lee en la notificación del móvil, antes de abrir Slack: también ahí cada
# tipo tiene que reconocerse solo.
RESUMENES = {
    MENCION: "Mention: {quien} tagged you in {canal}",
    PRESENTACION: "New introduction: {quien} — say hi",
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
    """Apunta los eventos de los mensajes recientes que todavía no se habían mirado.

    No publica: solo deja las filas en `pendiente`. Devuelve cuántas creó.

    **Solo mira lo reciente**, y no es un detalle: `slack_messages` lleva meses de
    histórico de los canales vigilados, así que sin este corte la primera pasada
    anunciaría como recién llegadas decenas de presentaciones y menciones de hace
    semanas. Es la misma regla que `signals/procesar.py` aplica a las señales y que
    `ingest/watcher.py` aplica a la lectura ("no backfill, on purpose"): avisar de algo
    que pasó hace días no le sirve a nadie, y hacerlo a montones es peor que no avisar,
    porque se dejan de leer los avisos. El corte es `alertas_edad_maxima_horas`, el
    mismo que decide hasta cuándo una señal merece alerta.

    Los mensajes del dueño quedan fuera por dos vías: el watcher los marca `ignorado`
    y además se exige que el autor no sea él -- mencionarse a sí mismo o presentarse
    en su propio club no es un aviso.
    """
    canal_presentaciones = str(db_config.value("canal_presentaciones", "") or "").strip().upper()
    desde = (datetime.now(UTC) - timedelta(hours=procesar.edad_maxima_horas())).timestamp()
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
           and sm.ts::numeric >= %(desde)s
           and position(%(aguja)s in coalesce(sm.text, '')) > 0
           and (p.state is null or p.state <> 'descartado')
        on conflict (channel_id, ts, tipo) do nothing
        """,
        {"tipo": MENCION, "dueno": owner_id, "aguja": f"<@{owner_id}>", "desde": desde},
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
               -- Solo mensajes principales. Desde que se leen los hilos (0032), en este
               -- canal también hay respuestas, y una respuesta a una presentación es un
               -- "Welcome Dajana!", no alguien presentándose: contarlas así publicó 20
               -- avisos falsos la primera noche. Las menciones sí miran las respuestas:
               -- etiquetar a Anthony dentro de un hilo es una mención de verdad.
               and (sm.thread_ts is null or sm.thread_ts = sm.ts)
               and sm.ts::numeric >= %(desde)s
               and btrim(coalesce(sm.text, '')) <> ''
               and (p.state is null or p.state <> 'descartado')
            on conflict (channel_id, ts, tipo) do nothing
            """,
            {
                "tipo": PRESENTACION,
                "dueno": owner_id,
                "canal": canal_presentaciones,
                "desde": desde,
            },
        )
    return creados


def _pendientes(tipo: str, limite: int) -> list[dict]:
    return db.fetch_all(
        """
        select e.id::text as id, e.channel_id, e.ts, e.tipo, e.slack_user_id,
               e.prospect_id::text as prospect_id, e.saludo,
               sm.text, c.nombre as canal_nombre,
               p.full_name, p.company_name, p.company_name_override, p.research_links
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


def identidad(fila: dict, reader) -> tuple[str | None, str | None]:
    """El nombre y la empresa de quien escribió, o None lo que no se sepa.

    Primero lo que ya hay en su ficha. Quien se presenta suele ser gente nueva, sin
    ficha todavía (la crea el research, minutos después): para ellos se lee su perfil de
    Slack, que es una lectura (`users.info`) y la misma de la que el research saca el
    nombre. Sin eso el aviso solo podía enseñar un código de usuario, y no había nombre
    con el que saludar ni con el que buscar en LinkedIn. Si Slack no contesta, el aviso
    sale igual con lo que haya.
    """
    nombre = (fila.get("full_name") or "").strip() or None
    empresa = (fila.get("company_name_override") or fila.get("company_name") or "").strip() or None
    if nombre or not fila.get("slack_user_id"):
        return nombre, empresa
    try:
        perfil = reader.user_profile(fila["slack_user_id"])
    except Exception:  # noqa: BLE001 - el nombre es un extra del aviso
        # Sin perfil (Slack caído, usuario borrado, un lector que no sabe leerlo): no es
        # motivo para perder el aviso.
        logger.warning("sin perfil de Slack para %s; el aviso sale sin él", fila["slack_user_id"])
        return nombre, empresa
    nombre = (perfil.real_name or "").strip() or None
    return nombre, empresa or company_from_title(perfil.title)


def visible(fila: dict, nombre: str | None) -> str:
    """El nombre tal como puede entrar en un aviso: sale de un perfil ajeno, así que se
    escapa y se recorta como en las tarjetas. Sin nombre, el código de usuario."""
    return card._escape_and_cap(nombre or fila.get("slack_user_id") or "Someone", card.NAME_CHARS)


def quien(fila: dict, nombre: str | None, empresa: str | None) -> str:
    """`*Nombre* — Empresa`, escapado."""
    empresa_visible = card._escape_and_cap(empresa, card.COMPANY_CHARS)
    nombre_visible = visible(fila, nombre)
    return f"*{nombre_visible}* — {empresa_visible}" if empresa_visible else f"*{nombre_visible}*"


def dossier_de(prospect_id: str | None) -> dict:
    """El último dossier de la persona, o {} si no tiene ficha o aún no se investigó."""
    if not prospect_id:
        return {}
    fila = db.fetch_one(
        "select content from dossiers where prospect_id = %s order by version desc limit 1",
        (prospect_id,),
    )
    contenido = fila["content"] if fila else None
    return contenido if isinstance(contenido, dict) else {}


def seccion(texto: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": texto}}


def _enlace(fila: dict, reader) -> str | None:
    try:
        enlace = reader.permalink(fila["channel_id"], fila["ts"])
    except SlackUnavailable:
        # El aviso vale sin enlace: lo importante es que AG sepa que pasó.
        logger.warning("evento %s: sin enlace permanente", fila["id"])
        return None
    return enlace if card._is_safe_link(enlace) else None


def _cita(fila: dict) -> str | None:
    cita = (fila.get("text") or "").strip()
    # El mismo renderizado que las tarjetas: dentro de un código en línea, para que
    # Slack no autoenlace una URL que alguien haya metido en su mensaje.
    return card.quote(cita) if cita else None


def _aviso_mencion(fila: dict, reader) -> tuple[list[dict], str]:
    """La mención: corta, porque lo único que hay que hacer es ir a contestar."""
    from .. import canales

    nombre, empresa = identidad(fila, reader)
    canal = canales.etiqueta(fila["channel_id"], fila.get("canal_nombre"))
    # Cuánto corre (0039): una mención de un lead caliente es "act now"; de cualquier
    # otro, "today". Lo decide `prioridad.py`.
    nivel = prioridad.visible(
        prioridad.nivel(prioridad.MENCION, caliente=prioridad.es_caliente(fila.get("prospect_id")))
    )
    partes = [
        prioridad.cabecera(CABECERAS[MENCION], nivel),
        f"{quien(fila, nombre, empresa)} · in {canal}",
    ]
    cita = _cita(fila)
    if cita:
        partes.append(cita)
    bloques = [seccion("\n".join(partes))]
    enlace = _enlace(fila, reader)
    if enlace:
        pie = f"They tagged you · <{enlace}|Open in Slack> to reply"
        bloques.append({"type": "context", "elements": [{"type": "mrkdwn", "text": pie}]})
    texto = RESUMENES[MENCION].format(quien=visible(fila, nombre), canal=canal)
    return bloques, prioridad.resumen(texto, nivel)


def _bloque_saludo(texto: str) -> str:
    """El saludo como bloque de código: se copia de un gesto y Slack no autoenlaza ni
    interpreta nada dentro. Ya pasó la validación de `saludo.py`; aun así se escapa
    (Slack decodifica las entidades también ahí) y un acento grave pasa a apóstrofo para
    que no pueda cerrar el bloque antes de tiempo, igual que en `card.quote`. El tope es
    el del saludo ya escapado: 400 caracteres por 5 en el peor caso, lejos de los 3000
    de un bloque."""
    return "```" + card._escape_and_cap(texto.replace("`", "'"), saludo.MAX_SALUDO * 5) + "```"


def _aviso_presentacion(fila: dict, reader) -> tuple[list[dict], str]:
    """La presentación: qué dijo, qué hacer (saludar), con qué palabras y dónde seguir
    (LinkedIn). Cada cosa en su bloque, para que el saludo se copie solo."""
    from .. import canales

    nombre, empresa = identidad(fila, reader)
    dossier = dossier_de(fila.get("prospect_id"))
    canal = canales.etiqueta(fila["channel_id"], fila.get("canal_nombre"))

    # Saludar vale hoy (0039): pasado mañana ya no es una bienvenida.
    nivel = prioridad.visible(prioridad.nivel(prioridad.PRESENTACION))
    partes = [
        prioridad.cabecera(CABECERAS[PRESENTACION], nivel),
        quien(fila, nombre, empresa),
        f"introduced themselves in {canal}",
    ]
    cita = _cita(fila)
    if cita:
        partes.append(cita)
    bloques = [seccion("\n".join(partes))]

    sugerido = saludo.sugerir(fila, nombre, empresa, dossier)
    if sugerido:
        bloques.append(
            seccion(
                "👉 *Say hi* — reply in their intro thread. "
                "_Suggested greeting (copy, tweak, send):_"
            )
        )
        bloques.append(seccion(_bloque_saludo(sugerido)))
    else:
        bloques.append(seccion("👉 *Say hi* — reply in their intro thread to welcome them."))

    conectar = linkedin.linea(dossier, nombre, empresa, fila.get("research_links"))
    if conectar:
        bloques.append(seccion(conectar))

    enlace = _enlace(fila, reader)
    if enlace:
        # Un botón de enlace: el receptor (`web/app.py`) los ignora, solo abren la URL.
        bloques.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "ver_original",
                        "text": {"type": "plain_text", "text": "Open intro in Slack"},
                        "url": enlace,
                        "style": "primary",
                    }
                ],
            }
        )
    texto = RESUMENES[PRESENTACION].format(quien=visible(fila, nombre), canal=canal)
    return bloques, prioridad.resumen(texto, nivel)


_AVISOS = {MENCION: _aviso_mencion, PRESENTACION: _aviso_presentacion}


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
        try:
            # El montaje también va dentro del `try`: si algo falla al construir el
            # aviso, la reserva se libera igual que si fallara Slack.
            bloques, resumen = _AVISOS[tipo](fila, reader)
            _, ts = slack_writer.post_card(bloques, resumen)
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
