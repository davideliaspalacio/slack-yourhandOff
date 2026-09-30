import pandas as pd
import pytest

from handoff_agent.tools import jobs


def _frame(rows):
    return pd.DataFrame(rows)


def test_buscar_ofertas_maps_the_dataframe(conn, monkeypatch):
    monkeypatch.setattr(
        jobs,
        "_scrape",
        lambda **kw: _frame(
            [
                {
                    "title": "Support Lead",
                    "company": "Acme",
                    "location": "Remote",
                    "job_url": "https://x/1",
                    "site": "linkedin",
                },
                {
                    "title": "Ops Associate",
                    "company": "Acme",
                    "location": "NYC",
                    "job_url": "https://x/2",
                    "site": "indeed",
                },
            ]
        ),
    )
    postings = jobs.buscar_ofertas("Acme")
    assert [p.title for p in postings] == ["Support Lead", "Ops Associate"]
    assert postings[0].site == "linkedin"


def test_buscar_ofertas_returns_empty_list_when_nothing_found(conn, monkeypatch):
    monkeypatch.setattr(jobs, "_scrape", lambda **kw: _frame([]))
    assert jobs.buscar_ofertas("Empresa Inexistente") == []


def test_buscar_ofertas_survives_a_scraper_failure(conn, monkeypatch):
    def boom(**kw):
        raise RuntimeError("linkedin blocked us")

    monkeypatch.setattr(jobs, "_scrape", boom)
    assert jobs.buscar_ofertas("Acme") == []


def test_buscar_ofertas_logs_the_action(conn, monkeypatch):
    monkeypatch.setattr(jobs, "_scrape", lambda **kw: _frame([]))
    jobs.buscar_ofertas("Acme")
    with conn.cursor() as cur:
        cur.execute("select action, payload from agent_actions")
        row = cur.fetchone()
    assert row[0] == "buscar_ofertas"
    assert row[1]["company"] == "Acme"


def test_buscar_ofertas_discards_other_employers(conn, monkeypatch):
    """JobSpy busca por palabra clave: una búsqueda de "Anthropic" devuelve
    ofertas de TRM Labs o ADT cuyo texto menciona la palabra. Solo el empleador
    pedido puede llegar al dossier."""
    monkeypatch.setattr(
        jobs,
        "_scrape",
        lambda **kw: _frame(
            [
                {
                    "title": "Research Engineer",
                    "company": "Anthropic",
                    "location": "SF",
                    "job_url": "https://x/1",
                    "site": "linkedin",
                },
                {
                    "title": "AI Architect",
                    "company": "ADT",
                    "location": "FL",
                    "job_url": "https://x/2",
                    "site": "indeed",
                },
                {
                    "title": "Consulting AI Director",
                    "company": "Baker Tilly Canada",
                    "location": "Toronto",
                    "job_url": "https://x/3",
                    "site": "indeed",
                },
            ]
        ),
    )
    postings = jobs.buscar_ofertas("Anthropic")
    assert [p.company for p in postings] == ["Anthropic"]


def test_buscar_ofertas_matches_employer_case_insensitively(conn, monkeypatch):
    monkeypatch.setattr(
        jobs,
        "_scrape",
        lambda **kw: _frame(
            [
                {
                    "title": "Support Lead",
                    "company": "ACME CORP",
                    "location": "Remote",
                    "job_url": "https://x/1",
                    "site": "indeed",
                },
            ]
        ),
    )
    assert len(jobs.buscar_ofertas("Acme Corp")) == 1


def test_buscar_ofertas_attributes_the_action_to_a_person(conn, monkeypatch):
    from handoff_agent.tools import prospects

    person = prospects.upsert_prospect("U_ATTR")
    monkeypatch.setattr(jobs, "_scrape", lambda **kw: _frame([]))
    jobs.buscar_ofertas("Acme", prospect_id=person["id"])
    with conn.cursor() as cur:
        cur.execute("select prospect_id from agent_actions where action = 'buscar_ofertas'")
        assert str(cur.fetchone()[0]) == str(person["id"])


def test_buscar_ofertas_asks_the_boards_for_the_right_thing(conn, monkeypatch):
    """Los dobles descartaban **kwargs, así que search_term o results_wanted
    se podían romper sin que ningún test se enterase."""
    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)
        return _frame([])

    monkeypatch.setattr(jobs, "_scrape", capture)
    jobs.buscar_ofertas("Acme Corp", limit=10)
    assert captured["search_term"] == "Acme Corp"
    assert "linkedin" in captured["site_name"]
    # Se piden de más porque el filtro por empleador descarta la mayoría.
    assert captured["results_wanted"] > 10


