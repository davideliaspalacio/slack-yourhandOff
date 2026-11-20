"""El enlace que abre un mensaje del Founders Club dentro de la app de Slack (0041).

Casi todos los avisos enlazan al mensaje original con el enlace web que da
`chat.getPermalink` (`https://<workspace>.slack.com/archives/C…/p…`). En el ordenador
abre el navegador y lleva al mensaje, y así se queda. En el teléfono no: AG toca el
enlace, cae en una página del navegador y, cuando salta a Slack, la app se abre sin
llegar al mensaje. Por eso cada aviso lleva, JUNTO al enlace web, un segundo enlace
"Open in app" con el esquema `slack://`, que la app recibe directamente.

**El formato vive aquí y solo aquí** (`_url`). No se pudo probar en un teléfono al
escribirlo: si la prueba real dice que hay que cambiarlo, se cambia en este módulo y
cambia en todos los avisos.

    slack://channel?team=<T…>&id=<C…>&message=<ts>[&thread_ts=<ts del principal>]

Qué es oficial y qué no (https://docs.slack.dev/interactivity/deep-linking):

- `slack://channel?team={TEAM_ID}&id={CHANNEL_ID}` SÍ está documentado, para iPhone,
  Android, Mac y Windows; `team` es obligatorio y van códigos, no nombres. Por sí solo
  abre el canal (o el DM) en el workspace correcto.
- `message` y `thread_ts` NO están en la documentación: no documenta ningún parámetro
  para abrir un mensaje concreto (para eso remite a `chat.getPermalink`, que es el
  enlace web que ya llevamos). Son los que usan las herramientas de la comunidad que
  convierten un enlace web en uno de app. Si la app los ignora, el enlace abre igual el
  canal correcto, que ya es más de lo que hacía el enlace web en el teléfono.

Por qué es un enlace de texto y nunca un botón: la referencia del botón de Block Kit
dice que `url` es "a URL to load in the user's browser" y no dice que acepte otros
esquemas. Si Slack rechazara un botón con `slack://`, rechazaría el aviso entero
(`invalid_blocks`) y se perdería; un enlace `<slack://…|texto>` dentro de `mrkdwn` no
puede tumbar la publicación.

Hace falta el código del workspace del Founders Club (`team_id`). Sale de `auth.test`
(que ya estaba en `READ_METHODS`: ningún método nuevo) y se guarda en `config`
(`founders_club_team_id`) para que lo tenga también quien pinta avisos sin lector. El
worker lo refresca de tarde en tarde (`recordar`), no en cada aviso.

Nada de esto puede romper un aviso: sin `team_id`, con el interruptor apagado
(`enlace_app_activo`), con un enlace que no se entiende o con la base caída, las
funciones devuelven None y el aviso sale solo con el enlace web, como antes.

Solo sirve para enlaces del Founders Club. Los enlaces a las tarjetas del Slack de
Handoff no pasan por aquí: son de otro workspace (el `team_id` sería el equivocado) y
dentro de su propia app ya abren bien.
"""

from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import parse_qs, urlparse

from .. import db, db_config

logger = logging.getLogger(__name__)

TEAM_ID_KEY = "founders_club_team_id"
ACTIVO_KEY = "enlace_app_activo"

# Lo que lee el equipo. En inglés, como todo lo que sale del agente.
TEXTO = "Open in app"
PISTA = "📱 On your phone?"

# Cada cuánto vuelve el worker a preguntar el código del workspace. No cambia salvo que
# se pegue una credencial de otro workspace, y entonces basta con enterarse ese día.
REFRESCO_SEGUNDOS = 6 * 3600

# Todo lo que entra en la URL se comprueba contra estos patrones: solo letras
# mayúsculas, dígitos y un punto. Así el enlace no puede llevar un '|', un '<' ni un
# '>' que rompan `<url|texto>`, venga de donde venga el enlace web.
_TEAM = re.compile(r"^[TE][A-Z0-9]{2,20}$")
_CANAL = re.compile(r"^[CDG][A-Z0-9]{2,20}$")
_TS = re.compile(r"^\d{1,12}\.\d{6}$")
_RUTA = re.compile(r"^/archives/([CDG][A-Z0-9]{2,20})/p(\d{7,18})$")

_ultimo_refresco: float | None = None


def activo() -> bool:
    """El interruptor de Settings. Encendido salvo que la base diga `false`; si la base
    no contesta, apagado: sin este enlace el aviso es exactamente el de antes."""
    try:
        return db_config.value(ACTIVO_KEY, True) is True
    except Exception:
        logger.exception("enlace de app: no se pudo leer el interruptor; los avisos salen sin él")
        return False


