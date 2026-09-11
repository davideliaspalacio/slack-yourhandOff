from handoff_agent.research import gather as g
from handoff_agent.tools.jobs import JobPosting
from handoff_agent.tools.search import SearchResult, SearchUnavailable
from handoff_agent.tools.web import PageContent, PageUnavailable


def fake_search(results_by_query=None, default=()):
    calls = []

    def search(query, limit=8, prospect_id=None):
        calls.append(query)
        for key, results in (results_by_query or {}).items():
            if key in query:
                return list(results)
        return list(default)

    search.calls = calls
    return search


def fake_page(ok_paths=("/", "/careers")):
    calls = []

    def leer(url, max_chars=20_000, prospect_id=None):
        calls.append(url)
        path = "/" + url.split("/", 3)[3] if url.count("/") >= 3 else "/"
        if path not in ok_paths:
            raise PageUnavailable(f"404 at {url}")
        return PageContent(url=url, final_url=url, title=f"title {path}", text=f"text {path}")

    leer.calls = calls
    return leer


def test_resolve_domain_skips_directories_and_social_sites(monkeypatch):
    monkeypatch.setattr(
        g.search,
        "buscar_web",
        fake_search(
            default=[
                SearchResult("Acme | LinkedIn", "https://www.linkedin.com/company/acme", ""),
                SearchResult(
                    "Acme - Crunchbase", "https://www.crunchbase.com/organization/acme", ""
                ),
                SearchResult("Acme — Home", "https://www.acme.com/", ""),
            ]
        ),
    )
    assert g.resolve_domain("Acme", "pid") == "acme.com"


def test_gather_reads_site_pages_and_records_failures(monkeypatch):
    monkeypatch.setattr(g.search, "buscar_web", fake_search())
    monkeypatch.setattr(g.web, "leer_sitio", fake_page(ok_paths=("/", "/careers")))
    monkeypatch.setattr(g.jobs, "buscar_ofertas", lambda company, limit=20, prospect_id=None: [])
    result = g.gather("pid", "Ada Ruiz", "Acme", domain="acme.com")
    kinds = {e.kind for e in result.evidence}
    assert {"home", "careers"} <= kinds
    assert any(err.startswith("about") for err in result.errors)


def test_linkedin_is_only_ever_searched_never_fetched(monkeypatch):
    page = fake_page()
    monkeypatch.setattr(
        g.search,
        "buscar_web",
        fake_search(
            {
                "linkedin.com/in": [
                    SearchResult(
                        "Ada Ruiz - CEO - Acme",
                        "https://www.linkedin.com/in/adaruiz",
                        "CEO at Acme",
                    )
                ],
            }
        ),
    )
    monkeypatch.setattr(g.web, "leer_sitio", page)
    monkeypatch.setattr(g.jobs, "buscar_ofertas", lambda company, limit=20, prospect_id=None: [])
    result = g.gather("pid", "Ada Ruiz", "Acme", domain="acme.com")
    assert any(e.kind == "linkedin" and e.text == "CEO at Acme" for e in result.evidence)
    assert not any("linkedin.com" in url for url in page.calls)


def test_sources_include_job_urls(monkeypatch):
    monkeypatch.setattr(g.search, "buscar_web", fake_search())
    monkeypatch.setattr(g.web, "leer_sitio", fake_page())
    monkeypatch.setattr(
        g.jobs,
        "buscar_ofertas",
        lambda company, limit=20, prospect_id=None: [
            JobPosting("Support Lead", "Acme", "Remote", "https://jobs.example/1", "linkedin"),
        ],
    )
    result = g.gather("pid", "Ada Ruiz", "Acme", domain="acme.com")
    assert "https://jobs.example/1" in result.sources
    assert "https://acme.com/" in result.sources


def test_search_outage_degrades_instead_of_crashing(monkeypatch):
    def down(query, limit=8, prospect_id=None):
        raise SearchUnavailable("SearXNG did not answer")

    monkeypatch.setattr(g.search, "buscar_web", down)
    monkeypatch.setattr(g.web, "leer_sitio", fake_page())
    monkeypatch.setattr(g.jobs, "buscar_ofertas", lambda company, limit=20, prospect_id=None: [])
    result = g.gather("pid", "Ada Ruiz", "Acme")
    assert result.domain is None
    assert result.errors


def test_no_company_means_no_job_search(monkeypatch):
    asked = []
    monkeypatch.setattr(g.search, "buscar_web", fake_search())
    monkeypatch.setattr(g.web, "leer_sitio", fake_page())
    monkeypatch.setattr(
        g.jobs,
        "buscar_ofertas",
        lambda company, limit=20, prospect_id=None: asked.append(company) or [],
    )
    g.gather("pid", "Ada Ruiz", None)
    assert asked == []


def test_dedupe_keeps_the_first_occurrence_of_each_url():
    a = g.Evidence("home", "https://acme.com/", "t", "primero")
    b = g.Evidence("about", "https://acme.com/", "t", "redirigió a home")
    assert g.dedupe_evidence([a, b]) == [a]


def no_jobs(company, limit=20, prospect_id=None):
    return []


def test_every_search_failing_marks_the_gathering_degraded(monkeypatch):
    def down(query, limit=8, prospect_id=None):
        raise SearchUnavailable("motores vetados")

    monkeypatch.setattr(g.search, "buscar_web", down)
    monkeypatch.setattr(g.web, "leer_sitio", fake_page())
    monkeypatch.setattr(g.jobs, "buscar_ofertas", no_jobs)
    result = g.gather("pid", "Ada Ruiz", "Acme")
    assert result.searches_attempted == 3  # dominio, linkedin, prensa
    assert result.searches_answered == 0
    assert result.search_degraded


def test_searches_with_zero_results_also_count_as_degraded(monkeypatch):
    monkeypatch.setattr(g.search, "buscar_web", fake_search())
    monkeypatch.setattr(g.web, "leer_sitio", fake_page())
    monkeypatch.setattr(g.jobs, "buscar_ofertas", no_jobs)
    result = g.gather("pid", "Ada Ruiz", "Acme", domain="acme.com")
    assert result.searches_attempted == 2  # linkedin y prensa; el dominio ya venía
    assert result.search_degraded


def test_one_search_with_results_is_enough(monkeypatch):
    monkeypatch.setattr(
        g.search,
        "buscar_web",
        fake_search({"funding": [SearchResult("Acme raises", "https://news.example/a", "x")]}),
    )
    monkeypatch.setattr(g.web, "leer_sitio", fake_page())
    monkeypatch.setattr(g.jobs, "buscar_ofertas", no_jobs)
    result = g.gather("pid", "Ada Ruiz", "Acme")
    assert result.searches_attempted == 3
    assert result.searches_answered == 1
    assert not result.search_degraded


def test_no_search_attempted_is_not_degraded():
    assert not g.Gathered("acme.com", [], [], []).search_degraded
