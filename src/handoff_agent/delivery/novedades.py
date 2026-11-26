"""El mensaje de "What's new" en el canal de Handoff después de cada despliegue.

Cada funcionalidad nueva añade aquí una entrada: un `id` que no cambia nunca, un título
y unas pocas líneas en inglés, escritas para Anthony y no para desarrolladores. Cuando
el worker arranca con código nuevo, publica en el canal de tarjetas las entradas que aún
no se habían anunciado, todas en un solo mensaje.

Por qué así y no de otra forma:

- **Lo escribe una persona, no el modelo.** Resumir commits con IA saldría barato, pero
  los commits son técnicos, están en español y un resumen automático puede inventarse
  lo que no hace una funcionalidad. Aquí se publica exactamente lo que está escrito.
- **No se repite.** Lo anunciado se apunta en el ledger (`novedades_publicadas`, con
  los `id`), así que reiniciar el worker, o desplegar sin entradas nuevas, no publica
  nada. Por eso un `id` no se reutiliza ni se cambia nunca.
- **El apunte va antes de publicar**, como en el resumen diario (`delivery/digest.py`):
  si Slack falla se borra el apunte y se reintenta en la vuelta siguiente; si lo que
  falla es borrar el apunte, se pierde ese anuncio en vez de repetirlo cada vuelta.
- Va al canal de Handoff con el bot de Handoff (`slack_writer`), como todo lo que
  publica el agente. Nunca al Founders Club.

Nada de esto lanza: un anuncio no puede tumbar el bucle.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from .. import db, guards
from ..config import load_settings
from . import prioridad, slack_writer

logger = logging.getLogger(__name__)

ACCION = "novedades_publicadas"
CABECERA = "📣 *What's new in the Founders Club agent*"


@dataclass(frozen=True)
class Novedad:
    id: str
    titulo: str
    lineas: tuple[str, ...]


# Las entradas, de la más vieja a la más nueva. Al añadir una funcionalidad, se añade
# aquí su entrada en el mismo commit. Lo ya anunciado no hace falta borrarlo: no vuelve a
# salir, y queda como historial de lo que se ha ido contando.
NOVEDADES: tuple[Novedad, ...] = (
    Novedad(
        id="2026-10-20-hilos",
        titulo="Reading thread replies",
        lineas=(
            (
                "The agent can now read the replies inside threads, not just the main posts. "
                'That\'s where people often write "+1, we need this too".'
            ),
            "It reads the whole thread as context, so it knows what a reply is answering.",
            (
                "It only flags someone who clearly states a need of their own, not people "
                "answering, recommending someone, offering their services or just following "
                "along."
            ),
            "Several people in the same thread become one alert, not many.",
            (
                "It only reads: it never writes, reacts or joins a channel, and it never marks "
                "anything as read."
            ),
            ("It's off for now. Turn it on in Settings → Watched channels → Read thread replies."),
        ),
    ),
    Novedad(
        id="2026-10-20-seguimiento-mas-tranquilo",
        titulo="Fewer follow-up messages",
        lineas=(
            (
                "Follow-up alerts were too noisy: 18 messages in 13 hours. That is fixed, "
                "and they are much quieter now."
            ),
            (
                "Only conversations waiting on YOU interrupt: an unread message from a "
                "lead, or someone who replied and is waiting for your answer."
            ),
            (
                '"You wrote and nobody answered" no longer sends alerts. It stays in the '
                "once-a-day summary, where it belongs."
            ),
            "Each conversation is brought up once a day at most, instead of every 4 hours.",
        ),
    ),
    Novedad(
        id="2026-11-03-reaccion-es-respuesta",
        titulo="An emoji reaction counts as a reply",
        lineas=(
            (
                "If you answer someone with a 👍 instead of typing, the agent now treats "
                "that as a reply and stops reminding you about it."
            ),
            (
                "It works both ways: if you wrote and they reacted to your message, that "
                "counts as their answer too."
            ),
            (
                "Any emoji counts, and the agent never looks at which one. It still "
                "cannot react to anything itself."
            ),
        ),
    ),
    Novedad(
        id="2026-11-11-panel-nueva-direccion",
        titulo="The panel has a new address",
        lineas=(
            (
                "It now lives at "
                "https://panel-production-a2fc11e3.up.railway.app — update your bookmark."
            ),
            (
                "Same panel, same login. If you are signed in on the old address, sign in "
                "again on this one."
            ),
            (
                'The sign-in page now has a "Forgot your password?" link, so you no '
                "longer need anyone to reset it for you."
            ),
        ),
    ),
    Novedad(
        id="2026-11-11-chat",
        titulo="Read the Founders Club from the panel",
        lineas=(
            (
                "There is a new Chat tab showing the conversations the agent reads: "
                "channels, their threads, and who said what."
            ),
            (
                "Public channels and private messages are clearly separated, so it is "
                "never unclear who can see what."
            ),
            (
                "It is read-only for now. Replying from the panel is built but switched "
                "off until we turn it on deliberately."
            ),
        ),
    ),
    Novedad(
        id="2026-11-11-hubspot",
        titulo="Leads reach HubSpot",
        lineas=(
            (
                "When Anthony marks a lead as contacted, the agent now creates or updates "
                "the company and contact in HubSpot and attaches a note with why they fit "
                "and a link to their full profile in the panel."
            ),
            (
                "It never creates duplicates: it matches on email and website, and updates "
                "what is already there."
            ),
            (
                "It only writes for people Anthony actually contacted, never for every "
                "card, so the CRM stays clean."
            ),
            (
                "Nothing private goes in: no direct messages, no quotes from the community. "
                "The CRM gets the conclusion, the panel keeps the evidence."
            ),
            "It is off until we switch it on, and it never imports old leads by itself.",
        ),
    ),
    Novedad(
        id="2026-11-20-prioridad",
        titulo="Alerts now tell you what to handle first",
        lineas=(
            (
                "Every alert starts with how urgent it is: 🔴 ACT NOW (a likely customer is "
                "waiting), 🟡 TODAY (worth a few minutes before the day ends) or ⚪ FYI (good "
                "to know, nothing waiting on you). The phone notification shows it too."
            ),
            (
                "One daily brief each morning (8 am New York time; you can change the hour): "
                "everything still open, most urgent first, with a link on every line. It "
                "replaces the separate follow-ups summary, so it is still one summary a day."
            ),
            (
                "People leave the brief as soon as you mark them Contacted or Discard, and "
                "a conversation leaves it once you answer."
            ),
            (
                "Each kind of alert has its own look (research card, they asked, mention, "
                "introduction, new member, waiting on you), so you can tell them apart "
                "before opening them."
            ),
            (
                "Introductions now come with a reminder to say hi, a suggested greeting "
                "ready to copy, and a LinkedIn link to connect."
            ),
            "New alert when someone joins the club, so you can welcome them.",
            (
                "New reminders for thread replies and mentions you have not answered yet. "
                "Reacting with an emoji counts as answering."
            ),
            "The Today page in the panel is sorted the same way: most important first.",
            "Everything here can be adjusted or switched off in Settings.",
        ),
    ),
    Novedad(
        id="2026-11-20-sin-repetidas-y-reporte-semanal",
        titulo="No more repeated cards, and a weekly hiring report",
        lineas=(
            (
                "Fixed: the same person no longer shows up several times. One person "
                "got 12 cards in 3 days; that was a bug, and it is gone."
            ),
            (
                "From now on it is one card per person. Within 7 days a second one only "
                "comes if their fit went up or someone on the team asked for new "
                "research. The same goes for the SMS."
            ),
            (
                "Companies that are only hiring no longer come as HIGH SIGNAL. When all "
                "we know is that a company posted open roles, and the person did not ask "
                "for anything, there is no card and no SMS."
            ),
            (
                "Instead they come once a week in a hiring report (Mondays, 9 am New York "
                "time): up to 15 companies hiring right now, each with the member, their "
                "role, what they are hiring for and a LinkedIn link. It is a list for "
                "nurturing: open their LinkedIn, comment on a post."
            ),
            (
                "Someone who says in their own words that they need help still comes "
                "right away, exactly as before."
            ),
            (
                'Alerts now have a new "Open in app" link, made for your phone. Tell us '
                "if it lands on the message or only on the channel."
            ),
            "The day, the hour and the limits can be changed in Settings.",
        ),
    ),
    Novedad(
        id="2026-11-25-cualquier-rol",
        titulo="Hiring alerts for every role",
        lineas=(
            (
                "The agent used to alert only for a fixed list of roles (support, "
                "operations, assistants, back-office, sales, recruiting, accounting, dev). "
                "That is why it skipped a post hiring for two marketing roles."
            ),
            (
                "That limit is gone. Any founder who says they are hiring, or plan to hire "
                "soon, for any role, full-time or part-time, now triggers an alert."
            ),
            (
                "Still skipped: people looking for a job, offering their services, or "
                "looking for an agency or a tool instead of a person."
            ),
        ),
    ),
)


def _anunciadas() -> set[str]:
    filas = db.fetch_all("select payload from agent_actions where action = %s", (ACCION,))
    ids: set[str] = set()
    for fila in filas:
        ids.update((fila["payload"] or {}).get("ids") or [])
    return ids


def bloques(lista: list[Novedad], nivel: str | None = None) -> list[dict]:
    """Un bloque por entrada: el límite de Slack es por bloque, y así una entrada larga
    no se come a las demás. `nivel` es la prioridad que decidió `prioridad.py` (0039) y
    va delante de la cabecera."""
    cabecera = prioridad.cabecera(CABECERA, nivel)
    salida = [{"type": "section", "text": {"type": "mrkdwn", "text": cabecera}}]
    for n in lista:
        texto = f"*{n.titulo}*\n" + "\n".join(f"• {linea}" for linea in n.lineas)
        salida.append({"type": "section", "text": {"type": "mrkdwn", "text": texto}})
    return salida


def resumen_texto(lista: list[Novedad]) -> str:
    """El texto de la notificación del móvil, que no enseña los bloques."""
    return "What's new: " + "; ".join(n.titulo for n in lista)


def _publicar() -> int:
    settings = load_settings()
    if not (settings.handoff_bot_token and settings.handoff_channel_id):
        return 0
    try:
        guards.check_kill_switch()
    except guards.KillSwitchActive:
        # Con el agente parado no se anuncia nada; sale cuando se reactive.
        return 0

    pendientes = [n for n in NOVEDADES if n.id not in _anunciadas()]
    if not pendientes:
        return 0

    marcador = db.fetch_one(
        "insert into agent_actions (action, payload) values (%s, %s) returning id",
        (ACCION, json.dumps({"ids": [n.id for n in pendientes]})),
    )
    # Un anuncio es para estar al tanto: nunca compite con un lead (0039).
    nivel = prioridad.visible(prioridad.nivel(prioridad.NOVEDADES))
    try:
        slack_writer.post_card(
            bloques(pendientes, nivel), prioridad.resumen(resumen_texto(pendientes), nivel)
        )
    except Exception:
        logger.exception("novedades: no se pudo publicar; se reintenta en la vuelta siguiente")
        db.execute("delete from agent_actions where id = %s", (marcador["id"],))
        return 0
    return len(pendientes)


def publicar_pendientes() -> int:
    """Publica las novedades aún no anunciadas. Devuelve cuántas salieron. Nunca lanza."""
    try:
        return _publicar()
    except Exception:
        logger.exception("novedades: fallo inesperado; se reintenta en la vuelta siguiente")
        return 0
