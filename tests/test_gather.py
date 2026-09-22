from handoff_agent import db
from handoff_agent.research import gather as g
from handoff_agent.tools import prospects
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


def test_resolve_domain_rejects_sites_that_are_not_the_company(monkeypatch):
    monkeypatch.setattr(
        g.search,
        "buscar_web",
        fake_search(
            default=[
                SearchResult("Acme raises $20M - TechCrunch", "https://techcrunch.com/acme", ""),
                SearchResult("Some blog", "https://randomblog.io/acme-review", "Acme review"),
                SearchResult("Acme — Home", "https://acme.io/", ""),
            ]
        ),
    )
    assert g.resolve_domain("Acme", "pid") == "acme.io"


def test_resolve_domain_returns_none_when_no_result_is_the_company(monkeypatch):
    monkeypatch.setattr(
        g.search,
        "buscar_web",
        fake_search(
            default=[
                SearchResult("Top 10 CRMs", "https://randomblog.io/crm", ""),
            ]
        ),
    )
    assert g.resolve_domain("Northwind Ops", "pid") is None


def test_multi_word_companies_match_their_compact_domain(monkeypatch):
    monkeypatch.setattr(
        g.search,
        "buscar_web",
        fake_search(
            default=[
                SearchResult("Customer support software", "https://www.helpscout.com/", ""),
            ]
        ),
    )
    assert g.resolve_domain("Help Scout", "pid") == "helpscout.com"


def test_a_company_site_can_match_by_page_title(monkeypatch):
    monkeypatch.setattr(
        g.search,
        "buscar_web",
        fake_search(
            default=[
                SearchResult("Northwind Ops | Logistics software", "https://nwops.com/", ""),
            ]
        ),
    )
    assert g.resolve_domain("Northwind Ops", "pid") == "nwops.com"


def test_legal_suffixes_do_not_have_to_appear_in_the_domain(monkeypatch):
    monkeypatch.setattr(
        g.search,
        "buscar_web",
        fake_search(
            default=[
                SearchResult("Acme", "https://acme.com/", ""),
            ]
        ),
    )
    assert g.resolve_domain("Acme, Inc.", "pid") == "acme.com"


def test_company_tokens_drop_punctuation_accents_and_legal_suffixes():
    assert g.company_tokens("Café Olé, S.A.") == ["cafe", "ole"]
    assert g.company_tokens("Acme Inc") == ["acme"]


def test_a_guessed_domain_is_flagged_and_a_given_one_is_not(monkeypatch):
    monkeypatch.setattr(
        g.search,
        "buscar_web",
        fake_search(
            default=[
                SearchResult("Acme", "https://acme.com/", ""),
            ]
        ),
    )
    monkeypatch.setattr(g.web, "leer_sitio", fake_page())
    monkeypatch.setattr(g.jobs, "buscar_ofertas", lambda company, limit=20, prospect_id=None: [])
    assert g.gather("pid", "Ada", "Acme").domain_guessed is True
    assert g.gather("pid", "Ada", "Acme", domain="acme.com").domain_guessed is False


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


def test_source_records_carry_kind_and_title_once_per_url():
    gathered = g.Gathered(
        "acme.com",
        [
            g.Evidence("home", "https://acme.com/", "Acme", "t"),
            g.Evidence("prensa", "https://news.example/a", "Acme raises", "t"),
        ],
        [
            JobPosting("Support Lead", "Acme", "Remote", "https://jobs.example/1", "linkedin"),
            JobPosting("Support Lead", "Acme", "Remote", "https://jobs.example/1", "indeed"),
            JobPosting("Ops", "Acme", "Remote", "", "indeed"),
            JobPosting("Home dup", "Acme", "Remote", "https://acme.com/", "indeed"),
        ],
    )
    assert gathered.source_records() == [
        {"url": "https://acme.com/", "kind": "home", "title": "Acme"},
        {"url": "https://news.example/a", "kind": "prensa", "title": "Acme raises"},
        {"url": "https://jobs.example/1", "kind": "vacante", "title": "Support Lead"},
    ]
    assert {r["url"] for r in gathered.source_records()} == gathered.sources


