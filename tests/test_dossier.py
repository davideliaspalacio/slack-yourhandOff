from decimal import Decimal

from handoff_agent.dossier import DossierInvalid, cited_sources, validate_dossier

SOURCES = {"https://acme.com/", "https://acme.com/careers", "https://jobs.example/1"}


def make_dossier(**overrides) -> dict:
    data = {
        "persona": {"nombre": "Ada Ruiz", "cargo": "CEO", "fuente": "https://acme.com/"},
        "empresa": {
            "nombre": "Acme",
            "dominio": "acme.com",
            "sector": "SaaS",
            "empleados_aprox": 60,
            "ubicacion": "Austin, TX",
            "descripcion": "Software de logística.",
            "fuentes": ["https://acme.com/"],
        },
        "contratacion": {
            "vacantes_abiertas": 1,
            "roles": ["Support Lead"],
            "roles_deslocalizables": ["Support Lead"],
            "fuentes": ["https://jobs.example/1"],
        },
        "senales_contexto": [{"hecho": "Contrata soporte", "fuente": "https://acme.com/careers"}],
        "encaje_handoff": {"puntuacion": 2, "razon": "Contrata soporte, que Handoff cubre."},
        "huecos": [],
        "busquedas_sugeridas": [],
        "resumen": "CEO de una SaaS de 60 personas que contrata soporte.",
    }
    data.update(overrides)
    return data


def test_a_grounded_dossier_passes():
    assert validate_dossier(make_dossier(), SOURCES) == []


def test_missing_sections_are_reported():
    data = make_dossier()
    del data["empresa"]
    assert any("empresa" in p for p in validate_dossier(data, SOURCES))


def test_an_invented_source_is_rejected():
    data = make_dossier(
        senales_contexto=[{"hecho": "Levantó $20M", "fuente": "https://inventada.com/x"}]
    )
    problems = validate_dossier(data, SOURCES)
    assert any("https://inventada.com/x" in p for p in problems)


def test_null_sources_are_allowed_for_unknown_facts():
    data = make_dossier(persona={"nombre": "Ada Ruiz", "cargo": None, "fuente": None})
    assert validate_dossier(data, SOURCES) == []


def test_fit_score_must_be_an_integer_from_0_to_3():
    for bad in (4, -1, "3", 2.5, None):
        data = make_dossier(encaje_handoff={"puntuacion": bad, "razon": "x"})
        assert validate_dossier(data, SOURCES), bad


def test_empty_summary_is_rejected():
    assert validate_dossier(make_dossier(resumen="  "), SOURCES)


def test_cited_sources_walks_nested_sections():
    paths = dict(cited_sources(make_dossier()))
    assert paths["senales_contexto[0].fuente"] == "https://acme.com/careers"
    assert "empresa.fuentes" in paths


def test_dossier_invalid_carries_its_problems_and_cost():
    exc = DossierInvalid(["falta resumen"], cost_usd=Decimal("0.02"))
    assert exc.problems == ["falta resumen"]
    assert exc.cost_usd == Decimal("0.02")
    assert "falta resumen" in str(exc)
