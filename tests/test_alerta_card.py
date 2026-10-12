"""La tarjeta de alerta de señal: contenido, botones y defensas."""

import json
import re

import pytest

from handoff_agent.delivery import alerta_card, card
from tests.test_card import (
    HOSTILE_URL_QUOTES,
    _assert_card_under_limits_no_syntax_leak,
    _mrkdwn_texts,
    assert_quote_is_not_autolinkable,
)

PERSON = {
    "id": "11111111-1111-1111-1111-111111111111",
    "full_name": "Ada Ruiz",
    "company_name": "Acme",
    "slack_user_id": "U1",
    "state": "investigado",
}
SENAL = {
    "slack_user_id": "U1",
    "tipo": "contratacion",
    "cita": "We're hiring a support rep this month",
}
DOSSIER = {"encaje_handoff": {"puntuacion": 2, "razon": "r"}}
PERMALINK = "https://foundersclub.slack.com/archives/C1/p1726000000000100"


def text_of(blocks):
    return json.dumps(blocks, ensure_ascii=False)


def actions_of(blocks):
    return next(b for b in blocks if b["type"] == "actions")["elements"]


def test_the_header_names_the_kind_of_ask_in_english():
    blocks = alerta_card.build(SENAL, PERSON, None, PERMALINK)
    header = blocks[0]["text"]["text"]
    assert header.splitlines()[0] == "🗣️ *THEY ASKED · hiring*"
    assert "*Ada Ruiz* — Acme" in header


def test_every_signal_type_has_a_readable_english_label():
    for tipo in alerta_card.TIPO_LABELS:
        label = alerta_card.tipo_label(tipo)
        assert label and "_" not in label.replace("back office", "")
    assert alerta_card.tipo_label("back_office") == "back office"
    assert alerta_card.tipo_label("soporte") == "support"
    # Un tipo desconocido (o no-string) no se reproduce tal cual.
    assert alerta_card.tipo_label("<!channel>") == "other"
    assert alerta_card.tipo_label(None) == "other"


def test_the_quote_is_a_block_quote_and_escaped():
    senal = {**SENAL, "cita": "ping <!channel> & <@U1>"}
    text = text_of(alerta_card.build(senal, PERSON, None, None))
    assert "> `ping &lt;!channel&gt; &amp; &lt;@U1&gt;`" in text
    assert "<!channel>" not in text


@pytest.mark.parametrize("cita", HOSTILE_URL_QUOTES)
def test_a_url_in_the_quote_is_not_autolinkable(cita):
    """Un enlace de phishing en la cita no puede llegar al CEO como enlace clicable."""
    blocks = alerta_card.build({**SENAL, "cita": cita}, PERSON, None, None)
    assert_quote_is_not_autolinkable(blocks)
    assert any(
        "handoff-relink.co" in b["text"]["text"] or "evil.co" in b["text"]["text"]
        for b in blocks
        if b["type"] == "section" and b["text"]["text"].startswith(">")
    )


def test_with_a_dossier_a_line_shows_the_fit_and_the_state():
    blocks = alerta_card.build(SENAL, PERSON, DOSSIER, None)
    context = next(b for b in blocks if b["type"] == "context")
    assert context["elements"][0]["text"] == "Dossier: fit 2/3 · Researched"
    contacted = alerta_card.build(SENAL, {**PERSON, "state": "contactado"}, DOSSIER, None)
    assert "fit 2/3 · Contacted" in text_of(contacted)


def test_a_dossier_without_a_real_fit_number_says_so_instead_of_printing_it():
    dossier = {"encaje_handoff": {"puntuacion": "3<!channel>"}}
    text = text_of(alerta_card.build(SENAL, PERSON, dossier, None))
    assert "no fit score" in text
    assert "<!channel>" not in text


def test_without_a_dossier_it_says_the_research_is_on_its_way():
    blocks = alerta_card.build(SENAL, PERSON, None, None)
    context = next(b for b in blocks if b["type"] == "context")
    assert "Research is on its way" in context["elements"][0]["text"]
    assert "fit" not in context["elements"][0]["text"]


def test_a_sender_who_is_not_a_prospect_yet_shows_the_slack_id():
    blocks = alerta_card.build(SENAL, None, None, PERMALINK)
    assert "*U1*" in blocks[0]["text"]["text"]


