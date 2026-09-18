import json
import re

from handoff_agent.delivery import card

PERSON = {
    "id": "11111111-1111-1111-1111-111111111111",
    "full_name": "Ada Ruiz",
    "company_name": "Acme",
    "slack_user_id": "U1",
}
DOSSIER = {
    "persona": {"nombre": "Ada Ruiz", "cargo": "CEO"},
    "empresa": {"nombre": "Acme", "empleados_aprox": 60},
    "contratacion": {"vacantes_abiertas": 6, "roles_deslocalizables": ["support", "ops"]},
    "senales_contexto": [{"hecho": "Levantó Series A hace 3 meses", "fuente": "https://tc.com/a"}],
    "encaje_handoff": {"puntuacion": 3, "razon": "Contrata soporte ahora mismo"},
    "resumen": "CEO de Acme, 60 personas, contratando soporte.",
}
MESSAGE = {"text": "Our support queue is eating my whole week", "ts": "1726000000.000100"}


def blocks_text(blocks):
    return json.dumps(blocks, ensure_ascii=False)


def test_the_card_leads_with_the_person_and_the_quote():
    blocks = card.build(PERSON, DOSSIER, "alta", MESSAGE, "https://slack.com/p1")
    text = blocks_text(blocks)
    assert "Ada Ruiz" in text and "CEO" in text and "Acme" in text
    assert "support queue is eating my whole week" in text


def test_the_card_explains_why_it_matters_with_its_sources():
    text = blocks_text(card.build(PERSON, DOSSIER, "alta", MESSAGE, None))
    assert "Series A" in text and "https://tc.com/a" in text
    assert "Contrata soporte ahora mismo" in text


def test_the_buttons_carry_the_person_and_the_action():
    blocks = card.build(PERSON, DOSSIER, "alta", MESSAGE, "https://slack.com/p1")
    actions = next(b for b in blocks if b["type"] == "actions")["elements"]
    assert [e["action_id"] for e in actions[:3]] == ["contactado", "descartar", "investigar_mas"]
    assert all(e["value"] == PERSON["id"] for e in actions[:3])
    link = actions[3]
    assert link["url"] == "https://slack.com/p1"


def test_without_a_permalink_there_is_no_broken_button():
    blocks = card.build(PERSON, DOSSIER, "media", MESSAGE, None)
    actions = next(b for b in blocks if b["type"] == "actions")["elements"]
    assert all("url" not in e for e in actions)


def test_a_new_member_has_no_quote_to_show():
    """Un miembro nuevo que aún no ha escrito no tiene mensaje que citar."""
    text = blocks_text(card.build(PERSON, DOSSIER, "baja", None, None))
    assert "Ada Ruiz" in text
    assert "Dijo en" not in text


def test_the_quote_cannot_break_the_card():
    """El texto lo escribió un tercero: no puede romper el formato ni inyectar
    bloques."""
    hostile = {"text": "```\n*/ }] injected", "ts": "1.0"}
    blocks = card.build(PERSON, DOSSIER, "alta", hostile, None)
    assert isinstance(blocks, list)
    assert "injected" in blocks_text(blocks)


