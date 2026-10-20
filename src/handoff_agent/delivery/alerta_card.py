"""La tarjeta de alerta de señal: más ligera que la de research.

Sale cuando alguien dice algo que Handoff cubre ("we're hiring a support rep"),
sin esperar a ningún research: cabecera, quién es, la cita y poco más. Si la
persona ya tiene dossier, una línea con su encaje y su estado; si no, una línea
diciendo que la investigación está en marcha.

Mismas defensas que `card.py`, y se reutilizan sus funciones en vez de
reescribirlas: la cita, el nombre y la empresa los escribió un tercero, así que
todo pasa por `_escape_and_cap`/`_quote` antes de entrar en un bloque `mrkdwn`;
el encaje solo se pinta si es un `int` de verdad (`_safe_number`); y el único
enlace que sale de aquí es el del panel (montado con `settings.panel_url` y el
id de la persona) o el permalink que dio Slack, validados con `_is_safe_link`.

Los botones "Contacted" y "Discard" llevan los mismos `action_id` y `value` que
los de la tarjeta de research, así que el receptor (`web/app.py`) los procesa
sin cambios. Solo se dibujan cuando hay un prospect al que aplicarlos: su
`value` es el id del prospect, y sin él el clic no podría hacer nada.

Aritmética del peor caso (tope de Slack: 3000 por bloque `section`, 2000 por
bloque `context`):
  cabecera:  "🗣️ *THEY ASKED · " + etiqueta fija (<= 16) + "*"                  ~  40
  nombre:    "\n*" + NAME_CHARS + "*" + " — " + COMPANY_CHARS                    ~ 330
  cita:      "> `" + QUOTE_CHARS (300) caracteres escapados (5x al escapar '&') + "`"
             (código en línea para que ninguna URL se autoenlace, ver card._quote) ~ 1505
  dossier:   "Dossier: fit " + NUM_CHARS + "/3 · " + etiqueta fija              ~  60
Todo muy por debajo de los topes.

La alerta de un hilo (`build_hilo`) junta en una tarjeta a todas las personas
que, en un mismo hilo, dijeron tener la necesidad: "In a thread:" con el
mensaje principal citado y recortado, y por cada persona su nombre, su cita y un
enlace a su respuesta. Sin "Contacted"/"Discard": el receptor repinta la tarjeta
quitando TODOS los bloques de acciones (`web/app.py::_repaint`), así que un clic
sobre una persona se llevaría los botones de las demás. Solo botones de enlace
("View reply", "Open in panel"), que el receptor ignora. Peor caso por bloque:
el principal es "*In a thread:*\n" + una cita como la de arriba (~1520), y cada
persona repite los bloques de nombre y cita; con `MAX_PERSONAS_HILO` personas son
2 + 3 x 5 = 17 bloques, lejos del tope de 50.
"""

from __future__ import annotations

from ..config import load_settings
from .card import (
    COMPANY_CHARS,
    NAME_CHARS,
    NUM_CHARS,
    _escape_and_cap,
    _is_safe_link,
    _quote,
    _safe_number,
)

# Etiqueta en inglés de cada tipo de señal (los valores internos son los de
# signals/detector.py). Un tipo desconocido cae a "other": nunca se reproduce
# el valor crudo en la cabecera.
TIPO_LABELS = {
    "contratacion": "hiring",
    "soporte": "support",
    "operaciones": "operations",
    "asistente": "assistant",
    "back_office": "back office",
    "ventas": "sales",
    "reclutamiento": "recruiting",
    "contabilidad": "accounting",
    "desarrollo": "development",
}
# Estado del prospect tal como se ve en Slack. "descartado" no está: a un
# descartado no se le avisa nunca (ver signals/alertas.py).
ESTADO_LABELS = {
    "nuevo": "New",
    "investigado": "Researched",
    "contactado": "Contacted",
    "cliente": "Client",
    "incompleto": "Incomplete",
}


def tipo_label(tipo: object) -> str:
    return TIPO_LABELS.get(tipo, "other") if isinstance(tipo, str) else "other"


def summary(senal: dict, person: dict | None) -> str:
    """Texto de la notificación push (el `text` de chat.postMessage)."""
    who = (person or {}).get("full_name") or senal.get("slack_user_id") or "someone"
    return f"They asked: {tipo_label(senal.get('tipo'))} · {_escape_and_cap(who, NAME_CHARS)}"


def _who(senal: dict, person: dict | None, dossier: dict | None) -> str:
    person = person or {}
    empresa = (dossier or {}).get("empresa") or {}
    persona = (dossier or {}).get("persona") or {}
    raw_name = (
        persona.get("nombre") or person.get("full_name") or senal.get("slack_user_id") or "unknown"
    )
    name = _escape_and_cap(raw_name, NAME_CHARS)
    company = _escape_and_cap(person.get("company_name") or empresa.get("nombre"), COMPANY_CHARS)
    return f"*{name}*" + (f" — {company}" if company else "")


def _status_line(person: dict | None, dossier: dict | None) -> str:
    if not isinstance(dossier, dict):
        return "🔎 Research is on its way: the full dossier will follow."
    fit = dossier.get("encaje_handoff")
    score = _safe_number(fit.get("puntuacion") if isinstance(fit, dict) else None, NUM_CHARS)
    estado = ESTADO_LABELS.get((person or {}).get("state"), "")
    bits = [f"fit {score}/3" if score else "no fit score"]
    if estado:
        bits.append(estado)
    return "Dossier: " + " · ".join(bits)


