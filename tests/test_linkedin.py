"""El enlace de LinkedIn de una persona: qué cuenta como perfil y cómo se monta la
búsqueda. Todo lo que entra viene de un tercero o de un modelo, así que lo que más
importa aquí es lo que NO se enlaza."""

from urllib.parse import parse_qs, urlparse

import pytest

from handoff_agent.delivery import linkedin

PERFIL = "https://www.linkedin.com/in/ada-ruiz"


@pytest.mark.parametrize(
    "url",
    [
        PERFIL,
        "https://linkedin.com/in/ada-ruiz/",
        "https://es.linkedin.com/in/ada-ruiz-123abc",
    ],
)
def test_a_person_profile_is_a_profile(url):
    assert linkedin.es_perfil(url)


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        42,
        "http://www.linkedin.com/in/ada-ruiz",  # sin https
        "https://www.linkedin.com/company/acme",  # una empresa no es una persona
        "https://www.linkedin.com/search/results/people/?keywords=ada",
        "https://www.linkedin.com/in/",  # sin alias
        "https://linkedin.com.evil.co/in/ada-ruiz",  # el dominio solo lo parece
        "https://evil.co/in/ada-ruiz?x=linkedin.com",
        "https://notlinkedin.com/in/ada-ruiz",
        "https://user:pass@www.linkedin.com/in/ada-ruiz",
        "https://www.linkedin.com/in/ada|<!channel>",  # rompería `<url|texto>`
        "https://www.linkedin.com/in/ada ruiz",
        "https://www.linkedin.com/in/" + "a" * 400,
    ],
)
def test_anything_else_is_not_a_profile(url):
    assert not linkedin.es_perfil(url)


def test_the_profile_comes_from_the_source_the_dossier_cites():
    assert linkedin.perfil({"persona": {"cargo": "CEO", "fuente": PERFIL}}) == PERFIL


def test_a_source_that_is_not_linkedin_is_not_the_profile():
    assert linkedin.perfil({"persona": {"cargo": "CEO", "fuente": "https://acme.com/team"}}) is None


@pytest.mark.parametrize("dossier", [None, {}, {"persona": None}, {"persona": "x"}, "x", []])
def test_a_missing_or_broken_dossier_has_no_profile(dossier):
    assert linkedin.perfil(dossier) is None


def test_a_link_the_team_added_by_hand_wins():
    """Alguien de Handoff lo eligió a propósito: gana a lo que citó el modelo."""
    a_mano = "https://www.linkedin.com/in/ada-ruiz-la-buena"
    dossier = {"persona": {"fuente": PERFIL}}
    assert linkedin.perfil(dossier, ["https://acme.com", a_mano]) == a_mano


def test_the_search_carries_the_name_and_the_company():
    url = linkedin.busqueda("Ada  Ruiz", "Acme Inc")
    partes = urlparse(url)
    assert (partes.scheme, partes.netloc) == ("https", "www.linkedin.com")
    assert partes.path == "/search/results/people/"
    assert parse_qs(partes.query) == {"keywords": ["Ada Ruiz Acme Inc"]}


def test_the_search_works_with_the_name_alone():
    assert parse_qs(urlparse(linkedin.busqueda("Ada Ruiz")).query) == {"keywords": ["Ada Ruiz"]}


@pytest.mark.parametrize("nombre", [None, "", "   "])
def test_without_a_name_there_is_no_search(nombre):
    """Buscar solo por empresa no encuentra a la persona."""
    assert linkedin.busqueda(nombre, "Acme") is None


def test_a_hostile_name_cannot_leave_the_query():
    """El nombre sale de un perfil ajeno: va codificado, y no puede ni cambiar de sitio
    el enlace ni cerrar el `<url|texto>` de Slack."""
    url = linkedin.busqueda("Ada|<!channel>&next=https://evil.co #x", "A>B")
    assert not any(ch in url for ch in "|<> ")
    assert url.startswith(linkedin.BUSQUEDA)
    assert list(parse_qs(urlparse(url).query)) == ["keywords"]
    assert urlparse(url).fragment == ""


def test_a_very_long_name_does_not_make_an_endless_link():
    assert len(linkedin.busqueda("a" * 5000)) < 200


def test_the_reminder_links_the_profile_when_it_is_known():
    linea = linkedin.linea({"persona": {"fuente": PERFIL}}, "Ada Ruiz", "Acme")
    assert f"<{PERFIL}|Open their profile>" in linea
    assert "Connect on LinkedIn" in linea


def test_the_reminder_falls_back_to_a_search_and_says_so():
    """Una búsqueda no es un perfil comprobado, y el aviso tiene que decirlo."""
    linea = linkedin.linea({}, "Ada Ruiz", "Acme")
    assert "search/results/people/?keywords=Ada+Ruiz+Acme" in linea
    assert "Find them on LinkedIn" in linea
    assert "not a confirmed profile" in linea


def test_without_a_profile_or_a_name_there_is_no_reminder():
    assert linkedin.linea({}, None, "Acme") is None
