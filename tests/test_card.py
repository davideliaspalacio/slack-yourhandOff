import json

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
