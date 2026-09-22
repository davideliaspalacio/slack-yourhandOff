import json
import re
import uuid

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
    assert [e["action_id"] for e in actions[:2]] == ["contactado", "descartar"]
    assert all(e["value"] == PERSON["id"] for e in actions[:2])
    link = actions[2]
    assert link["url"] == "https://slack.com/p1"


def test_there_is_no_research_more_button_anymore():
    """Task 2: ese trabajo se mudó al panel ("Help the research")."""
    blocks = card.build(PERSON, DOSSIER, "alta", MESSAGE, "https://slack.com/p1")
    actions = next(b for b in blocks if b["type"] == "actions")["elements"]
    assert "investigar_mas" not in [e.get("action_id") for e in actions]


def test_without_a_permalink_there_is_no_broken_button():
    blocks = card.build(PERSON, DOSSIER, "media", MESSAGE, None)
    actions = next(b for b in blocks if b["type"] == "actions")["elements"]
    assert all("url" not in e for e in actions)


def test_the_panel_button_appears_when_panel_url_is_configured(monkeypatch):
    monkeypatch.setenv("PANEL_URL", "https://panel.example.com")
    blocks = card.build(PERSON, DOSSIER, "alta", MESSAGE, None)
    actions = next(b for b in blocks if b["type"] == "actions")["elements"]
    panel_button = next(e for e in actions if e["action_id"] == "abrir_panel")
    assert panel_button["url"] == f"https://panel.example.com/personas/{PERSON['id']}"
    assert panel_button["text"] == {"type": "plain_text", "text": "Open in panel"}


def test_the_panel_button_is_absent_without_panel_url(monkeypatch):
    monkeypatch.delenv("PANEL_URL", raising=False)
    blocks = card.build(PERSON, DOSSIER, "alta", MESSAGE, None)
    actions = next(b for b in blocks if b["type"] == "actions")["elements"]
    assert all(e.get("action_id") != "abrir_panel" for e in actions)


def test_the_panel_button_and_the_original_message_button_can_coexist(monkeypatch):
    monkeypatch.setenv("PANEL_URL", "https://panel.example.com")
    blocks = card.build(PERSON, DOSSIER, "alta", MESSAGE, "https://slack.com/p1")
    actions = next(b for b in blocks if b["type"] == "actions")["elements"]
    url_action_ids = [e["action_id"] for e in actions if "url" in e]
    assert url_action_ids == ["abrir_panel", "ver_original"]


def test_the_panel_url_with_a_trailing_slash_does_not_double_it(monkeypatch):
    """El recorte de la barra final vive en config.load_settings (PANEL_URL);
    aquí solo se comprueba que la tarjeta no acaba con dos barras seguidas."""
    monkeypatch.setenv("PANEL_URL", "https://panel.example.com/")
    blocks = card.build(PERSON, DOSSIER, "alta", MESSAGE, None)
    actions = next(b for b in blocks if b["type"] == "actions")["elements"]
    panel_button = next(e for e in actions if e["action_id"] == "abrir_panel")
    assert panel_button["url"] == f"https://panel.example.com/personas/{PERSON['id']}"


def test_the_panel_button_uses_the_stringified_uuid(monkeypatch):
    monkeypatch.setenv("PANEL_URL", "https://panel.example.com")
    person = {**PERSON, "id": uuid.UUID(PERSON["id"])}
    blocks = card.build(person, DOSSIER, "alta", MESSAGE, None)
    actions = next(b for b in blocks if b["type"] == "actions")["elements"]
    panel_button = next(e for e in actions if e["action_id"] == "abrir_panel")
    assert panel_button["url"] == f"https://panel.example.com/personas/{PERSON['id']}"


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
        b for b in blocks if b["type"] == "section" and "Why it matters" in b["text"]["text"]
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


def test_a_fully_hostile_card_stays_under_every_slack_block_limit(monkeypatch):
    """Cabecera, cita, razón, tres señales, la línea de contratación y el
    resumen, todos largos y con caracteres que se expanden al escaparse: la
    tarjeta entera debe seguir cabiendo en los topes de Slack por bloque.

    PANEL_URL configurado a la vez que el permalink: los tres botones de
    enlace (panel, mensaje original) más los dos de acción conviven sin que
    nada del dossier hostil los afecte -- solo person['id'] y settings.panel_url
    entran en esa URL, nunca un campo del dossier."""
    monkeypatch.setenv("PANEL_URL", "https://panel.example.com")
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
        "empresa_no_confirmada": {"dominio_adivinado": "<!channel>&" * 200},
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
    assert "open roles" not in text


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


