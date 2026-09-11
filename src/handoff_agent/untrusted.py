"""Third-party text, marked as such before it reaches a model.

Everything the research tools fetch was written by someone else: company sites,
careers pages, search snippets. A model reads all of it in the same context as
our instructions, so a careers page that says "ignore previous instructions"
would otherwise read exactly like one of ours.

The fence is one layer, not the whole defence. It works because the system
prompt tells the model that fenced text is data, and because nothing inside can
close the fence early. The other layers: the model never gets a tool with side
effects, and every fact it returns must cite a source we fetched ourselves.
"""

from __future__ import annotations

import html
import re

TAG = "contenido-web-no-confiable"
NEUTRALISED = "[etiqueta retirada]"

# The tag name in any case, with spaces, hyphens or underscores between words.
# Matching the bare name, not just a well-formed tag, also catches a partial
# "</contenido-web-no-confiable" left unclosed right before our own closing tag.
_TAG_NAME = re.compile(
    r"<?\s*/?\s*contenido[\s_-]*web[\s_-]*no[\s_-]*confiable[^>\n]*>?", re.IGNORECASE
)


def neutralise(text: str) -> str:
    """Remove anything that could open a fake fence or close the real one."""
    return _TAG_NAME.sub(NEUTRALISED, text)


def fence(text: str, origin: str) -> str:
    """Wrap third-party `text` so a model can tell it apart from instructions."""
    safe_origin = html.escape(neutralise(origin), quote=True)
    return f'<{TAG} origen="{safe_origin}">\n{neutralise(text)}\n</{TAG}>'