@pytest.mark.parametrize(
    ("row_company", "wanted", "should_match"),
    [
        ("Anthropic", "Anthropic", True),
        ("Acme, Inc.", "Acme", True),
        ("ACME CORP", "Acme Corp", True),
        ("Metabase", "Meta", False),
        ("Handoff Logistics", "Handoff", False),
        ("Meta", "Metabase", False),
        ("", "Acme", False),
    ],
)
def test_employer_matching_rejects_lookalikes(row_company, wanted, should_match):
    """El prefijo dejaba pasar Metabase para Meta, y cada oferta ajena suma un
    +2 de corroboración falso al score."""
    assert jobs._matches_company(row_company, wanted) is should_match


def test_a_missing_job_url_becomes_empty_not_nan(conn, monkeypatch):
    """pandas pone NaN en las celdas vacías; str(NaN) es "nan", que entraría
    en el dossier como si fuera una fuente."""
    monkeypatch.setattr(
        jobs,
        "_scrape",
        lambda **kw: _frame(
            [
                {
                    "title": "Support Lead",
                    "company": "Acme",
                    "location": "Remote",
                    "job_url": float("nan"),
                    "site": "linkedin",
                }
            ]
        ),
    )
    [posting] = jobs.buscar_ofertas("Acme")
    assert posting.url == ""


# --- ofertas_de_cuenta (radar de cuentas) --------------------------------------


def _row(title, company, site, url="https://x/1", location="Santiago"):
    return {
        "title": title,
        "company": company,
        "location": location,
        "job_url": url,
        "site": site,
    }


def test_with_a_linkedin_id_linkedin_is_asked_by_company_and_not_filtered(conn, monkeypatch):
    """Con el id de empresa, LinkedIn ya devuelve solo esa empresa: el nombre
    que muestra puede ser "CODELCO" o una filial y no hay que descartarlo."""
    calls = []

    def scrape(**kw):
        calls.append(kw)
        if kw["site_name"] == ["linkedin"]:
            return _frame([_row("Mining Engineer", "CODELCO - Corporación", "linkedin")])
        return _frame(
            [
                _row("Geólogo", "Codelco", "indeed"),
                _row("Chofer", "Otra Empresa", "indeed"),
            ]
        )

    monkeypatch.setattr(jobs, "_scrape", scrape)
    result = jobs.ofertas_de_cuenta("Codelco", linkedin_company_id="16300")
    linkedin_call = next(c for c in calls if c["site_name"] == ["linkedin"])
    indeed_call = next(c for c in calls if c["site_name"] == ["indeed"])
    assert linkedin_call["linkedin_company_ids"] == [16300]
    assert "search_term" not in linkedin_call
    # Sin ubicación, la búsqueda pública de LinkedIn se limita a EE. UU. y una
    # empresa chilena vuelve vacía (visto en vivo con Codelco el 2026-09-22).
    assert linkedin_call["location"] == "Worldwide"
    assert indeed_call["search_term"] == "Codelco"
    assert [(p.title, p.site) for p in result.postings] == [
        ("Mining Engineer", "linkedin"),
        ("Geólogo", "indeed"),
    ]
    assert result.errores == []


def test_without_a_linkedin_id_both_boards_use_the_employer_filter(conn, monkeypatch):
    calls = []

    def scrape(**kw):
        calls.append(kw)
        site = kw["site_name"][0]
        return _frame([_row("Ops", "Acme", site), _row("Ops", "Acme Rival", site)])

    monkeypatch.setattr(jobs, "_scrape", scrape)
    result = jobs.ofertas_de_cuenta("Acme")
    assert all(c["search_term"] == "Acme" and "linkedin_company_ids" not in c for c in calls)
    assert [p.site for p in result.postings] == ["linkedin", "indeed"]


def test_a_failing_board_is_reported_not_swallowed(conn, monkeypatch):
    def scrape(**kw):
        if kw["site_name"] == ["indeed"]:
            raise RuntimeError("indeed blocked us")
        return _frame([_row("Ops", "Acme", "linkedin")])

    monkeypatch.setattr(jobs, "_scrape", scrape)
    result = jobs.ofertas_de_cuenta("Acme")
    assert [p.site for p in result.postings] == ["linkedin"]
    assert result.errores == ["indeed: RuntimeError: indeed blocked us"]
    with conn.cursor() as cur:
        cur.execute("select result from agent_actions where action = 'ofertas_cuenta'")
        assert cur.fetchone()[0]["errores"] == ["indeed: RuntimeError: indeed blocked us"]


def test_without_a_linkedin_id_the_employer_filter_accepts_the_linkedin_long_name(
    conn, monkeypatch
):
    """LinkedIn llama "CODELCO – Corporación Nacional del Cobre de Chile" a
    Codelco: el filtro exacto la descartaba y el radar veía cero vacantes."""

    def scrape(**kw):
        site = kw["site_name"][0]
        return _frame(
            [
                _row("Geólogo", "CODELCO – Corporación Nacional del Cobre de Chile", site),
                _row("Chofer", "Codelco Tech", site),
            ]
        )

    monkeypatch.setattr(jobs, "_scrape", scrape)
    result = jobs.ofertas_de_cuenta("Codelco")
    assert [p.title for p in result.postings] == ["Geólogo", "Geólogo"]