def test_short_name_rejects_false_positive_in_other_domains(monkeypatch):
    """Short names like 'On' must not match as substring in unrelated hosts."""
    monkeypatch.setattr(
        g.search,
        "buscar_web",
        fake_search(
            default=[
                SearchResult("Amazon services", "https://www.amazon.com/", ""),
            ]
        ),
    )
    assert g.resolve_domain("On", "pid") is None


def test_short_name_accepts_exact_domain_label(monkeypatch):
    """Short name 'On' matches its own domain on.com as an exact label."""
    monkeypatch.setattr(
        g.search,
        "buscar_web",
        fake_search(
            default=[
                SearchResult("On - Home", "https://on.com/", ""),
            ]
        ),
    )
    assert g.resolve_domain("On", "pid") == "on.com"


def test_short_name_ally_rejects_really_domain(monkeypatch):
    """'Ally' must not match as substring in 'really.com'."""
    monkeypatch.setattr(
        g.search,
        "buscar_web",
        fake_search(
            default=[
                SearchResult("Really company", "https://really.com/", ""),
            ]
        ),
    )
    assert g.resolve_domain("Ally", "pid") is None


def test_short_name_ally_accepts_its_own_domain(monkeypatch):
    """'Ally' matches www.ally.com as an exact label."""
    monkeypatch.setattr(
        g.search,
        "buscar_web",
        fake_search(
            default=[
                SearchResult("Ally - Financial", "https://www.ally.com/", ""),
            ]
        ),
    )
    assert g.resolve_domain("Ally", "pid") == "ally.com"


def test_four_letter_name_rejects_getacme_domain(monkeypatch):
    """'Acme' (4 letters) must not match getacme.com even as a prefix."""
    monkeypatch.setattr(
        g.search,
        "buscar_web",
        fake_search(
            default=[
                SearchResult("Some service", "https://getacme.com/", ""),
            ]
        ),
    )
    assert g.resolve_domain("Acme", "pid") is None


def test_five_letter_name_accepts_getacme_domain(monkeypatch):
    """'Toggl' (5 letters) matches gettoggl.com as a suffix."""
    monkeypatch.setattr(
        g.search,
        "buscar_web",
        fake_search(
            default=[
                SearchResult("Time tracking", "https://gettoggl.com/", ""),
            ]
        ),
    )
    assert g.resolve_domain("Toggl", "pid") == "gettoggl.com"


def test_five_letter_name_accepts_prefix_domain(monkeypatch):
    """'Northwind' (9 letters) matches northwindhq.com as a prefix."""
    monkeypatch.setattr(
        g.search,
        "buscar_web",
        fake_search(
            default=[
                SearchResult("Enterprise software", "https://northwindhq.com/", ""),
            ]
        ),
    )
    assert g.resolve_domain("Northwind", "pid") == "northwindhq.com"


def test_gather_calls_enrichment_when_the_domain_is_known(monkeypatch):
    calls = []

    def fake_enrich(domain, prospect_id=None):
        calls.append((domain, prospect_id))
        return {"empleados_linkedin": 100}

    monkeypatch.setattr(g.search, "buscar_web", fake_search())
    monkeypatch.setattr(g.web, "leer_sitio", fake_page())
    monkeypatch.setattr(g.jobs, "buscar_ofertas", no_jobs)
    monkeypatch.setattr(g.enrichment, "enrich_company", fake_enrich)
    result = g.gather("pid", "Ada Ruiz", "Acme", domain="acme.com")
    assert calls == [("acme.com", "pid")]
    assert result.proveedor == {"empleados_linkedin": 100}


def test_gather_skips_enrichment_without_a_domain(monkeypatch):
    calls = []

    monkeypatch.setattr(g.search, "buscar_web", fake_search())
    monkeypatch.setattr(g.web, "leer_sitio", fake_page())
    monkeypatch.setattr(g.jobs, "buscar_ofertas", no_jobs)
    monkeypatch.setattr(
        g.enrichment, "enrich_company", lambda domain, prospect_id=None: calls.append(domain)
    )
    result = g.gather("pid", "Ada Ruiz", None)
    assert calls == []
    assert result.proveedor is None


# --- Feature A: un dominio adivinado se confirma antes de usarse -----------


