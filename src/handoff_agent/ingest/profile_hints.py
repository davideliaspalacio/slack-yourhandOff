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
    if not title:
        return None

    # Step 1: Drop parenthetical asides
    cleaned_title = re.sub(r"\s*\([^)]*\)", "", title)

    # Step 2: Prefer current employer (@, at, en - whole words, case-insensitive)
    # Find the FIRST occurrence of @ or whole words "at" or "en"
    at_match = re.search(r"@|(?:\s|^)at(?:\s|$)|(?:\s|^)en(?:\s|$)", cleaned_title, re.IGNORECASE)

    if at_match:
        # Extract text after the marker
        text_after = cleaned_title[at_match.end() :].lstrip()
        # Cut at the next separator (|, comma, ·, •, ;, or dash surrounded by spaces)
        sep_match = re.search(r"\s*[|,·•;]\s*|\s+[-–—]\s+", text_after)
        if sep_match:
            candidate = text_after[: sep_match.start()]
        else:
            candidate = text_after
    else:
        # Step 2b: Fall back to last segment behavior (only if 2+ segments)
        parts = [
            part.strip() for part in _SEPARATORS.split(cleaned_title or "") if part and part.strip()
        ]
        if len(parts) < 2:
            return None
        candidate = parts[-1]

    candidate = candidate.strip()

    # Step 3: Reject previous-employer markers (case-insensitive)
    prev_employer_markers = [
        "ex-",
        "ex ",
        "former",
        "formerly",
        "prev",
        "prev.",
        "previously",
        "antes",
        "anteriormente",
    ]
    candidate_lower = candidate.casefold()
    for marker in prev_employer_markers:
        if candidate_lower.startswith(marker):
            return None

    # Step 4: Reject compound roles (only role words)
    # Split on &, /, +, comma and words "and"/"y"
    pieces = re.split(r"[&/+,]|\s+(?:and|y)\s+", candidate, flags=re.IGNORECASE)

    all_are_roles = True
    for piece in pieces:
        piece_clean = piece.strip()
        if not piece_clean:
            continue
        # Check as-is and normalized (hyphens and spaces removed)
        piece_lower = piece_clean.casefold()
        piece_norm = piece_lower.replace("-", "").replace(" ", "")
        if piece_lower not in ROLE_WORDS and piece_norm not in ROLE_WORDS:
            all_are_roles = False
            break

    # Only reject if there are non-empty pieces and ALL are roles
    non_empty_pieces = [p.strip() for p in pieces if p.strip()]
    if all_are_roles and non_empty_pieces:
        return None

    # Step 5: Existing guards (empty, too long, no letters)
    if not candidate or len(candidate) > MAX_COMPANY_CHARS or not re.search(r"[^\W\d_]", candidate):
        return None

    # Strip surrounding whitespace and trailing punctuation
    candidate = candidate.strip().rstrip(".,;:-")

    return candidate if candidate else None


def domain_from_email(email: str) -> str | None:
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].strip().casefold()
    if "." not in domain or domain in FREE_MAIL_DOMAINS:
        return None
    return domain
