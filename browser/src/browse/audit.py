"""The voice audit: did anything reach a site that the calling model didn't write?

Takes a session packet (or several) and what a site received, and lists every submitted text value that is
neither a planned string, an option the site itself offered, nor a value the caller explicitly kept.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


def load_packets(paths: Iterable[Path]) -> list[dict]:
    out = []
    for p in paths:
        if ".nav" in p.name:  # the navigator's own traces sit next to the packets
            continue
        out += [e for e in (json.loads(line) for line in p.read_text().splitlines() if line.strip()) if "kind" in e]
    return out


def allowed_strings(events: list[dict]) -> set[str]:
    """Strings a site may legitimately receive: what the plans said, options the site offered, kept values."""
    ok: set[str] = set()
    for e in events:
        if e["kind"] == "fill":
            for fid, entry in e["plan"].items():
                for k in ("text", "choose", "choose_all", "tags"):
                    ok.update(entry.get(k) or [])
                if entry.get("keep"):
                    r = e["results"].get(fid, {})
                    v = r.get("value")
                    if isinstance(v, str):
                        ok.add(v)
        if e["kind"] == "survey":
            for g in e["survey"].get("groups", []):
                for f in g["fields"]:
                    ok.update(o for o in f.get("options", []) if isinstance(o, str))
    return ok


def _norm(s: str) -> str:
    return " ".join(s.replace("\r\n", "\n").split()).casefold()


def foreign_values(submitted: dict[str, Any], events: list[dict], *, ignore: Iterable[str] = ()) -> list[tuple[str, str]]:
    """(field name, value) for each submitted text that nobody planned.

    A value passes if it equals an allowed string, or is a join of allowed strings (tag inputs that post
    "a,b,c"), or is a site option's value. Short machine values (on, true, ids, empty) pass.
    """
    allowed = allowed_strings(events)
    allowed_n = {_norm(a) for a in allowed}
    bad = []
    skip = set(ignore)
    for name, val in submitted.items():
        if name in skip:
            continue
        for v in val if isinstance(val, list) else [val]:
            if not isinstance(v, str):
                v = json.dumps(v) if not isinstance(v, (int, float, bool)) or isinstance(v, bool) else str(v)
                if v in ("true", "false"):
                    continue
            if v.strip() in ("", "on", "true", "false") or len(v) <= 2:
                continue
            if _norm(v) in allowed_n:
                continue
            parts = [p for p in (x.strip() for x in v.split(",")) if p]
            if len(parts) > 1 and all(_norm(p) in allowed_n for p in parts):
                continue
            if _is_option_value(v, events):
                continue
            bad.append((name, v))
    return bad


def _is_option_value(v: str, events: list[dict]) -> bool:
    # Surveys keep option labels; a select posts its value, which for real sites is a slug of the label.
    slug = lambda s: "".join(c for c in s.casefold() if c.isalnum())  # noqa: E731
    for e in events:
        if e["kind"] == "survey":
            for g in e["survey"].get("groups", []):
                for f in g["fields"]:
                    if any(slug(o) == slug(v) for o in f.get("options", []) if isinstance(o, str)):
                        return True
    return False