def _assert_card_under_limits_no_syntax_leak(blocks):
    """Helper: Assert that a card doesn't exceed Slack limits or leak mrkdwn syntax.

    Extracted from test_a_fully_hostile_card_never_leaks_syntax_or_breaks_a_block_limit
    to be reused in multiple test cases that probe different fallback paths."""
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


def test_a_fully_hostile_card_never_leaks_syntax_or_breaks_a_block_limit(monkeypatch):
    """Ataca cada campo de `person`, `dossier` y `message` a la vez: strings
    hostiles larguísimos en todos los campos de texto, y strings hostiles
    (con pinta de número, pero no números) en los campos que deberían ser
    `int`. Una excepción deliberada: una señal lleva una `fuente` que sí es
    una URL segura, para comprobar que el único `<...|...>` que sobrevive es
    el de un enlace validado, no cualquier sintaxis de enlace colada por un
    campo hostil.

    PANEL_URL también configurado aquí: el botón "Open in panel" se construye
    solo a partir de settings.panel_url y person['id'] (un UUID fijo en este
    test), así que ningún campo hostil del dossier puede llegar a esa URL.

    Recorre el payload entero con `_mrkdwn_texts` en vez de indexar bloques
    concretos, para que un campo nuevo quede cubierto solo con añadirlo a
    los fixtures de este test."""
    monkeypatch.setenv("PANEL_URL", "https://panel.example.com")
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
        "empresa_no_confirmada": {"dominio_adivinado": hostile_text},
    }
    message = {"text": hostile_text, "ts": hostile_text}

    blocks = card.build(person, dossier, "alta", message, "https://slack.com/p1")
    _assert_card_under_limits_no_syntax_leak(blocks)


PROVEEDOR = {
    "empleados_linkedin": 16679,
    # 13 meses de serie: de 13.440 a 16.679 es +24 %.
    "evolucion_mensual": [{"mes": f"m{i}", "empleados": 13440} for i in range(12)]
    + [{"mes": "m12", "empleados": 16679}],
    "empleados_crm": 8000,
    "crecimiento": [
        {"meses": 6, "cambio_neto": 12, "porcentaje": 0.072},
        {"meses": 12, "cambio_neto": 24, "porcentaje": 0.1439},
        {"meses": 24, "cambio_neto": 55, "porcentaje": 0.3298},
    ],
    "empleados_por_area": {
        "support": 334,
        "operations": 1260,
        "sales": 1136,
        "engineering": 4202,
    },
}


def test_the_provider_block_has_the_expected_content():
    dossier = {**DOSSIER, "datos_proveedor": PROVEEDOR}
    blocks = card.build(PERSON, dossier, "alta", None, None)
    context_texts = [b["elements"][0]["text"] for b in blocks if b["type"] == "context"]
    text = next(t for t in context_texts if "Provider data (unverified)" in t)
    assert text == (
        "Provider data (unverified): 16,679 employees · +24% in 12 months · "
        "support 334 · operations 1,260 · sales 1,136"
    )


def test_without_a_monthly_series_the_provider_growth_is_already_a_percentage():
    """0.1439 del proveedor es 0,14 %, no 14 %."""
    proveedor = {k: v for k, v in PROVEEDOR.items() if k != "evolucion_mensual"}
    blocks = card.build(PERSON, {**DOSSIER, "datos_proveedor": proveedor}, "alta", None, None)
    assert "+0.1% in 12 months" in blocks_text(blocks)
    assert "+14%" not in blocks_text(blocks)


def test_no_provider_block_when_the_dossier_has_no_provider_data():
    blocks = card.build(PERSON, DOSSIER, "alta", None, None)
    assert "Proveedor" not in blocks_text(blocks)


def test_no_provider_block_when_nothing_usable_survives():
    dossier = {**DOSSIER, "datos_proveedor": {"fuente": "proveedor externo (sin verificar)"}}
    blocks = card.build(PERSON, dossier, "alta", None, None)
    assert "Proveedor" not in blocks_text(blocks)


def test_no_provider_block_when_datos_proveedor_is_not_a_dict():
    dossier = {**DOSSIER, "datos_proveedor": "not a dict"}
    blocks = card.build(PERSON, dossier, "alta", None, None)
    assert "Proveedor" not in blocks_text(blocks)


