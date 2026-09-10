import pandas as pd

from handoff_agent.tools import jobs


def _frame(rows):
    return pd.DataFrame(rows)


def test_buscar_ofertas_maps_the_dataframe(conn, monkeypatch):
    monkeypatch.setattr(jobs, "_scrape", lambda **kw: _frame([
        {"title": "Support Lead", "company": "Acme", "location": "Remote",
         "job_url": "https://x/1", "site": "linkedin"},
        {"title": "Ops Associate", "company": "Acme", "location": "NYC",
         "job_url": "https://x/2", "site": "indeed"},
    ]))
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
