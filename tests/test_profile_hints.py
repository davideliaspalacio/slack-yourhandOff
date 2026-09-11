import pytest

from handoff_agent.ingest import profile_hints as ph


@pytest.mark.parametrize(
    ("title", "company"),
    [
        ("CEO @ Acme", "Acme"),
        ("CEO @Acme", "Acme"),
        ("Founder at Northwind Ops", "Northwind Ops"),
        ("CEO, Acme Inc", "Acme Inc"),
        ("CEO | Acme", "Acme"),
        ("Co-founder & CEO - Acme", "Acme"),
        ("CEO en Rappi", "Rappi"),
    ],
)
def test_company_is_read_from_common_title_shapes(title, company):
    assert ph.company_from_title(title) == company


@pytest.mark.parametrize("title", ["", "CEO", "CEO, Founder", "Co-founder", "@", "x" * 200])
def test_titles_without_a_company_give_none(title):
    assert ph.company_from_title(title) is None


@pytest.mark.parametrize(
    ("email", "domain"),
    [
        ("ada@acme.com", "acme.com"),
        ("Ada@Acme.COM", "acme.com"),
        ("ada@gmail.com", None),
        ("ada@outlook.com", None),
        ("", None),
        ("no-es-un-email", None),
    ],
)
def test_a_work_email_gives_the_company_domain(email, domain):
    assert ph.domain_from_email(email) == domain
