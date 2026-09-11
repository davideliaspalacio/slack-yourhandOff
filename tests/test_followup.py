from handoff_agent.research import followup as f
from handoff_agent.research.gather import Evidence, Gathered
from handoff_agent.tools.search import SearchResult, SearchUnavailable


def base() -> Gathered:
    return Gathered("acme.com", [Evidence("home", "https://acme.com/", "Acme", "texto")], [], [])


def test_takes_at_most_three_non_empty_queries():
    dossier = {"busquedas_sugeridas": ["uno", "", "  ", "dos", "tres", "cuatro"]}
    assert f.suggested_queries(dossier) == ["uno", "dos", "tres"]


def test_long_queries_are_truncated():
    [query] = f.suggested_queries({"busquedas_sugeridas": ["x" * 1000]})
    assert len(query) == f.MAX_QUERY_CHARS


def test_non_string_suggestions_are_ignored():
    assert f.suggested_queries({"busquedas_sugeridas": [{"q": "x"}, 3, None, "vale"]}) == ["vale"]


def test_missing_suggestions_mean_no_followup():
    assert f.suggested_queries({}) == []


def test_run_followup_adds_new_evidence_and_keeps_the_old(monkeypatch):
    monkeypatch.setattr(
        f.search,
        "buscar_web",
        lambda q, limit=5, prospect_id=None: [
            SearchResult("Acme raises $12M", "https://news.example/acme", "Series A"),
        ],
    )
    enriched = f.run_followup("pid", base(), ["acme funding"])
    urls = [e.url for e in enriched.evidence]
    assert urls == ["https://acme.com/", "https://news.example/acme"]
    assert "https://news.example/acme" in enriched.sources


def test_run_followup_degrades_on_search_outage(monkeypatch):
    def down(q, limit=5, prospect_id=None):
        raise SearchUnavailable("down")

    monkeypatch.setattr(f.search, "buscar_web", down)
    enriched = f.run_followup("pid", base(), ["acme funding"])
    assert len(enriched.evidence) == 1
    assert enriched.errors
