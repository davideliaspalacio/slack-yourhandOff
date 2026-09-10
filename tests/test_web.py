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


@respx.mock
def test_leer_sitio_extracts_readable_text(conn):
    respx.get("https://acme.com/careers").mock(return_value=httpx.Response(200, html=HTML))
    page = web.leer_sitio("https://acme.com/careers")
    assert "Support Lead" in page.text
    assert "<p>" not in page.text
    assert page.url == "https://acme.com/careers"


@respx.mock
def test_leer_sitio_truncates_to_max_chars(conn):
    long_html = (
        "<html><body><article><p>" + ("palabra " * 5000) + "</p></article></body></html>"
    )
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
