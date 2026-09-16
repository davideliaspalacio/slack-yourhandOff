"""La tarjeta de Slack: el producto que ve Anthony.

Todo el sistema existe para producir un clic en "Ver mensaje original", así que
la tarjeta cita textualmente, explica por qué importa con fuentes, y propone un
ángulo. El texto ajeno se cita como bloque de cita y se recorta: no puede
romper el formato ni alargar la tarjeta sin fin.

Todo lo que se interpola aquí (la cita, y cada campo del dossier) lo escribió
un tercero o un modelo leyendo páginas de terceros: nunca se confía en su
contenido, solo se valida su presencia. Por eso pasa siempre por
`_escape_mrkdwn` antes de entrar en un bloque `mrkdwn`.
"""

from __future__ import annotations

from urllib.parse import urlparse

QUOTE_CHARS = 300
HECHO_CHARS = 240
RAZON_CHARS = 400
FUENTE_CHARS = 200
ROLES_CHARS = 200
RESUMEN_CHARS = 1900
MAX_SIGNALS = 3
EMOJI = {"alta": "🔥", "media": "👀", "baja": "📋"}

# Los topes de arriba se aplican al texto YA escapado (ver _escape_and_cap):
# `_escape_mrkdwn` puede multiplicar la longitud hasta por 5 ('&' -> '&amp;'),
# así que recortar el texto crudo y escapar después, como se hacía antes, no
# garantiza nada — un `resumen` de 1000 '&' pasaba de 500 a 2500 caracteres.
#
# Aritmética del peor caso para la sección "*Por qué importa*" (tope de Slack:
# 3000 caracteres por bloque `section`):
#   prefijo "*Por qué importa*\n"                        18
#   razón:            "· " + RAZON_CHARS                  2 + 400 =  402
#   3 señales:        "· <" + FUENTE_CHARS + "|"
#                      + HECHO_CHARS + ">"     3 × (5 + 200 + 240) = 1335
#   contratación:     "· " + nº vacantes (≤6 dígitos en la
#                      práctica) + " vacantes abiertas: " + ROLES_CHARS
#                                              2 + 6 + 20 + 200 =  228
#   4 saltos de línea entre 5 líneas                                4
#                                                          --------------
#                                                          total  1987
# 1987 < 3000 con margen de sobra para el nº de vacantes real.
#
# Aritmética del peor caso para el bloque `context` del resumen (tope de
# Slack: 2000 caracteres por bloque `context`): RESUMEN_CHARS = 1900 < 2000.


def _escape_mrkdwn(text: str | None) -> str:
    """Neutraliza los caracteres de control de mrkdwn de Slack.

    Slack interpreta `<!channel>`, `<!here>`, `<@U123>` y `<#C123>` como
    menciones o enlaces vivos sin importar lo que los rodee, y el ampersand
    forma parte de esa sintaxis de escape. Sin este paso, un mensaje o un
    campo del dossier que contenga `<!channel>` termina avisando a todo el
    canal de Handoff en cuanto se publica la tarjeta. Hay que escapar `&`
    antes que `<` y `>`, si no el escape de estos últimos se duplicaría.
    """
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _trim_escaped(escaped: str, limit: int) -> str:
    """Recorta texto ya escapado sin partir una entidad (`&amp;`, `&lt;`...).

    Si el corte cae a mitad de una entidad se retrocede hasta antes del '&'
    correspondiente: dejar un '&am' colgando reintroduciría un '&' suelto en
    el mrkdwn final, justo lo que `_escape_mrkdwn` existe para evitar.
    """
    if len(escaped) <= limit:
        return escaped
    cut = escaped[:limit]
    amp = cut.rfind("&")
    if amp != -1 and ";" not in cut[amp:]:
        cut = cut[:amp]
    return cut


def _escape_and_cap(text: str | None, limit: int) -> str:
    """Escapa primero y recorta después, para que el tope se aplique al texto
    que de verdad llega a Slack y no al crudo (ver la nota junto a las
    constantes de arriba)."""
    return _trim_escaped(_escape_mrkdwn(text), limit)


def _is_safe_link(url: str | None) -> bool:
    """Solo se enlaza si la URL es plausible y no puede romper `<url|texto>`.

    `fuente` es salida de un modelo leyendo páginas ajenas: basta un '|', un
    '<' o un '>' dentro para cerrar el enlace antes de tiempo y colar el
    resto como mrkdwn libre (incluida una mención). Ante la duda, no se
    enlaza: un enlace roto nunca debe llegar a la tarjeta.
    """
    if not url or any(ch in url for ch in "|<>"):
        return False
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _quote(text: str) -> str:
    clean = " ".join((text or "").split())[:QUOTE_CHARS]
    escaped = _escape_mrkdwn(clean)
    return "\n".join(f"> {line}" for line in escaped.splitlines() or [""])


def _headline(person: dict, dossier: dict, band: str) -> str:
    persona = dossier.get("persona") or {}
    empresa = dossier.get("empresa") or {}
    name = persona.get("nombre") or person.get("full_name") or person["slack_user_id"]
    role = persona.get("cargo")
    company = empresa.get("nombre") or person.get("company_name")
    size = empresa.get("empleados_aprox")
    bits = [_escape_mrkdwn(b) for b in (role, company) if b]
    tail = f" (~{size} personas)" if size else ""
    return f"*{_escape_mrkdwn(name)}*" + (f" — {', '.join(bits)}{tail}" if bits else "")


def build(
    person: dict,
    dossier: dict,
    band: str,
    message: dict | None,
    permalink: str | None,
) -> list[dict]:
    fit = dossier.get("encaje_handoff") or {}
    hiring = dossier.get("contratacion") or {}
    header = (
        f"{EMOJI.get(band, '')} *SEÑAL {band.upper()}* · encaje {fit.get('puntuacion')}/3\n"
        + _headline(person, dossier, band)
    )
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
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
        reasons.append(f"· {_escape_and_cap(fit['razon'], RAZON_CHARS)}")
    for signal in (dossier.get("senales_contexto") or [])[:MAX_SIGNALS]:
        hecho = _escape_and_cap(signal.get("hecho"), HECHO_CHARS)
        fuente = signal.get("fuente")
        if not hecho:
            continue
        if _is_safe_link(fuente):
            reasons.append(f"· <{_escape_and_cap(fuente, FUENTE_CHARS)}|{hecho}>")
        else:
            reasons.append(f"· {hecho}")
    if hiring.get("vacantes_abiertas"):
        roles_joined = ", ".join(
            _escape_mrkdwn(r) for r in (hiring.get("roles_deslocalizables") or [])
        )
        roles = _trim_escaped(roles_joined, ROLES_CHARS)
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

    resumen = dossier.get("resumen")
    if resumen:
        trimmed = _escape_and_cap(resumen, RESUMEN_CHARS)
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": trimmed}]})

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
