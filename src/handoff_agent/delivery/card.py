"""La tarjeta de Slack: el producto que ve Anthony.

Todo el sistema existe para producir un clic en "View original message", así que
la tarjeta cita textualmente, explica por qué importa con fuentes, y propone un
ángulo. El texto ajeno se cita como bloque de cita y se recorta: no puede
romper el formato ni alargar la tarjeta sin fin.

Todo lo que se interpola aquí (la cita, y cada campo del dossier) lo escribió
un tercero o un modelo leyendo páginas de terceros: nunca se confía en su
contenido, solo se valida su presencia. Por eso pasa siempre por
`_escape_mrkdwn` antes de entrar en un bloque `mrkdwn`, y un campo que debería
ser numérico (`empleados_aprox`, `puntuacion`, `vacantes_abiertas`) solo se
renderiza cuando de verdad es un `int` (ver `_safe_number`); si no, se omite
exactamente como si faltara. No hay ningún f-string ni concatenación en este
módulo que interpole un valor de `person`, `dossier` o `message` sin pasar
antes por `_escape_and_cap`/`_escape_mrkdwn` (texto) o `_safe_number`
(números).
"""

from __future__ import annotations

from urllib.parse import urlparse

QUOTE_CHARS = 300
HECHO_CHARS = 240
RAZON_CHARS = 400
FUENTE_CHARS = 200
ROLES_CHARS = 200
RESUMEN_CHARS = 1900
NAME_CHARS = 160
ROLE_CHARS = 160
COMPANY_CHARS = 160
NUM_CHARS = 20
MAX_SIGNALS = 3
EMOJI = {"alta": "🔥", "media": "👀", "baja": "📋"}
# Etiqueta visible en Slack para cada banda interna (alta/media/baja): el
# resto del sistema sigue usando el nombre en español como valor interno (ver
# bands.py), solo la cabecera de la tarjeta se traduce.
BAND_LABELS = {"alta": "HIGH", "media": "MEDIUM", "baja": "LOW"}

# `empresa_no_confirmada` (ver research/worker.py, a partir de
# Gathered.unconfirmed_domain): igual que el resto del dossier, se trata como
# escrito por un tercero -- nunca se confía en su tipo, solo se valida su
# presencia. El dominio es corto en la práctica, pero se escapa y se recorta
# igual que cualquier otro campo de texto.
UNCONFIRMED_DOMAIN_CHARS = 200

# `datos_proveedor` (ver research/worker.py): datos de un proveedor externo,
# sin fuente citable. Igual que el resto del dossier, se trata como escrito
# por un tercero -- nunca se confía en su tipo, solo se valida su presencia.
PROVEEDOR_CHARS = 500
PROVEEDOR_MAX_AREAS = 3
PROVEEDOR_GROWTH_MONTHS = 12
PROVEEDOR_AREA_LABELS = {
    "support": "support",
    "operations": "operations",
    "sales": "sales",
    "engineering": "engineering",
    "finance": "finance",
    "human_resources": "HR",
    "marketing": "marketing",
    "customer_success": "customer success",
}
# En qué orden se prefieren las áreas cuando hay que elegir solo tres: son las
# que mejor predicen encaje con el staffing de Handoff en LATAM.
PROVEEDOR_PRIORITY_AREAS = ("support", "operations", "sales", "customer_success")

# Los topes de arriba se aplican al texto YA escapado (ver _escape_and_cap):
# `_escape_mrkdwn` puede multiplicar la longitud hasta por 5 ('&' -> '&amp;'),
# así que recortar el texto crudo y escapar después, como se hacía antes, no
# garantiza nada — un `resumen` de 1000 '&' pasaba de 500 a 2500 caracteres.
#
# Los campos numéricos (`empleados_aprox`, `puntuacion`, `vacantes_abiertas`)
# no pasan por `_escape_mrkdwn` (un `int` de verdad no puede llevar sintaxis
# de Slack), pero si no son un `int` real — ni un `bool`, ni un string
# numérico como "50<!channel>" — `_safe_number` los descarta directamente y
# se omiten como si faltaran. Cuando sí son un `int`, se recortan a
# NUM_CHARS: nada impide que llegue un entero de precisión arbitraria con
# miles de dígitos.
#
# Aritmética del peor caso para la cabecera (tope de Slack: 3000 caracteres
# por bloque `section`), con las etiquetas ya en inglés:
#   emoji + " *" + band_label + " SIGNAL*"   band_label es "HIGH"/"MEDIUM"/
#                                             "LOW", longitud fija y acotada  17
#   " · fit " + NUM_CHARS + "/3"                     7 + 20 + 2       =   29
#   salto de línea                                                        1
#   "*" + NAME_CHARS + "*"                          2 + 160          =  162
#   " — " + ROLE_CHARS + ", " + COMPANY_CHARS    3 + 160 + 2 + 160   =  325
#   " (~" + NUM_CHARS + " people)"                  3 + 20 + 8       =   31
#                                                                --------------
#                                                                total    565
# 565 < 3000 con margen de sobra.
#
# Aritmética del peor caso para la sección "*Why it matters*" (tope de Slack:
# 3000 caracteres por bloque `section`):
#   prefijo "*Why it matters*\n"                          17
#   razón:            "· " + RAZON_CHARS                  2 + 400 =  402
#   3 señales:        "· <" + FUENTE_CHARS + "|"
#                      + HECHO_CHARS + ">"     3 × (5 + 200 + 240) = 1335
#   contratación:     "· " + NUM_CHARS + " open roles: "
#                      + ROLES_CHARS                 2 + 20 + 13 + 200 =  235
#   4 saltos de línea entre 5 líneas                                4
#                                                          --------------
#                                                          total  1993
# 1993 < 3000 con margen de sobra.
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


