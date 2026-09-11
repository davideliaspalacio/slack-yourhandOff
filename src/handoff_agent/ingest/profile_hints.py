"""What a Slack profile can tell the research before it starts.

The title often names the company ("CEO @ Acme") and a work email gives the
real domain, which spares the fragile guessed-domain path. Both are hints: a
wrong company here is caught by the grounding rules downstream, and a missing
one just means the research works from the name alone.
"""

from __future__ import annotations

import re

FREE_MAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "hotmail.com",
        "outlook.com",
        "live.com",
        "msn.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "gmx.com",
        "gmx.net",
        "yandex.com",
        "zoho.com",
        "hey.com",
        "fastmail.com",
        "mail.com",
    }
)

ROLE_WORDS = frozenset(
    {
        "ceo",
        "cto",
        "coo",
        "cfo",
        "cmo",
        "cro",
        "cpo",
        "founder",
        "co-founder",
        "cofounder",
        "co founder",
        "owner",
        "president",
        "partner",
        "director",
        "managing director",
        "vp",
        "head",
        "chair",
        "chairman",
        "fundador",
        "cofundador",
        "socio",
        "dueño",
    }
)

# "@", " at ", " en ", "|", ",", "·" y guiones rodeados de espacios. Un guion
# pegado ("Co-founder") no separa.
_SEPARATORS = re.compile(r"\s*@\s*|\s+at\s+|\s+en\s+|\s*[|,·]\s*|\s+[-–—]\s+", re.IGNORECASE)
MAX_COMPANY_CHARS = 60


def company_from_title(title: str) -> str | None:
    parts = [part.strip() for part in _SEPARATORS.split(title or "") if part and part.strip()]
    if len(parts) < 2:
        return None
    candidate = parts[-1]
    if (
        candidate.casefold() in ROLE_WORDS
        or len(candidate) > MAX_COMPANY_CHARS
        or not re.search(r"[^\W\d_]", candidate)
    ):
        return None
    return candidate


def domain_from_email(email: str) -> str | None:
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].strip().casefold()
    if "." not in domain or domain in FREE_MAIL_DOMAINS:
        return None
    return domain