def build(
    senal: dict,
    person: dict | None,
    dossier: dict | None,
    permalink: str | None,
) -> list[dict]:
    """`senal`: dict de signals/procesar (tipo, cita, slack_user_id...). `person`:
    fila de `prospects` o None si quien escribe aún no es un prospect. `dossier`:
    el `content` del último dossier, o None si todavía no hay."""
    header = f"🗣️ *THEY ASKED · {tipo_label(senal.get('tipo'))}*\n" + _who(senal, person, dossier)
    blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": header}}]

    cita = senal.get("cita")
    if isinstance(cita, str) and cita.strip():
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _quote(cita)}})

    status = _status_line(person, dossier)
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": status}]})

    elements: list[dict] = []
    person_id = str((person or {}).get("id") or "")
    if person_id:
        elements += [
            {
                "type": "button",
                "action_id": "contactado",
                "text": {"type": "plain_text", "text": "Contacted"},
                "value": person_id,
                "style": "primary",
            },
            {
                "type": "button",
                "action_id": "descartar",
                "text": {"type": "plain_text", "text": "Discard"},
                "value": person_id,
                "style": "danger",
            },
        ]
        panel_url = load_settings().panel_url
        if panel_url:
            panel_link = f"{panel_url}/personas/{person_id}"
            if _is_safe_link(panel_link):
                elements.append(
                    {
                        "type": "button",
                        "action_id": "abrir_panel",
                        "text": {"type": "plain_text", "text": "Open in panel"},
                        "url": panel_link,
                    }
                )
    if permalink and _is_safe_link(permalink):
        elements.append(
            {
                "type": "button",
                "action_id": "ver_original",
                "text": {"type": "plain_text", "text": "View original message"},
                "url": permalink,
            }
        )
    # Slack rechaza un bloque de acciones vacío.
    if elements:
        blocks.append({"type": "actions", "elements": elements})
    return blocks


# Cuántas personas caben en una alerta de hilo. `signals/alertas.py` usa el mismo
# número para decidir cuántas entran; las demás esperan a la vuelta siguiente.
MAX_PERSONAS_HILO = 5
# El principal es contexto: lo justo para entender a qué contestan. `_quote` ya
# corta en QUOTE_CHARS, pero sin avisar; aquí se recorta antes y con "...".
POST_CHARS = 280


def _recorte(texto: str, tope: int) -> str:
    texto = " ".join(texto.split())
    return texto if len(texto) <= tope else texto[: tope - 3].rstrip() + "..."


def summary_hilo(entradas: list[dict]) -> str:
    """Texto de la notificación push de una alerta de hilo."""
    primera = entradas[0] if entradas else {}
    senal, person = primera.get("senal") or {}, primera.get("person")
    who = (person or {}).get("full_name") or senal.get("slack_user_id") or "someone"
    resto = f" +{len(entradas) - 1}" if len(entradas) > 1 else ""
    return (
        f"They asked in a thread: {tipo_label(senal.get('tipo'))} · "
        f"{_escape_and_cap(who, NAME_CHARS)}{resto}"
    )


def _botones_persona(person: dict | None, permalink: str | None) -> list[dict]:
    elements: list[dict] = []
    if permalink and _is_safe_link(permalink):
        elements.append(
            {
                "type": "button",
                "action_id": "ver_original",
                "text": {"type": "plain_text", "text": "View reply"},
                "url": permalink,
            }
        )
    person_id = str((person or {}).get("id") or "")
    panel_url = load_settings().panel_url if person_id else None
    if person_id and panel_url:
        panel_link = f"{panel_url}/personas/{person_id}"
        if _is_safe_link(panel_link):
            elements.append(
                {
                    "type": "button",
                    "action_id": "abrir_panel",
                    "text": {"type": "plain_text", "text": "Open in panel"},
                    "url": panel_link,
                }
            )
    return elements


def build_hilo(post: str | None, entradas: list[dict]) -> list[dict]:
    """`post`: texto del mensaje principal del hilo, o None si no está guardado.
    `entradas`: una por persona, en orden, como dicts con `senal` (la de
    signals/alertas: tipo, cita, slack_user_id...), `person` (fila de `prospects`
    o None) y `permalink` (de su respuesta, o None). Se pintan como mucho
    `MAX_PERSONAS_HILO`; quien llama ya no debería mandar más."""
    entradas = entradas[:MAX_PERSONAS_HILO]
    tipo = tipo_label(((entradas[0] if entradas else {}).get("senal") or {}).get("tipo"))
    n = len(entradas)
    gente = "1 person" if n == 1 else f"{n} people"
    header = f"🗣️ *THEY ASKED · {tipo}*\n{gente} in the same thread said they need it."
    blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": header}}]

    if isinstance(post, str) and post.strip():
        contexto = "*In a thread:*\n" + _quote(_recorte(post, POST_CHARS))
    else:
        contexto = "*In a thread* (the original post is not available)."
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": contexto}})

    for entrada in entradas:
        senal = entrada.get("senal") or {}
        person = entrada.get("person")
        linea = _who(senal, person, None)
        propio = tipo_label(senal.get("tipo"))
        if propio != tipo:
            # Cada uno puede necesitar algo distinto aunque estén en el mismo hilo.
            linea += f" · {propio}"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": linea}})
        cita = senal.get("cita")
        if isinstance(cita, str) and cita.strip():
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _quote(cita)}})
        botones = _botones_persona(person, entrada.get("permalink"))
        if botones:
            blocks.append({"type": "actions", "elements": botones})
    return blocks