def _safe_number(value: object, limit: int) -> str:
    """Solo se renderiza un campo numérico si de verdad es un `int`.

    `empleados_aprox`, `puntuacion` y `vacantes_abiertas` deberían ser
    números, pero llegan desde un tercero o un modelo leyendo páginas
    ajenas, así que no se confía en el tipo: un `bool` (subclase de `int`
    en Python) o un string con pinta de número ("50<!channel>") no cuentan
    como número real y se descartan, tratándose exactamente como si el
    campo faltara. Los dígitos de un `int` no pueden formar sintaxis de
    Slack, así que no hace falta `_escape_mrkdwn` aquí — pero sí se
    recorta a `limit`, porque nada impide que llegue un entero de
    precisión arbitraria capaz de reventar el tope de un bloque.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)[:limit]
    return ""


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
    raw_name = persona.get("nombre") or person.get("full_name") or person["slack_user_id"]
    name = _escape_and_cap(raw_name, NAME_CHARS)
    role = _escape_and_cap(persona.get("cargo"), ROLE_CHARS)
    raw_company = empresa.get("nombre") or person.get("company_name")
    company = _escape_and_cap(raw_company, COMPANY_CHARS)
    size = _safe_number(empresa.get("empleados_aprox"), NUM_CHARS)
    bits = [b for b in (role, company) if b]
    tail = f" (~{size} people)" if size else ""
    return f"*{name}*" + (f" — {', '.join(bits)}{tail}" if bits else "")


def _proveedor_int(value: object) -> int | None:
    """Como `_safe_number`, pero devuelve el `int` (o None), no el texto ya
    recortado: aquí el número todavía se combina con otros antes de escapar."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _format_int(n: int) -> str:
    return f"{n:,}"


def _area_label(key: str) -> str:
    if key in PROVEEDOR_AREA_LABELS:
        return PROVEEDOR_AREA_LABELS[key]
    return key.replace("_", " ")


def _proveedor_areas(areas: object) -> list[tuple[str, int]]:
    if not isinstance(areas, dict):
        return []
    safe = {
        key: _proveedor_int(value)
        for key, value in areas.items()
        if isinstance(key, str) and _proveedor_int(value) is not None
    }
    selected = [k for k in PROVEEDOR_PRIORITY_AREAS if k in safe][:PROVEEDOR_MAX_AREAS]
    if not selected:
        by_count = sorted(safe.items(), key=lambda kv: -kv[1])
        selected = [k for k, _ in by_count[:PROVEEDOR_MAX_AREAS]]
    return [(k, safe[k]) for k in selected]


def _growth_from_series(evolucion: object) -> str | None:
    """Crecimiento a 12 meses calculado de la evolución mensual: es el dato
    coherente con el recuento de empleados. El `crecimiento` del proveedor usa
    otra base y con Stripe daba +0,14 % mientras la serie mostraba +24 %."""
    if not isinstance(evolucion, list):
        return None
    counts = [
        item.get("empleados")
        for item in evolucion
        if isinstance(item, dict) and _proveedor_int(item.get("empleados")) is not None
    ]
    if len(counts) <= PROVEEDOR_GROWTH_MONTHS or counts[-PROVEEDOR_GROWTH_MONTHS - 1] <= 0:
        return None
    before, now = counts[-PROVEEDOR_GROWTH_MONTHS - 1], counts[-1]
    value = round((now - before) * 100 / before)
    sign = "+" if value >= 0 else ""
    return f"{sign}{value}% in {PROVEEDOR_GROWTH_MONTHS} months"


def _proveedor_growth(proveedor: dict) -> str | None:
    from_series = _growth_from_series(proveedor.get("evolucion_mensual"))
    if from_series:
        return from_series
    crecimiento = proveedor.get("crecimiento")
    if not isinstance(crecimiento, list):
        return None
    for item in crecimiento:
        if not isinstance(item, dict) or item.get("meses") != PROVEEDOR_GROWTH_MONTHS:
            continue
        pct = item.get("porcentaje")
        if isinstance(pct, bool) or not isinstance(pct, (int, float)):
            continue
        # El proveedor ya lo da en puntos porcentuales (0.1439 = 0,14 %).
        sign = "+" if pct >= 0 else ""
        return f"{sign}{pct:.1f}% in {PROVEEDOR_GROWTH_MONTHS} months"
    return None