def _mrkdwn_texts(blocks):
    """Todos los valores de texto (mrkdwn y plain_text) de la tarjeta, para
    poder afirmar tanto ausencias como la falta de strings vacíos."""
    texts = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") in ("mrkdwn", "plain_text") and "text" in node:
                texts.append(node["text"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(blocks)
    return texts


def test_a_channel_mention_in_the_quote_is_escaped():
    """`<!channel>` sin escapar avisaría a todo el canal de Handoff en cuanto
    se publica la tarjeta: Slack lo interpreta como mención viva."""
    hostile = {"text": "<!channel> free money, ping <@U123> now", "ts": "1.0"}
    text = blocks_text(card.build(PERSON, DOSSIER, "alta", hostile, None))
    assert "<!channel>" not in text
    assert "<@U123>" not in text
    assert "&lt;!channel&gt;" in text
    assert "&lt;@U123&gt;" in text


def test_a_hostile_signal_does_not_produce_link_syntax():
    """`hecho`/`fuente` son salida de un modelo sobre páginas ajenas: un '|'
    o un '<' ahí no debe poder cerrar el enlace antes de tiempo."""
    dossier = {
        **DOSSIER,
        "senales_contexto": [
            {"hecho": "click here|<!channel>", "fuente": "https://evil.example|hack"}
        ],
    }
    text = blocks_text(card.build(PERSON, dossier, "alta", None, None))
    assert "<https://evil.example|hack" not in text
    assert "&lt;!channel&gt;" in text
    assert "click here" in text


def test_an_overlong_resumen_is_trimmed():
    dossier = {**DOSSIER, "resumen": "x" * (card.RESUMEN_CHARS + 500)}
    blocks = card.build(PERSON, dossier, "alta", None, None)
    context = next(b for b in blocks if b["type"] == "context")
    assert len(context["elements"][0]["text"]) <= card.RESUMEN_CHARS


def test_an_overlong_hecho_is_trimmed():
    dossier = {
        **DOSSIER,
        "senales_contexto": [
            {"hecho": "y" * (card.HECHO_CHARS + 500), "fuente": "https://tc.com/a"}
        ],
    }
    text = blocks_text(card.build(PERSON, dossier, "alta", None, None))
    assert "y" * (card.HECHO_CHARS + 1) not in text


def _porque_importa_text(blocks):
    section = next(
        b for b in blocks if b["type"] == "section" and "Por qué importa" in b["text"]["text"]
    )
    return section["text"]["text"]


def test_a_resumen_of_characters_that_expand_when_escaped_stays_under_the_context_cap():
    """`_escape_mrkdwn` puede quintuplicar la longitud ('&' -> '&amp;'): recortar
    el crudo y escapar después no ata nada. El tope tiene que aplicarse al
    texto escapado, que es el que de verdad llega a Slack."""
    dossier = {**DOSSIER, "resumen": "&" * 1000}
    blocks = card.build(PERSON, dossier, "alta", None, None)
    context = next(b for b in blocks if b["type"] == "context")
    text = context["elements"][0]["text"]
    assert len(text) <= card.RESUMEN_CHARS
    assert len(text) <= 2000  # tope real de Slack para un bloque context
    assert not text.endswith("&am") and not text.endswith("&amp")


def test_hechos_of_characters_that_expand_when_escaped_keep_the_section_under_the_cap():
    dossier = {
        **DOSSIER,
        "senales_contexto": [
            {"hecho": "&" * 240, "fuente": None},
            {"hecho": "&" * 240, "fuente": None},
            {"hecho": "&" * 240, "fuente": None},
        ],
    }
    blocks = card.build(PERSON, dossier, "alta", None, None)
    text = _porque_importa_text(blocks)
    assert len(text) <= 3000  # tope real de Slack para un bloque section


def test_a_long_razon_stays_under_the_section_cap():
    dossier = {**DOSSIER, "encaje_handoff": {"puntuacion": 3, "razon": "&" * 5000}}
    blocks = card.build(PERSON, dossier, "alta", None, None)
    text = _porque_importa_text(blocks)
    assert len(text) <= 3000


def test_a_long_fuente_stays_under_the_section_cap():
    dossier = {
        **DOSSIER,
        "senales_contexto": [
            {"hecho": "Levantó Series A", "fuente": "https://tc.com/" + "a" * 5000}
        ],
    }
    blocks = card.build(PERSON, dossier, "alta", None, None)
    text = _porque_importa_text(blocks)
    assert len(text) <= 3000


def test_a_fully_hostile_card_stays_under_every_slack_block_limit():
    """Cabecera, cita, razón, tres señales, la línea de contratación y el
    resumen, todos largos y con caracteres que se expanden al escaparse: la
    tarjeta entera debe seguir cabiendo en los topes de Slack por bloque."""
    hostile_message = {"text": "&" * 2000, "ts": "1.0"}
    hostile_dossier = {
        **DOSSIER,
        "encaje_handoff": {"puntuacion": 3, "razon": "&" * 5000},
        "senales_contexto": [
            {"hecho": "<" * 1000, "fuente": "https://tc.com/" + "a" * 5000},
            {"hecho": ">" * 1000, "fuente": "https://tc.com/" + "b" * 5000},
            {"hecho": "&" * 1000, "fuente": "https://tc.com/" + "c" * 5000},
        ],
        "resumen": "&" * 5000,
    }
    blocks = card.build(PERSON, hostile_dossier, "alta", hostile_message, "https://slack.com/p1")
    for block in blocks:
        if block["type"] == "section":
            assert len(block["text"]["text"]) <= 3000
        elif block["type"] == "context":
            for element in block["elements"]:
                assert len(element["text"]) <= 2000


def test_no_block_text_is_ever_the_empty_string():
    """Slack rechaza cualquier bloque cuyo valor de texto sea la cadena vacía."""
    for blocks in (
        card.build(PERSON, DOSSIER, "alta", MESSAGE, "https://slack.com/p1"),
        card.build(PERSON, {**DOSSIER, "persona": None}, "alta", MESSAGE, None),
        card.build(PERSON, {**DOSSIER, "empresa": None}, "alta", MESSAGE, None),
        card.build(PERSON, {**DOSSIER, "contratacion": None}, "alta", MESSAGE, None),
        card.build(PERSON, {**DOSSIER, "senales_contexto": []}, "alta", MESSAGE, None),
        card.build(PERSON, {**DOSSIER, "resumen": None}, "alta", MESSAGE, None),
        card.build(
            {**PERSON, "full_name": None, "company_name": None},
            {"persona": None, "empresa": None},
            "alta",
            None,
            None,
        ),
    ):
        assert all(text != "" for text in _mrkdwn_texts(blocks))


def test_missing_persona_still_falls_back_to_the_person_record():
    dossier = {**DOSSIER, "persona": None}
    text = blocks_text(card.build(PERSON, dossier, "alta", MESSAGE, None))
    assert "Ada Ruiz" in text


def test_missing_empresa_still_falls_back_to_the_person_record():
    dossier = {**DOSSIER, "empresa": None}
    text = blocks_text(card.build(PERSON, dossier, "alta", MESSAGE, None))
    assert "Acme" in text


def test_missing_contratacion_produces_no_vacantes_line():
    dossier = {**DOSSIER, "contratacion": None}
    text = blocks_text(card.build(PERSON, dossier, "alta", MESSAGE, None))
    assert "vacantes abiertas" not in text


def test_empty_senales_contexto_produces_no_signal_bullets():
    dossier = {**DOSSIER, "senales_contexto": []}
    text = blocks_text(card.build(PERSON, dossier, "alta", MESSAGE, None))
    assert "tc.com" not in text


def test_resumen_null_produces_no_context_block():
    dossier = {**DOSSIER, "resumen": None}
    blocks = card.build(PERSON, dossier, "alta", MESSAGE, None)
    assert all(b["type"] != "context" for b in blocks)


def test_person_without_full_name_or_company_name_still_gets_a_headline():
    person = {**PERSON, "full_name": None, "company_name": None}
    dossier = {"persona": None, "empresa": None}
    blocks = card.build(person, dossier, "alta", None, None)
    header = next(b for b in blocks if b["type"] == "section")
    assert header["text"]["text"].strip() != ""
    assert "U1" in header["text"]["text"]


def test_a_fully_hostile_card_never_leaks_syntax_or_breaks_a_block_limit():
    """Ataca cada campo de `person`, `dossier` y `message` a la vez: strings
    hostiles larguísimos en todos los campos de texto, y strings hostiles
    (con pinta de número, pero no números) en los campos que deberían ser
    `int`. Una excepción deliberada: una señal lleva una `fuente` que sí es
    una URL segura, para comprobar que el único `<...|...>` que sobrevive es
    el de un enlace validado, no cualquier sintaxis de enlace colada por un
    campo hostil.

    Recorre el payload entero con `_mrkdwn_texts` en vez de indexar bloques
    concretos, para que un campo nuevo quede cubierto solo con añadirlo a
    los fixtures de este test."""
    hostile_text = "<!channel> <@U1> <#C1|x> & " * 400
    hostile_number = "50<!channel>"
    unsafe_fuente = "https://evil.example|hack"
    safe_fuente = "https://safe.example/a"

    person = {
        "id": "11111111-1111-1111-1111-111111111111",
        "full_name": hostile_text,
        "company_name": hostile_text,
        "slack_user_id": hostile_text,
    }
    dossier = {
        "persona": {"nombre": hostile_text, "cargo": hostile_text},
        "empresa": {"nombre": hostile_text, "empleados_aprox": hostile_number},
        "contratacion": {
            "vacantes_abiertas": hostile_number,
            "roles_deslocalizables": [hostile_text, hostile_text],
        },
        "senales_contexto": [
            {"hecho": hostile_text, "fuente": unsafe_fuente},
            {"hecho": hostile_text, "fuente": hostile_text},
            {"hecho": hostile_text, "fuente": safe_fuente},
        ],
        "encaje_handoff": {"puntuacion": hostile_number, "razon": hostile_text},
        "resumen": hostile_text,
    }
    message = {"text": hostile_text, "ts": hostile_text}

    blocks = card.build(person, dossier, "alta", message, "https://slack.com/p1")

    for block in blocks:
        if block["type"] == "section":
            assert len(block["text"]["text"]) <= 3000
        elif block["type"] == "context":
            for element in block["elements"]:
                assert len(element["text"]) <= 2000

    for text in _mrkdwn_texts(blocks):
        assert text != ""
        assert "<!" not in text
        assert "<@" not in text
        assert "<#" not in text
        for url in re.findall(r"<([^<>|]*)\|[^<>]*>", text):
            assert card._is_safe_link(url), f"link syntax with an unsafe url: {url!r}"
