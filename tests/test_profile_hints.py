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
    ("title", "company"),
    [
        # Current employer wins over previous employer
        ("CEO @ Acme | ex-Google", "Acme"),
        ("Founder @ Acme · prev. Stripe", "Acme"),
        ("CEO @ Acme (antes en Globant)", "Acme"),
        ("Head of Growth at Acme", "Acme"),
        ("Partner at Sequoia Capital", "Sequoia Capital"),
        ("CEO en Rappi", "Rappi"),
    ],
)
def test_current_employer_wins_over_previous(title, company):
    assert ph.company_from_title(title) == company


@pytest.mark.parametrize(
    "title",
    [
        "CEO, ex-Google",
        "Founder | formerly Stripe",
    ],
)
def test_previous_employer_only_titles_give_none(title):
    assert ph.company_from_title(title) is None


@pytest.mark.parametrize(
    "title",
    [
        "Jane Doe, Co-Founder & CEO",
        "Jane Doe, CEO/Founder",
        "Some Name - Co-Founder & CEO",
    ],
)
def test_compound_roles_give_none(title):
    assert ph.company_from_title(title) is None


@pytest.mark.parametrize(
    ("title", "company"),
    [
        ("Fundadora en Café Olé", "Café Olé"),
    ],
)
def test_unicode_company_survives(title, company):
    assert ph.company_from_title(title) == company


@pytest.mark.parametrize(
    ("title", "company"),
    [
        # Marker precedence: last segment wins if it has no marker, or text after marker in last segment
        ("Growth at heart, Founder | Acme", "Acme"),  # @ not present, so look at last segment
        # Single-segment titles with marker inside: take text after marker (documented accepted misses)
        ("Data at scale guy", "scale guy"),  # Single segment; documented miss
        ("Director en jefe", "jefe"),  # Single segment; documented miss
    ],
)
def test_marker_precedence(title, company):
    assert ph.company_from_title(title) == company


@pytest.mark.parametrize(
    ("title", "company"),
    [
        # Nested and unclosed parentheses should be stripped completely
        ("CEO @ Acme (based in SF (approx))", "Acme"),
        ("CEO @ Acme (antes en Globant", "Acme"),  # Unclosed paren
    ],
)
def test_parenthesis_cleanup(title, company):
    assert ph.company_from_title(title) == company


@pytest.mark.parametrize(
    ("title", "company"),
    [
        # prev/ex markers should only match as whole tokens, not as prefixes within words
        ("CEO @ Previsora", "Previsora"),  # "prev" is only a prefix, not a whole token
        (
            "Founder at Prevail Health",
            "Prevail Health",
        ),  # "prev" is only a prefix, not a whole token
    ],
)
def test_prev_marker_as_whole_token_only(title, company):
    assert ph.company_from_title(title) == company


@pytest.mark.parametrize(
    ("title", "company"),
    [
        # Company-then-location: keep the company, not the location
        ("CEO en Rappi en Colombia", "Rappi en Colombia"),
        ("Regional Sales at Acme at NYC", "Acme at NYC"),
        ("President at University at Buffalo", "University at Buffalo"),
        ("Store Manager at Target at Downtown Location", "Target at Downtown Location"),
    ],
)
def test_company_then_location_keeps_company(title, company):
    assert ph.company_from_title(title) == company


@pytest.mark.parametrize(
    "title",
    [
        # Bare trailing markers are rejected
        "CEO, Former",
        "Founder, Formerly",
        "CEO, Antes",
    ],
)
def test_bare_trailing_markers_rejected(title):
    assert ph.company_from_title(title) is None


@pytest.mark.parametrize(
    ("title", "company"),
    [
        # Single segment with no marker is not a company
        ("Ingeniero", None),
        ("Acme", None),
    ],
)
def test_single_segment_no_marker_not_company(title, company):
    assert ph.company_from_title(title) == company


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