def _proveedor_block(dossier: dict) -> dict | None:
    """Una línea de contexto con lo poco del proveedor externo que se puede
    presentar sin verificar: recuento de empleados, crecimiento a 12 meses y
    hasta tres áreas. Si no sobrevive ningún número, no hay bloque."""
    proveedor = dossier.get("datos_proveedor")
    if not isinstance(proveedor, dict):
        return None

    empleados = _proveedor_int(proveedor.get("empleados_linkedin"))
    if empleados is None:
        empleados = _proveedor_int(proveedor.get("empleados_crm"))

    bits = []
    if empleados is not None:
        bits.append(f"{_format_int(empleados)} employees")

    growth = _proveedor_growth(proveedor)
    if growth:
        bits.append(growth)

    for key, count in _proveedor_areas(proveedor.get("empleados_por_area")):
        bits.append(f"{_area_label(key)} {_format_int(count)}")

    if not bits:
        return None

    text = _escape_and_cap("Provider data (unverified): " + " · ".join(bits), PROVEEDOR_CHARS)
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _unconfirmed_domain_block(dossier: dict) -> dict | None:
    """Aviso cuando la web se adivinó y no se pudo confirmar (ver
    research/gather.py `_confirm_domain`): el resto del dossier puede
    describir la empresa equivocada, y solo el panel puede corregirlo."""
    info = dossier.get("empresa_no_confirmada")
    if not isinstance(info, dict):
        return None
    domain = info.get("dominio_adivinado")
    if not isinstance(domain, str) or not domain.strip():
        return None
    # Solo el dominio pasa por _escape_and_cap: es el único trozo de este
    # bloque que no lo escribimos nosotros. El resto del texto es fijo y
    # corto, así que el bloque entero cabe de sobra en el tope de 2000
    # caracteres de un bloque `context` sin necesidad de recortarlo también.
    escaped_domain = _escape_and_cap(domain, UNCONFIRMED_DOMAIN_CHARS)
    text = (
        f"⚠️ Company not confirmed (guessed website: {escaped_domain} was discarded). "
        "Set the right website in the panel and re-research."
    )
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def build(
    person: dict,
    dossier: dict,
    band: str,
    message: dict | None,
    permalink: str | None,
) -> list[dict]:
    fit = dossier.get("encaje_handoff") or {}
    hiring = dossier.get("contratacion") or {}
    score = _safe_number(fit.get("puntuacion"), NUM_CHARS)
    score_bit = f" · fit {score}/3" if score else ""
    band_label = BAND_LABELS.get(band, band.upper())
    header = f"{EMOJI.get(band, '')} *{band_label} SIGNAL*{score_bit}\n" + _headline(
        person, dossier, band
    )
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
    ]

    unconfirmed_block = _unconfirmed_domain_block(dossier)
    if unconfirmed_block:
        blocks.append(unconfirmed_block)

    if message and (message.get("text") or "").strip():
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "Said:\n" + _quote(message["text"])},
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
    vacantes = _safe_number(hiring.get("vacantes_abiertas"), NUM_CHARS)
    if vacantes:
        roles_joined = ", ".join(
            _escape_mrkdwn(r) for r in (hiring.get("roles_deslocalizables") or [])
        )
        roles = _trim_escaped(roles_joined, ROLES_CHARS)
        reasons.append(f"· {vacantes} open roles" + (f": {roles}" if roles else ""))
    if reasons:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*Why it matters*\n" + "\n".join(reasons)},
            }
        )

    resumen = dossier.get("resumen")
    if resumen:
        trimmed = _escape_and_cap(resumen, RESUMEN_CHARS)
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": trimmed}]})

    proveedor_block = _proveedor_block(dossier)
    if proveedor_block:
        blocks.append(proveedor_block)

    elements = [
        {
            "type": "button",
            "action_id": "contactado",
            "text": {"type": "plain_text", "text": "Contacted"},
            "value": str(person["id"]),
            "style": "primary",
        },
        {
            "type": "button",
            "action_id": "descartar",
            "text": {"type": "plain_text", "text": "Discard"},
            "value": str(person["id"]),
            "style": "danger",
        },
        {
            "type": "button",
            "action_id": "investigar_mas",
            "text": {"type": "plain_text", "text": "Research more"},
            "value": str(person["id"]),
        },
    ]
    if permalink:
        elements.append(
            {
                "type": "button",
                "action_id": "ver_original",
                "text": {"type": "plain_text", "text": "View original message"},
                "url": permalink,
            }
        )
    blocks.append({"type": "actions", "elements": elements})
    return blocks