def team_id() -> str | None:
    """El código del workspace del Founders Club que dejó el worker, o None."""
    try:
        valor = db_config.value(TEAM_ID_KEY, "")
    except Exception:
        logger.exception("enlace de app: no se pudo leer el workspace; los avisos salen sin él")
        return None
    return valor if _TEAM.match(valor) else None


def recordar(reader, clock=time.monotonic) -> None:
    """Deja en `config` el código del workspace del lector. Una llamada a `auth.test`
    por proceso y luego una cada `REFRESCO_SEGUNDOS`; nunca una por aviso. Nunca lanza:
    sin el código, los avisos salen sin enlace de app y se reintenta más tarde."""
    global _ultimo_refresco
    ahora = clock()
    if _ultimo_refresco is not None and ahora - _ultimo_refresco < REFRESCO_SEGUNDOS:
        return
    try:
        valor = reader.team_id()
        if not (isinstance(valor, str) and _TEAM.match(valor)):
            logger.warning("enlace de app: auth.test no dio un workspace válido")
            return
        db.execute(
            """
            insert into config (key, value, updated_at) values (%s, %s::jsonb, now())
            on conflict (key) do update set value = excluded.value, updated_at = now()
             where config.value is distinct from excluded.value
            """,
            (TEAM_ID_KEY, json.dumps(valor)),
        )
        _ultimo_refresco = ahora
    except Exception:
        logger.warning("enlace de app: no se pudo anotar el workspace; se reintenta", exc_info=True)


def _partes(permalink: str | None) -> tuple[str, str, str | None] | None:
    """(canal, ts, ts del principal o None) de un enlace web de Slack, o None si no lo es.

    `…/archives/C123/p1700000000123456` es un mensaje; con `?thread_ts=…` es una
    respuesta dentro de ese hilo (así lo describe `chat.getPermalink`)."""
    if not permalink or not isinstance(permalink, str):
        return None
    try:
        partes = urlparse(permalink)
    except ValueError:
        return None
    if partes.scheme != "https" or not (partes.hostname or "").endswith(".slack.com"):
        return None
    ruta = _RUTA.match(partes.path.rstrip("/"))
    if not ruta:
        return None
    canal, digitos = ruta.groups()
    ts = f"{digitos[:-6]}.{digitos[-6:]}"
    hilo = (parse_qs(partes.query).get("thread_ts") or [None])[0]
    return canal, ts, hilo if hilo and _TS.match(hilo) and hilo != ts else None


def _url(team: str, canal: str, ts: str | None = None, hilo: str | None = None) -> str:
    """EL formato. Único sitio que hay que tocar si la prueba en el teléfono lo pide."""
    url = f"slack://channel?team={team}&id={canal}"
    if ts:
        url += f"&message={ts}"
    if hilo:
        url += f"&thread_ts={hilo}"
    return url


def de_mensaje(permalink: str | None) -> str | None:
    """El enlace de app al mismo mensaje que `permalink` (un enlace web del Founders
    Club), en su hilo si es una respuesta. None si no se puede o no se debe montar."""
    partes = _partes(permalink)
    if partes is None or not activo():
        return None
    team = team_id()
    return _url(team, *partes) if team else None


def de_conversacion(team: str | None, canal: str | None) -> str | None:
    """El enlace de app a una conversación entera (un DM), con el workspace que ya
    guarda el rastro. Este es el formato documentado tal cual, sin parámetros de más."""
    if not (team and canal and _TEAM.match(team) and _CANAL.match(canal)) or not activo():
        return None
    return _url(team, canal)


def mrkdwn(url: str | None) -> str | None:
    """`<slack://…|Open in app>`, para ir detrás de un enlace web dentro de una línea."""
    return f"<{url}|{TEXTO}>" if url else None


def con_app(enlace_web: str, url: str | None) -> str:
    """Un enlace web ya montado (`<https://…|Open in Slack>`) seguido del de la app, o
    él solo si no hay enlace de app."""
    app = mrkdwn(url)
    return f"{enlace_web} · {app}" if app else enlace_web


def bloque(url: str | None) -> dict | None:
    """El enlace de app como bloque de contexto, para ir debajo de los botones de un
    aviso cuyo enlace web es un botón. None si no hay enlace."""
    app = mrkdwn(url)
    if not app:
        return None
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": f"{PISTA} {app}"}]}