def test_provider_block_falls_back_to_the_crm_employee_count():
    dossier = {**DOSSIER, "datos_proveedor": {"empleados_crm": 8000}}
    text = blocks_text(card.build(PERSON, dossier, "alta", None, None))
    assert "8,000 employees" in text


def test_provider_block_ignores_a_boolean_disguised_as_an_employee_count():
    dossier = {**DOSSIER, "datos_proveedor": {"empleados_linkedin": True}}
    blocks = card.build(PERSON, dossier, "alta", None, None)
    assert "Proveedor" not in blocks_text(blocks)


def test_provider_block_falls_back_to_top_areas_by_count_when_priority_areas_are_absent():
    dossier = {
        **DOSSIER,
        "datos_proveedor": {"empleados_por_area": {"finance": 5, "legal": 50, "marketing": 20}},
    }
    text = blocks_text(card.build(PERSON, dossier, "alta", None, None))
    assert "legal 50" in text
    assert "marketing 20" in text
    assert "finance 5" in text


def test_provider_block_only_shows_the_12_month_growth_entry():
    dossier = {
        **DOSSIER,
        "datos_proveedor": {"crecimiento": [{"meses": 24, "cambio_neto": 1, "porcentaje": 0.5}]},
    }
    text = blocks_text(card.build(PERSON, dossier, "alta", None, None))
    assert "in 24 months" not in text
    assert "in 12 months" not in text


def test_a_hostile_datos_proveedor_never_leaks_syntax_or_breaks_a_block_limit():
    """`datos_proveedor` viene de un webhook de terceros: mismo trato hostil
    que el resto del dossier. Se deja una cifra válida (`empleados_linkedin`)
    para forzar que el bloque exista y de verdad se someta a las cifras y
    claves hostiles del resto de campos."""
    hostile_text = "<!channel> <@U1> <#C1|x> & " * 400
    hostile_number = "50<!channel>"
    dossier = {
        **DOSSIER,
        "datos_proveedor": {
            "empleados_linkedin": 16679,
            "empleados_crm": hostile_number,
            "crecimiento": [{"meses": 12, "cambio_neto": hostile_number, "porcentaje": True}],
            "empleados_por_area": {hostile_text: 5, "support": hostile_number},
        },
    }
    blocks = card.build(PERSON, dossier, "alta", None, None)
    _assert_card_under_limits_no_syntax_leak(blocks)
    context_texts = [b["elements"][0]["text"] for b in blocks if b["type"] == "context"]
    assert any("Provider data (unverified)" in t for t in context_texts)


def test_a_realistic_hostile_datos_proveedor_still_renders_and_stays_safe():
    """Con formas válidas pero con claves de área hostiles, el bloque debe
    seguir cabiendo en el tope de un bloque `context` y no filtrar sintaxis."""
    hostile_key = ("mala" * 100)[:40]
    dossier = {
        **DOSSIER,
        "datos_proveedor": {
            "empleados_linkedin": 16679,
            "empleados_por_area": {hostile_key: 5000000000000},
        },
    }
    blocks = card.build(PERSON, dossier, "alta", None, None)
    _assert_card_under_limits_no_syntax_leak(blocks)
    context_texts = [b["elements"][0]["text"] for b in blocks if b["type"] == "context"]
    assert any("Provider data (unverified)" in t for t in context_texts)


def test_fallback_to_person_full_name_when_persona_missing_is_escaped():
    """When persona and empresa are missing, person.full_name and person.company_name
    are used for the header. They must be escaped and bounded even with hostile payloads."""
    hostile_text = "<!channel> <@U1> <#C1|x> & " * 400

    person = {
        "id": "11111111-1111-1111-1111-111111111111",
        "full_name": hostile_text,
        "company_name": hostile_text,
        "slack_user_id": "U1",
    }
    dossier = {
        "persona": None,
        "empresa": None,
        "contratacion": None,
        "senales_contexto": [],
        "encaje_handoff": {"puntuacion": 1, "razon": "test"},
        "resumen": None,
    }

    blocks = card.build(person, dossier, "alta", None, None)
    _assert_card_under_limits_no_syntax_leak(blocks)

    # Prove fallback full_name was rendered by checking for escaped hostile payload.
    # Use a small distinctive substring that's guaranteed to survive the NAME_CHARS cap.
    text = blocks_text(blocks)
    assert "&lt;!channel&gt;" in text, "Fallback full_name not rendered in card"


