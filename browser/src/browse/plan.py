"""The fill plan: what the calling model wants in each field, written by it and typed verbatim by code.

A plan maps field ids (from a survey) to entries. Shorthand: a string is exact text, a bool sets a
checkbox. Every other entry is an object with exactly one of the keys below. The code never generates,
shortens or rewords text: it only types what is here, or picks among choices written here.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

ACTIONS = ("text", "choose", "choose_all", "check", "file", "tags", "keep", "clear")


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: list[str] | None = None
    """Exact text. Several strings are variants in order of preference: the first that fits the field's
    limit is typed, and a limit the page reveals (silent truncation) moves to the next one that fits."""
    choose: list[str] | None = None
    """Ranked option labels or values for a select, radio group or combobox; the first one present wins."""
    choose_all: list[str] | None = None
    """Every option to select in a checkbox group or multi-select; others are cleared."""
    check: bool | None = None
    file: list[str] | None = None
    """Absolute file paths, or paths relative to the server's assets directory (--assets)."""
    tags: list[str] | None = None
    """Tags typed into a tag/chip input one at a time; an autocomplete suggestion is taken only on an exact match."""
    keep: bool | None = None
    """Accept what the field already holds (e.g. a site-prefilled value you have read and agree with)."""
    clear: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, v: Any) -> Any:
        if isinstance(v, str):
            return {"text": [v]}
        if isinstance(v, bool):
            return {"check": v}
        if isinstance(v, dict):
            v = dict(v)
            for k in ("text", "choose", "choose_all", "file", "tags"):
                if isinstance(v.get(k), str):
                    v[k] = [v[k]]
        return v

    @model_validator(mode="after")
    def _one(self) -> Entry:
        set_ = [k for k in ACTIONS if getattr(self, k) is not None]
        if len(set_) != 1:
            raise ValueError(f"each entry needs exactly one of {', '.join(ACTIONS)} (got {', '.join(set_) or 'none'})")
        for k in ("text", "choose", "choose_all", "file", "tags"):
            if getattr(self, k) == [] and k not in ("choose_all", "tags"):
                raise ValueError(f"'{k}' must not be empty")
        return self

    @property
    def action(self) -> str:
        return next(k for k in ACTIONS if getattr(self, k) is not None)

    def strings(self) -> list[str]:
        """Every string this entry could put on the page (for the no-foreign-text audit)."""
        out: list[str] = []
        for k in ("text", "choose", "choose_all", "tags"):
            out += getattr(self, k) or []
        return out


def parse_plan(values: dict[str, Any]) -> dict[str, Entry]:
    errors, plan = [], {}
    for fid, raw in values.items():
        try:
            plan[fid] = Entry.model_validate(raw)
        except Exception as e:  # noqa: BLE001 - reported back to the caller per field
            msg = getattr(e, "errors", None)
            errors.append(f"{fid}: {msg()[0]['msg'] if callable(msg) else e}")
    if errors:
        raise ValueError("; ".join(errors))
    return plan
