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

# "@", "|", ",", "·", "•", ";", and dashes surrounded by spaces.
# "at" and "en" are no longer separators; they're markers within the last segment.
_SEPARATORS = re.compile(r"\s*[|,·•;]\s*|\s+[-–—]\s+", re.IGNORECASE)
MAX_COMPANY_CHARS = 60

# Marker vocabulary for previous employers, defined once and used by both checks below.
# Note: 'formally' (with an 'a') is a common misspelling of 'formerly' (past tense),
# and treating it as a marker only ever produces a safe None instead of a wrong company.
_PREV_EMPLOYER_MARKERS = (
    "previously",
    "formerly",
    "formally",
    "former",
    "prev",
    "ex",
    "antes",
    "anteriormente",
)

# Pattern for checking if a word (exactly) is a previous-employer marker
# Built from _PREV_EMPLOYER_MARKERS
_WORD_IS_PREV_EMPLOYER = re.compile(
    r"^(?:"
    r"ex-?|"  # "ex-" or "ex"
    r"former|formally|formerly|"  # "former", "formally", or "formerly"
    r"prev(?:\.|iously)?|"  # "prev", "prev.", "previously"
    r"antes|anteriormente"  # Spanish markers
    r")$",
    re.IGNORECASE,
)

# Pattern for checking if candidate starts with a previous-employer marker
# Built from _PREV_EMPLOYER_MARKERS
_PREV_EMPLOYER_CANDIDATE = re.compile(
    r"^(?:"
    r"ex-|ex\s+|"  # "ex-" or "ex " at the start
    r"\bformer\b|\bformally\b|\bformerly\b|"  # "former", "formally", or "formerly" as whole words
    r"\bprev(?:\.|iously|$)?(?:\s|$)|"  # "prev" when followed by ".", a space, "iously", or end
    r"\bantes\b|\banteriormente\b"  # "antes" and "anteriormente" as whole words
    r")",
    re.IGNORECASE,
)


def _has_prev_employer_marker_before_position(text: str) -> bool:
    """Check if the last word of text is a previous-employer marker."""
    if not text:
        return False
    words = text.split()
    if not words:
        return False
    last_word = words[-1].rstrip(".,;:-")
    return bool(_WORD_IS_PREV_EMPLOYER.match(last_word.casefold()))


def company_from_title(title: str) -> str | None:
    if not title:
        return None

    # Step 1: Drop parenthetical asides (repeatedly to handle nesting)
    cleaned_title = title
    prev = None
    while prev != cleaned_title:
        prev = cleaned_title
        cleaned_title = re.sub(r"\s*\([^)]*\)", "", cleaned_title)
    # Remove any leftover unmatched parenthesis
    if "(" in cleaned_title:
        cleaned_title = cleaned_title.split("(")[0]
    cleaned_title = cleaned_title.replace(")", "")

    # Step 2: Prefer current employer (@, at, en)
    # Check if @ is present
    if "@" in cleaned_title:
        # Take text after the FIRST @
        at_index = cleaned_title.index("@")
        # Check if the word before @ is a previous-employer marker
        text_before = cleaned_title[:at_index].rstrip()
        if _has_prev_employer_marker_before_position(text_before):
            return None
        text_after = cleaned_title[at_index + 1 :].lstrip()
        # Cut at the next separator (|, comma, ·, •, ;, or dash surrounded by spaces)
        sep_match = re.search(r"\s*[|,·•;]\s*|\s+[-–—]\s+", text_after)
        if sep_match:
            candidate = text_after[: sep_match.start()]
        else:
            candidate = text_after
    else:
        # Step 2b: No @, so split on separators and look at last segment
        parts = [
            part.strip() for part in _SEPARATORS.split(cleaned_title or "") if part and part.strip()
        ]

        # Get the last non-empty part as segment (if no separators, segment is the whole title)
        segment = parts[-1] if parts else ""

        # Check if segment contains the whole word "at" or "en"
        at_match = re.search(r"\bat\b|\ben\b", segment, re.IGNORECASE)
        if at_match:
            # Check if the word before the marker is a previous-employer marker
            text_before = segment[: at_match.start()].rstrip()
            if _has_prev_employer_marker_before_position(text_before):
                return None
            # Take text after the first whole-word "at" or "en" in the segment
            text_after = segment[at_match.end() :].lstrip()
            candidate = text_after
        else:
            # If there were at least two parts, use the segment; otherwise return None
            if len(parts) < 2:
                return None
            candidate = segment

    candidate = candidate.strip()

    # Step 3: Reject previous-employer markers (as word-boundary patterns)
    # Use the module-level pattern built from _PREV_EMPLOYER_MARKERS
    candidate_lower = candidate.casefold()
    if _PREV_EMPLOYER_CANDIDATE.match(candidate_lower):
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