def test_a_guessed_domain_confirmed_by_page_text_runs_no_extra_search(monkeypatch):
    calls = []

    def search(query, limit=8, prospect_id=None):
        calls.append(query)
        return [SearchResult("Acme — Home", "https://acme.com/", "")]

    monkeypatch.setattr(g.search, "buscar_web", search)
    monkeypatch.setattr(g.jobs, "buscar_ofertas", lambda company, limit=20, prospect_id=None: [])

    def leer(url, max_chars=20_000, prospect_id=None):
        return PageContent(
            url=url, final_url=url, title="Acme", text="Ada Ruiz is the CEO of Acme."
        )

    monkeypatch.setattr(g.web, "leer_sitio", leer)
    result = g.gather("pid", "Ada Ruiz", "Acme")
    assert result.domain == "acme.com"
    assert result.unconfirmed_domain is None
    # La página ya trae el nombre completo, así que no hace falta la búsqueda
    # de refuerzo ('"Ada Ruiz" site:acme.com'): solo corren las búsquedas que
    # gather() ya hacía (dominio, linkedin, prensa).
    assert '"Ada Ruiz" site:acme.com' not in calls
    assert result.searches_attempted == 3


def test_a_guessed_domain_confirmed_by_a_site_search(monkeypatch):
    def search(query, limit=8, prospect_id=None):
        if query == '"Ada Ruiz" site:acme.com':
            return [SearchResult("Ada Ruiz - Acme", "https://acme.com/team/ada", "")]
        return [SearchResult("Acme — Home", "https://acme.com/", "")]

    monkeypatch.setattr(g.search, "buscar_web", search)

    def leer(url, max_chars=20_000, prospect_id=None):
        return PageContent(url=url, final_url=url, title="Acme", text="No name mentioned here.")

    monkeypatch.setattr(g.web, "leer_sitio", leer)
    monkeypatch.setattr(g.jobs, "buscar_ofertas", lambda company, limit=20, prospect_id=None: [])
    result = g.gather("pid", "Ada Ruiz", "Acme")
    assert result.domain == "acme.com"
    assert result.unconfirmed_domain is None
    kinds = {e.kind for e in result.evidence}
    assert {"home", "about", "careers"} <= kinds


def test_an_unconfirmed_guessed_domain_is_dropped(monkeypatch):
    def search(query, limit=8, prospect_id=None):
        # Solo la búsqueda del dominio trae algo; la de confirmación (y
        # linkedin/prensa, irrelevantes aquí) no traen nada.
        if query == "Handoff official website":
            return [SearchResult("Handoff — Home", "https://handoff.ai/", "")]
        return []

    monkeypatch.setattr(g.search, "buscar_web", search)

    def leer(url, max_chars=20_000, prospect_id=None):
        return PageContent(
            url=url, final_url=url, title="Handoff", text="We build AI agents for enterprises."
        )

    monkeypatch.setattr(g.web, "leer_sitio", leer)
    # Sin conexión real a la base en este test (se prueba aparte, en
    # test_an_unconfirmed_domain_is_recorded_on_the_ledger).
    monkeypatch.setattr(g.ledger, "record_action", lambda *a, **kw: None)

    enrich_calls = []
    monkeypatch.setattr(
        g.enrichment,
        "enrich_company",
        lambda domain, prospect_id=None: enrich_calls.append(domain) or {"empleados_linkedin": 1},
    )
    monkeypatch.setattr(g.jobs, "buscar_ofertas", lambda company, limit=20, prospect_id=None: [])

    result = g.gather("pid", "David Handoff", "Handoff")
    assert result.domain is None
    assert result.unconfirmed_domain == "handoff.ai"
    assert result.evidence == []
    assert enrich_calls == []


def test_an_unconfirmed_domain_is_recorded_on_the_ledger(conn, monkeypatch):
    def search(query, limit=8, prospect_id=None):
        if "site:handoff.ai" in query:
            return []
        return [SearchResult("Handoff — Home", "https://handoff.ai/", "")]

    monkeypatch.setattr(g.search, "buscar_web", search)
    monkeypatch.setattr(
        g.web,
        "leer_sitio",
        # Ni el nombre ni el apellido aparecen: la comprobación gratis no
        # puede confirmar nada, así que cae a la búsqueda de refuerzo.
        lambda url, max_chars=20_000, prospect_id=None: PageContent(
            url=url, final_url=url, title="Welcome", text="We are a company. Nothing else here."
        ),
    )
    monkeypatch.setattr(g.jobs, "buscar_ofertas", lambda company, limit=20, prospect_id=None: [])

    pid = str(prospects.upsert_prospect("U_CONFIRM")["id"])
    g.gather(pid, "David Handoff", "Handoff")
    action = db.fetch_one(
        "select payload from agent_actions where action = 'empresa_no_confirmada' "
        "and prospect_id = %s",
        (pid,),
    )
    assert action["payload"] == {"dominio_adivinado": "handoff.ai"}


