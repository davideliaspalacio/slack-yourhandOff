from handoff_agent import ledger
from handoff_agent.tools import prospects


def test_upsert_creates_a_prospect_once(conn):
    first = prospects.upsert_prospect("U1", full_name="Ada")
    second = prospects.upsert_prospect("U1")
    assert first["id"] == second["id"]
    with conn.cursor() as cur:
        cur.execute("select count(*) from prospects")
        assert cur.fetchone()[0] == 1


def test_upsert_does_not_overwrite_an_existing_name(conn):
    prospects.upsert_prospect("U1", full_name="Ada")
    again = prospects.upsert_prospect("U1", full_name=None)
    assert again["full_name"] == "Ada"


def test_historial_returns_none_for_an_unknown_person(conn):
    history = prospects.historial_prospecto("U-desconocido")
    assert history["prospect"] is None
    assert history["dossier"] is None
    assert history["actions"] == []


def test_save_dossier_increments_the_version(conn):
    person = prospects.upsert_prospect("U1")
    assert prospects.save_dossier(person["id"], {"summary": "v1"}, []) == 1
    assert prospects.save_dossier(person["id"], {"summary": "v2"}, []) == 2


def test_historial_returns_the_latest_dossier(conn):
    person = prospects.upsert_prospect("U1")
    prospects.save_dossier(person["id"], {"summary": "viejo"}, [])
    prospects.save_dossier(person["id"], {"summary": "nuevo"}, [])
    history = prospects.historial_prospecto("U1")
    assert history["dossier"]["content"]["summary"] == "nuevo"
    assert history["dossier"]["version"] == 2


def test_historial_includes_recent_actions(conn):
    person = prospects.upsert_prospect("U1")
    ledger.record_action("buscar_web", {"query": "acme"}, prospect_id=person["id"])
    history = prospects.historial_prospecto("U1")
    assert history["actions"][0]["action"] == "buscar_web"


def test_tool_actions_reach_the_person_history(conn, monkeypatch):
    """Sin atribución, historial_prospecto devolvería siempre acciones vacías y
    el agente no tendría contexto de lo que ya se hizo con esta persona."""
    import httpx
    import respx

    from handoff_agent.tools import search

    person = prospects.upsert_prospect("U_HIST")
    with respx.mock:
        respx.get("http://127.0.0.1:8080/search").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        search.buscar_web("acme", prospect_id=person["id"])

    history = prospects.historial_prospecto("U_HIST")
    assert [a["action"] for a in history["actions"]] == ["buscar_web"]


def test_save_dossier_survives_concurrent_writers(conn):
    """Dos disparadores pueden caer sobre la misma persona a la vez (escribió y
    además entró como miembro nuevo), más el botón de re-investigar. Sin el
    lock, el segundo moría por unique violation y se tiraba un dossier que
    costó dinero de verdad."""
    import concurrent.futures

    person = prospects.upsert_prospect("U_RACE")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(prospects.save_dossier, person["id"], {"n": i}, []) for i in range(8)
        ]
        versions = sorted(f.result() for f in futures)

    assert versions == [1, 2, 3, 4, 5, 6, 7, 8]
