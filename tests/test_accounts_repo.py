import pytest

from handoff_agent import db
from handoff_agent.accounts import repo


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("acme.com", "acme.com"),
        ("HTTPS://www.Acme.com/", "acme.com"),
        ("http://acme.com/careers?x=1", "acme.com"),
        ("  codelco.cl  ", "codelco.cl"),
        ("", None),
        (None, None),
    ],
)
def test_normalizar_dominio(raw, expected):
    assert repo.normalizar_dominio(raw) == expected


@pytest.mark.parametrize("raw", ["not a domain", "acme", "-acme.com"])
def test_an_invalid_domain_is_rejected(raw):
    with pytest.raises(ValueError):
        repo.normalizar_dominio(raw)


def test_agregar_cuenta_normalises_and_defaults(conn):
    cuenta = repo.agregar_cuenta("  Acme  ", "https://www.acme.com/", "123")
    assert (cuenta["name"], cuenta["domain"], cuenta["linkedin_company_id"]) == (
        "Acme",
        "acme.com",
        "123",
    )
    assert (cuenta["source"], cuenta["status"]) == ("manual", "watching")


def test_the_same_domain_is_an_update_that_keeps_the_resolved_id(conn):
    first = repo.agregar_cuenta("Acme", "acme.com", "123")
    second = repo.agregar_cuenta("Acme Corp", "acme.com", source="csv")
    assert first["id"] == second["id"]
    assert (second["name"], second["linkedin_company_id"]) == ("Acme Corp", "123")
    # Una cuenta dada de alta a mano no pasa a ser "csv" por reimportarla.
    assert second["source"] == "manual"


def test_without_a_domain_the_name_dedupes(conn):
    first = repo.agregar_cuenta("Codelco")
    assert repo.agregar_cuenta("CODELCO")["id"] == first["id"]
    assert db.fetch_one("select count(*) as n from target_accounts")["n"] == 1


@pytest.mark.parametrize(
    ("nombre", "linkedin_id"), [("", None), ("   ", None), ("Acme", "12a"), ("x" * 201, None)]
)
def test_bad_input_is_rejected(conn, nombre, linkedin_id):
    with pytest.raises(ValueError):
        repo.agregar_cuenta(nombre, "acme.com", linkedin_id)


def test_importar_csv_adds_good_rows_and_reports_bad_ones(conn, tmp_path):
    path = tmp_path / "cuentas.csv"
    path.write_text(
        "nombre,dominio,linkedin_id\nCodelco,codelco.cl,16300\nAcme,acme.com,\n,sin-nombre.com,\n"
        "Rota,no es un dominio,\n",
        encoding="utf-8",
    )
    result = repo.importar_csv(path)
    assert result.agregadas == 2
    assert len(result.errores) == 2
    assert "línea 4" in result.errores[0]
    rows = db.fetch_all("select name, source, linkedin_company_id from target_accounts order by 1")
    assert [(r["name"], r["source"], r["linkedin_company_id"]) for r in rows] == [
        ("Acme", "csv", None),
        ("Codelco", "csv", "16300"),
    ]


def test_importar_csv_without_the_linkedin_column(conn, tmp_path):
    path = tmp_path / "cuentas.csv"
    path.write_text("nombre,dominio\nAcme,acme.com\n", encoding="utf-8")
    assert repo.importar_csv(path).agregadas == 1


def test_importar_csv_requires_the_nombre_column(conn, tmp_path):
    path = tmp_path / "cuentas.csv"
    path.write_text("empresa,web\nAcme,acme.com\n", encoding="utf-8")
    with pytest.raises(ValueError, match="nombre"):
        repo.importar_csv(path)


def add_signal(account_id, key, score=3, closed=False):
    db.execute(
        "insert into hiring_signals (account_id, title, title_key, score, closed_at) "
        "values (%s, %s, %s, %s, case when %s then now() else null end)",
        (account_id, key.title(), key, score, closed),
    )


def test_listar_cuentas_counts_open_roles_and_the_top_score(conn):
    acme = repo.agregar_cuenta("Acme", "acme.com")
    repo.agregar_cuenta("Beta", "beta.com")
    add_signal(acme["id"], "ops lead", score=5)
    add_signal(acme["id"], "support", score=3)
    add_signal(acme["id"], "old role", score=9, closed=True)
    rows = {r["name"]: r for r in repo.listar_cuentas()}
    assert (rows["Acme"]["open_roles"], rows["Acme"]["top_score"]) == (2, 5)
    assert (rows["Beta"]["open_roles"], rows["Beta"]["top_score"]) == (0, None)


def test_buscar_cuenta_by_domain_or_name(conn):
    acme = repo.agregar_cuenta("Acme Mining", "acme.com")
    assert repo.buscar_cuenta("https://www.acme.com")["id"] == acme["id"]
    assert repo.buscar_cuenta("acme mining")["id"] == acme["id"]
    assert repo.buscar_cuenta("nadie") is None
    assert repo.buscar_cuenta("  ") is None


def test_senales_de_cuenta_lists_open_ones_by_score(conn):
    acme = repo.agregar_cuenta("Acme", "acme.com")
    add_signal(acme["id"], "a", score=3)
    add_signal(acme["id"], "b", score=8)
    add_signal(acme["id"], "c", score=9, closed=True)
    assert [s["title_key"] for s in repo.senales_de_cuenta(acme["id"])] == ["b", "a"]
    assert len(repo.senales_de_cuenta(acme["id"], incluir_cerradas=True)) == 3


def test_listar_senales_rejects_an_unknown_status(conn):
    with pytest.raises(ValueError):
        repo.listar_senales(estado="whatever")