def test_a_guessed_domain_with_no_full_name_cannot_be_confirmed(monkeypatch):
    monkeypatch.setattr(
        g.search,
        "buscar_web",
        fake_search(default=[SearchResult("Handoff — Home", "https://handoff.ai/", "")]),
    )
    monkeypatch.setattr(
        g.web,
        "leer_sitio",
        lambda url, max_chars=20_000, prospect_id=None: PageContent(
            url=url, final_url=url, title="Handoff", text="anything"
        ),
    )
    monkeypatch.setattr(g.jobs, "buscar_ofertas", lambda company, limit=20, prospect_id=None: [])
    monkeypatch.setattr(g.ledger, "record_action", lambda *a, **kw: None)
    result = g.gather("pid", None, "Handoff")
    assert result.domain is None
    assert result.unconfirmed_domain == "handoff.ai"


def test_a_given_domain_email_or_override_is_never_verified(monkeypatch):
    """domain pasado a mano (email/override en el llamante): no se guessed, así
    que no se intenta ninguna comprobación ni búsqueda de confirmación."""
    calls = []

    def search(query, limit=8, prospect_id=None):
        calls.append(query)
        return []

    monkeypatch.setattr(g.search, "buscar_web", search)
    monkeypatch.setattr(
        g.web,
        "leer_sitio",
        lambda url, max_chars=20_000, prospect_id=None: PageContent(
            url=url, final_url=url, title="Acme", text="no name here at all"
        ),
    )
    monkeypatch.setattr(g.jobs, "buscar_ofertas", lambda company, limit=20, prospect_id=None: [])
    result = g.gather("pid", "Ada Ruiz", "Acme", domain="acme.com")
    assert result.domain == "acme.com"
    assert result.unconfirmed_domain is None
    # linkedin y prensa, nada de dominio ni confirmación.
    assert calls == [
        'site:linkedin.com/in "Ada Ruiz" "Acme"',
        '"Acme" funding OR raises OR hiring',
    ]


def test_confirmation_search_outage_leaves_the_domain_unconfirmed(monkeypatch):
    def search(query, limit=8, prospect_id=None):
        if query == "Handoff official website":
            return [SearchResult("Handoff — Home", "https://handoff.ai/", "")]
        raise SearchUnavailable("motores vetados")

    monkeypatch.setattr(g.search, "buscar_web", search)
    monkeypatch.setattr(
        g.web,
        "leer_sitio",
        lambda url, max_chars=20_000, prospect_id=None: PageContent(
            url=url, final_url=url, title="Handoff", text="nothing relevant"
        ),
    )
    monkeypatch.setattr(g.jobs, "buscar_ofertas", lambda company, limit=20, prospect_id=None: [])
    monkeypatch.setattr(g.ledger, "record_action", lambda *a, **kw: None)
    result = g.gather("pid", "David Handoff", "Handoff")
    assert result.domain is None
    assert result.unconfirmed_domain == "handoff.ai"
    assert any("confirmar_dominio" in err for err in result.errors)


def test_name_confirms_page_requires_two_tokens():
    assert g._name_confirms_page("Ada", "Ada is here, Ada works at Acme") is False


def test_name_confirms_page_matches_first_and_last_near_each_other():
    assert g._name_confirms_page("Ada Ruiz", "Meet Ada Ruiz, our CEO") is True
    assert g._name_confirms_page("Ada Ruiz", "Ada, CEO — no last name mentioned") is False
    far_apart = "Ruiz " + "filler " * 10 + "and here comes Ada"
    assert g._name_confirms_page("Ada Ruiz", far_apart) is False


def test_tld_name_does_not_match_through_tld_label(monkeypatch):
    """A name equal to a TLD must not match through the TLD: 'IO' vs example.io."""
    monkeypatch.setattr(
        g.search,
        "buscar_web",
        fake_search(
            default=[
                SearchResult("Some company", "https://example.io/", ""),
            ]
        ),
    )
    assert g.resolve_domain("IO", "pid") is None
