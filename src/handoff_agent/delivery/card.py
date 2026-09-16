"""La tarjeta de Slack: el producto que ve Anthony.

Todo el sistema existe para producir un clic en "Ver mensaje original", así que
la tarjeta cita textualmente, explica por qué importa con fuentes, y propone un
ángulo. El texto ajeno se cita como bloque de cita y se recorta: no puede
romper el formato ni alargar la tarjeta sin fin.
"""

from __future__ import annotations

QUOTE_CHARS = 300
MAX_SIGNALS = 3
EMOJI = {"alta": "🔥", "media": "👀", "baja": "📋"}


def _quote(text: str) -> str:
    clean = " ".join((text or "").split())[:QUOTE_CHARS]
    return "\n".join(f"> {line}" for line in clean.splitlines() or [""])


def _headline(person: dict, dossier: dict, band: str) -> str:
    persona = dossier.get("persona") or {}
    empresa = dossier.get("empresa") or {}
    name = persona.get("nombre") or person.get("full_name") or person["slack_user_id"]
    role = persona.get("cargo")
    company = empresa.get("nombre") or person.get("company_name")
    size = empresa.get("empleados_aprox")
    bits = [b for b in (role, company) if b]
    tail = f" (~{size} personas)" if size else ""
    return f"*{name}*" + (f" — {', '.join(bits)}{tail}" if bits else "")


def build(
    person: dict,
    dossier: dict,
    band: str,
    message: dict | None,
    permalink: str | None,
) -> list[dict]:
    fit = dossier.get("encaje_handoff") or {}
    hiring = dossier.get("contratacion") or {}
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{EMOJI.get(band, '')} *SEÑAL {band.upper()}* · encaje {fit.get('puntuacion')}/3\n"
                + _headline(person, dossier, band),
            },
        }
    ]

    if message and (message.get("text") or "").strip():
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "Dijo:\n" + _quote(message["text"])},
            }
        )

    reasons = []
    if fit.get("razon"):
        reasons.append(f"· {fit['razon']}")
    for signal in (dossier.get("senales_contexto") or [])[:MAX_SIGNALS]:
        reasons.append(f"· <{signal.get('fuente')}|{signal.get('hecho')}>")
    if hiring.get("vacantes_abiertas"):
        roles = ", ".join(hiring.get("roles_deslocalizables") or [])
        reasons.append(
            f"· {hiring['vacantes_abiertas']} vacantes abiertas" + (f": {roles}" if roles else "")
        )
    if reasons:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*Por qué importa*\n" + "\n".join(reasons)},
            }
        )

    if dossier.get("resumen"):
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": dossier["resumen"]}]}
        )

    elements = [
        {
            "type": "button",
            "action_id": "contactado",
            "text": {"type": "plain_text", "text": "Contactado"},
            "value": person["id"],
            "style": "primary",
        },
        {
            "type": "button",
            "action_id": "descartar",
            "text": {"type": "plain_text", "text": "Descartar"},
            "value": person["id"],
            "style": "danger",
        },
        {
            "type": "button",
            "action_id": "investigar_mas",
            "text": {"type": "plain_text", "text": "Investigar más"},
            "value": person["id"],
        },
    ]
    if permalink:
        elements.append(
            {
                "type": "button",
                "action_id": "ver_original",
                "text": {"type": "plain_text", "text": "Ver mensaje original"},
                "url": permalink,
            }
        )
    blocks.append({"type": "actions", "elements": elements})
    return blocks
