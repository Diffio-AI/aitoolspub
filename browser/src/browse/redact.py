"""Keep API tokens a page shows out of tool results and packets.

Hugging Face showed a freshly created write token in a read-only box after "Create token", and the survey returned
it verbatim into the agent's transcript and the session packet. Token-shaped strings are masked on the way out;
`save_value` writes a field's real value to a file for the agent to use without ever printing it.
"""

from __future__ import annotations

import re

# Prefixes the big providers put on their tokens, then a run of token characters.
TOKEN = re.compile(
    r"\b(hf_[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|glpat-[A-Za-z0-9_-]{20,}"
    r"|sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|npm_[A-Za-z0-9]{30,}"
    r"|pypi-[A-Za-z0-9_-]{30,}|KGAT_[A-Za-z0-9]{20,}|sk_(?:live|test)_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{35})")


def masked(token: str) -> str:
    return f"(token {token[:4]}…, {len(token)} chars: save_value writes it to a file)"


def redact(text: str) -> str:
    return TOKEN.sub(lambda m: masked(m.group(0)), text)


def looks_secret(value: str, *, readonly: bool) -> bool:
    """A read-only field holding one long random-looking word is a key the site shows once (an API key box)."""
    v = value.strip()
    return bool(TOKEN.search(v)) or (readonly and len(v) >= 24 and " " not in v and "/" not in v and "@" not in v
                                     and re.search(r"[A-Za-z]", v) is not None and re.search(r"\d", v) is not None)