def test_fallback_to_person_slack_user_id_when_full_name_missing_is_escaped():
    """When full_name and persona are missing, person.slack_user_id is used as the
    name. It must be escaped and bounded even with hostile payloads."""
    hostile_text = "<!channel> <@U1> <#C1|x> & " * 400

    person = {
        "id": "11111111-1111-1111-1111-111111111111",
        "full_name": None,
        "company_name": hostile_text,
        "slack_user_id": hostile_text,
    }
    dossier = {
        "persona": None,
        "empresa": None,
        "contratacion": None,
        "senales_contexto": [],
        "encaje_handoff": {"puntuacion": 1, "razon": "test"},
        "resumen": None,
    }

    blocks = card.build(person, dossier, "alta", None, None)
    _assert_card_under_limits_no_syntax_leak(blocks)

    # Prove fallback slack_user_id was rendered by checking for escaped hostile payload.
    # Use a small distinctive substring that's guaranteed to survive the NAME_CHARS cap.
    text = blocks_text(blocks)
    assert "&lt;!channel&gt;" in text, "Fallback slack_user_id not rendered in card"


def test_the_rendered_card_has_no_leftover_spanish_labels():
    """Every user-visible string in the card must be in English (this task's
    requirement): a fully populated card (quote, reasons, hiring line, provider
    block and buttons all present) must not contain any of the old Spanish
    labels.

    Uses English-content dossier text (as the translated synthesize.py system
    prompt now requires from the LLM) instead of the shared `DOSSIER` fixture,
    whose free-text fields (`resumen`, `razon`) are deliberately Spanish for
    other tests and would otherwise trip this check on data, not on a label.
    """
    english_dossier = {
        "persona": {"nombre": "Ada Ruiz", "cargo": "CEO"},
        "empresa": {"nombre": "Acme", "empleados_aprox": 60},
        "contratacion": {"vacantes_abiertas": 6, "roles_deslocalizables": ["support", "ops"]},
        "senales_contexto": [
            {"hecho": "Raised a Series A 3 months ago", "fuente": "https://tc.com/a"}
        ],
        "encaje_handoff": {"puntuacion": 3, "razon": "Hiring support right now"},
        "resumen": "CEO of Acme, 60 people, hiring support.",
        "datos_proveedor": PROVEEDOR,
    }
    blocks = card.build(PERSON, english_dossier, "alta", MESSAGE, "https://slack.com/p1")
    text = blocks_text(blocks)
    spanish_leftovers = [
        "Por qué importa",
        "Dijo en",
        "Descartar",
        "Contactado",
        "Investigar",
        "vacantes",
        "personas",
        "Proveedor",
        "empleados",
        "encaje",
    ]
    for leftover in spanish_leftovers:
        assert leftover not in text, f"leftover Spanish label found in the card: {leftover!r}"


def test_unconfirmed_domain_warning_is_shown_when_present():
    dossier = {**DOSSIER, "empresa_no_confirmada": {"dominio_adivinado": "handoff.ai"}}
    text = blocks_text(card.build(PERSON, dossier, "media", None, None))
    assert "Company not confirmed" in text
    assert "handoff.ai" in text
    assert "Set the right website in the panel and re-research." in text


def test_no_unconfirmed_domain_warning_when_absent():
    text = blocks_text(card.build(PERSON, DOSSIER, "alta", None, None))
    assert "Company not confirmed" not in text


def test_unconfirmed_domain_warning_ignored_when_not_a_dict():
    dossier = {**DOSSIER, "empresa_no_confirmada": "handoff.ai"}
    text = blocks_text(card.build(PERSON, dossier, "alta", None, None))
    assert "Company not confirmed" not in text


def test_unconfirmed_domain_warning_ignored_without_a_usable_domain():
    dossier = {**DOSSIER, "empresa_no_confirmada": {"dominio_adivinado": ""}}
    text = blocks_text(card.build(PERSON, dossier, "alta", None, None))
    assert "Company not confirmed" not in text


def test_a_uuid_id_from_the_database_is_serialisable():
    """psycopg devuelve prospects.id como uuid.UUID, no como str: sin
    convertirlo, slack_sdk no puede serializar la tarjeta y no sale ninguna."""
    person = {**PERSON, "id": uuid.UUID(PERSON["id"])}
    blocks = card.build(person, DOSSIER, "alta", MESSAGE, "https://slack.com/p1")
    actions = next(b for b in blocks if b["type"] == "actions")["elements"]
    assert all(e["value"] == PERSON["id"] for e in actions[:2])
    json.dumps(blocks)
