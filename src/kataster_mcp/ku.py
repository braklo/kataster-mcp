"""Cadastral units (katastrálne územia) from the bundled table built by scripts/build_ku_table.py."""

import json
from functools import lru_cache
from importlib import resources

from .envelope import KatasterError
from .normalize import fold

MAX_RESULTS = 20


@lru_cache(maxsize=1)
def table() -> dict:
    raw = resources.files("kataster_mcp").joinpath("data/ku.json").read_text(encoding="utf-8")
    t = json.loads(raw)
    t["_rows"] = [dict(zip(t["stlpce"], r)) for r in t["ku"]]
    t["_by_code"] = {r["kod"]: r for r in t["_rows"]}
    return t


def search(query: str) -> list[dict]:
    """Exact (diacritics-insensitive) name or code match first, then substring matches."""
    q = query.strip()
    t = table()
    if q.isdigit():
        r = t["_by_code"].get(q)
        return [r] if r else []
    fq = fold(q)
    exact = [r for r in t["_rows"] if fold(r["nazov"]) == fq]
    if exact:
        return exact
    return [r for r in t["_rows"] if fq in fold(r["nazov"])]


def resolve(ku: str) -> dict:
    """Resolve a k.ú. name or code to exactly one unit, or raise."""
    hits = search(ku)
    if not hits:
        raise KatasterError(
            "not_found", f"No cadastral unit matches '{ku}'.",
            coverage=f"bundled list of {table()['pocet']} k.ú. downloaded {table()['stiahnute']}",
        )
    if len(hits) > 1:
        raise KatasterError(
            "ambiguous", f"'{ku}' matches {len(hits)} cadastral units; use the 6-digit code.",
            details=hits[:MAX_RESULTS],
        )
    return hits[0]