def test_the_buttons_reuse_the_research_card_actions(monkeypatch):
    monkeypatch.setenv("PANEL_URL", "https://panel.example.com")
    elements = actions_of(alerta_card.build(SENAL, PERSON, DOSSIER, PERMALINK))
    assert [e["action_id"] for e in elements] == [
        "contactado",
        "descartar",
        "abrir_panel",
        "ver_original",
    ]
    assert [e["text"]["text"] for e in elements] == [
        "Contacted",
        "Discard",
        "Open in panel",
        "View original message",
    ]
    assert elements[0]["value"] == elements[1]["value"] == PERSON["id"]
    assert elements[2]["url"] == f"https://panel.example.com/personas/{PERSON['id']}"
    assert elements[3]["url"] == PERMALINK
    # Los mismos action_id y estilo que la tarjeta de research: el receptor no cambia.
    research = card.build(PERSON, {**DOSSIER, "resumen": None}, "alta", None, None)
    research_buttons = [
        e for e in actions_of(research) if e["action_id"] in ("contactado", "descartar")
    ]
    assert elements[:2] == research_buttons


def test_no_panel_button_without_panel_url_and_no_view_button_without_permalink(monkeypatch):
    monkeypatch.delenv("PANEL_URL", raising=False)
    elements = actions_of(alerta_card.build(SENAL, PERSON, DOSSIER, None))
    assert [e["action_id"] for e in elements] == ["contactado", "descartar"]


def test_an_unsafe_permalink_gets_no_button():
    elements = actions_of(alerta_card.build(SENAL, PERSON, None, "https://x.com/a|<!channel>"))
    assert all(e["action_id"] != "ver_original" for e in elements)


def test_without_a_prospect_there_are_no_buttons_the_receiver_could_not_apply(monkeypatch):
    monkeypatch.setenv("PANEL_URL", "https://panel.example.com")
    elements = actions_of(alerta_card.build(SENAL, None, None, PERMALINK))
    assert [e["action_id"] for e in elements] == ["ver_original"]


def test_no_actions_block_at_all_when_there_is_nothing_to_click(monkeypatch):
    monkeypatch.delenv("PANEL_URL", raising=False)
    blocks = alerta_card.build(SENAL, None, None, None)
    assert all(b["type"] != "actions" for b in blocks)


def test_a_card_without_quote_has_no_empty_block():
    blocks = alerta_card.build({**SENAL, "cita": "  "}, PERSON, None, None)
    assert all(t != "" for t in _mrkdwn_texts(blocks))
    assert len([b for b in blocks if b["type"] == "section"]) == 1


def test_the_notification_text_is_short_and_escaped():
    assert alerta_card.summary(SENAL, PERSON) == "They asked: hiring · Ada Ruiz"
    hostile = alerta_card.summary({**SENAL, "slack_user_id": "<!channel>"}, None)
    assert "<!channel>" not in hostile


def test_the_alert_card_has_no_spanish_labels():
    text = text_of(alerta_card.build(SENAL, PERSON, DOSSIER, PERMALINK))
    for leftover in ("Contactado", "Descartar", "Dijo", "encaje", "investigación"):
        assert leftover not in text


def test_a_fully_hostile_alert_card_never_leaks_syntax_or_breaks_a_limit(monkeypatch):
    """Cada campo de texto de la señal, la persona y el dossier, hostil y
    larguísimo; los numéricos, con pinta de número pero sin serlo. Mismo helper
    que el test de propiedades de la tarjeta de research."""
    monkeypatch.setenv("PANEL_URL", "https://panel.example.com")
    hostile = "<!channel> <@U1> <#C1|x> & " * 400
    person = {
        "id": "11111111-1111-1111-1111-111111111111",
        "full_name": hostile,
        "company_name": hostile,
        "slack_user_id": hostile,
        "state": hostile,
    }
    senal = {"slack_user_id": hostile, "tipo": hostile, "cita": hostile}
    for dossier in (
        {
            "persona": {"nombre": hostile},
            "empresa": {"nombre": hostile},
            "encaje_handoff": {"puntuacion": "50<!channel>"},
        },
        {"encaje_handoff": {"puntuacion": 10**40}},
        {"encaje_handoff": None},
        None,
    ):
        for who in (person, None, {**person, "full_name": None, "company_name": None}):
            blocks = alerta_card.build(senal, who, dossier, "https://slack.com/p1")
            _assert_card_under_limits_no_syntax_leak(blocks)
    # Y la cita hostil de verdad cabe en su bloque.
    for url_quote in HOSTILE_URL_QUOTES:
        assert_quote_is_not_autolinkable(
            alerta_card.build({**senal, "cita": url_quote}, person, None, None)
        )
    big = alerta_card.build({**senal, "cita": "&" * 5000}, person, None, None)
    assert all(len(b["text"]["text"]) <= 3000 for b in big if b["type"] == "section")
    assert not re.search(r"&(?!amp;|lt;|gt;)", "".join(_mrkdwn_texts(big)))
