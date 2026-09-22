import httpx
import pytest
import respx

from handoff_agent.tools import web

HTML = """
<html><head><title>Acme — Careers</title></head>
<body><article>
<h1>Join Acme</h1>
<p>We are hiring a Support Lead and two Ops Associates for our growing team.</p>
</article></body></html>
"""


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Los tests no dependen de DNS real: todo host se considera público salvo
    que el propio test diga lo contrario."""
    monkeypatch.setattr(web, "_resolve", lambda host: ["93.184.216.34"])


@respx.mock
def test_leer_sitio_extracts_readable_text(conn):
    respx.get("https://acme.com/careers").mock(return_value=httpx.Response(200, html=HTML))
    page = web.leer_sitio("https://acme.com/careers")
    assert "Support Lead" in page.text
    assert "<p>" not in page.text
    assert page.url == "https://acme.com/careers"
    assert page.final_url == "https://acme.com/careers"


@respx.mock
def test_leer_sitio_truncates_to_max_chars(conn):
    long_html = "<html><body><article><p>" + ("palabra " * 5000) + "</p></article></body></html>"
    respx.get("https://acme.com/long").mock(return_value=httpx.Response(200, html=long_html))
    page = web.leer_sitio("https://acme.com/long", max_chars=100)
    assert len(page.text) <= 100


@respx.mock
def test_leer_sitio_raises_on_http_error(conn):
    respx.get("https://acme.com/404").mock(return_value=httpx.Response(404))
    with pytest.raises(web.PageUnavailable):
        web.leer_sitio("https://acme.com/404")


@respx.mock
def test_leer_sitio_logs_the_action(conn):
    respx.get("https://acme.com/").mock(return_value=httpx.Response(200, html=HTML))
    web.leer_sitio("https://acme.com/")
    with conn.cursor() as cur:
        cur.execute("select action, payload from agent_actions")
        row = cur.fetchone()
    assert row[0] == "leer_sitio"
    assert row[1]["url"] == "https://acme.com/"


# --- SSRF y límites de recurso ---


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # loopback: Supabase, Studio y SearXNG viven aquí
        "169.254.169.254",  # metadatos de instancia en Railway y demás nubes
        "10.0.0.5",  # red privada
        "192.168.1.10",  # red privada
        "::1",  # loopback IPv6
        "fd00::1",  # unique local IPv6 (equivalente a la RFC1918 en IPv6)
    ],
)
def test_leer_sitio_refuses_non_public_addresses(conn, monkeypatch, address):
    monkeypatch.setattr(web, "_resolve", lambda host: [address])
    with pytest.raises(web.PageUnavailable, match="non-public"):
        web.leer_sitio("https://interno.ejemplo/")


@respx.mock
def test_leer_sitio_allows_a_public_address(conn, monkeypatch):
    """Task 2: los enlaces del equipo son texto libre que llega hasta aquí --
    esta comprobación es la que evita que un enlace apuntando a la red interna
    se cuele como research_links, y no debe rechazar una IP pública normal."""
    monkeypatch.setattr(web, "_resolve", lambda host: ["93.184.216.34"])
    respx.get("https://acme.com/").mock(return_value=httpx.Response(200, html=HTML))
    page = web.leer_sitio("https://acme.com/")
    assert page.final_url == "https://acme.com/"


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://acme.com/",
        "ftp://acme.com/x",
    ],
)
def test_leer_sitio_refuses_non_http_schemes(conn, url):
    with pytest.raises(web.PageUnavailable, match="refusing scheme"):
        web.leer_sitio(url)


@respx.mock
def test_leer_sitio_checks_every_redirect_hop(conn, monkeypatch):
    """Vetar solo la URL pedida deja el destino de la redirección sin comprobar,
    que es justo por donde se llega a los metadatos de la instancia."""

    def resolve(host):
        return ["169.254.169.254"] if host == "metadata.interno" else ["93.184.216.34"]

    monkeypatch.setattr(web, "_resolve", resolve)
    respx.get("https://acme.com/redirige").mock(
        return_value=httpx.Response(302, headers={"location": "http://metadata.interno/creds"})
    )
    with pytest.raises(web.PageUnavailable, match="non-public"):
        web.leer_sitio("https://acme.com/redirige")


@respx.mock
def test_leer_sitio_follows_a_legitimate_redirect_and_reports_where_it_landed(conn):
    respx.get("https://acme.com/careers").mock(
        return_value=httpx.Response(301, headers={"location": "https://jobs.acme.com/"})
    )
    respx.get("https://jobs.acme.com/").mock(return_value=httpx.Response(200, html=HTML))
    page = web.leer_sitio("https://acme.com/careers")
    assert page.url == "https://acme.com/careers"
    assert page.final_url == "https://jobs.acme.com/"


@respx.mock
def test_leer_sitio_stops_a_redirect_loop(conn):
    respx.get("https://acme.com/bucle").mock(
        return_value=httpx.Response(302, headers={"location": "https://acme.com/bucle"})
    )
    with pytest.raises(web.PageUnavailable, match="too many redirects"):
        web.leer_sitio("https://acme.com/bucle")


@respx.mock
def test_leer_sitio_refuses_an_oversized_response(conn, monkeypatch):
    """Sin tope, un binario grande o un endpoint que gotea bytes agota la
    memoria del worker o lo deja clavado."""
    monkeypatch.setattr(web, "MAX_RESPONSE_BYTES", 1000)
    respx.get("https://acme.com/enorme").mock(
        return_value=httpx.Response(200, content=b"x" * 50_000)
    )
    with pytest.raises(web.PageUnavailable, match="exceeded"):
        web.leer_sitio("https://acme.com/enorme")


@respx.mock
def test_leer_sitio_records_the_final_url_for_the_dossier(conn):
    """Guardar la URL pedida cuando hubo redirección atribuye el texto a un
    dominio que no lo escribió."""
    respx.get("https://acme.com/x").mock(
        return_value=httpx.Response(302, headers={"location": "https://otro.com/y"})
    )
    respx.get("https://otro.com/y").mock(return_value=httpx.Response(200, html=HTML))
    web.leer_sitio("https://acme.com/x")
    with conn.cursor() as cur:
        cur.execute("select payload from agent_actions where action = 'leer_sitio'")
        payload = cur.fetchone()[0]
    assert payload["url"] == "https://acme.com/x"
    assert payload["final_url"] == "https://otro.com/y"
